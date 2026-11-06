"""配置文件与数据文件的安全读写层。

为什么需要这一层（2026-10 实际约束）：
    每日流水线由 cron 在 17:30 触发，Web 后端是常驻进程。两者会同时碰
    `config/inventory.json`、`config/config.yaml`、`data/` 下的文件。
    直接 `open(..., "w")` 有两个致命问题：
      1. 写到一半被另一个进程读到 → 读方拿到半截 JSON，整个日报跑挂；
      2. 读-改-写之间被插进一次别人的写 → 后写覆盖前写（丢数据）。

因此所有写入必须走本模块，遵守三条：
    · 加锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）——避免并发写；
    · 写临时文件 + `os.replace()` 原子替换 —— 避免读到半截文件；
    · **写前在锁内重读** —— 避免用旧内存副本覆盖别人刚写的内容。

YAML 写入另外做一件事：**保留注释**。配置文件里每一行解释都是给人看的，
`yaml.safe_dump()` 会把注释全部抹掉，所以这里改用逐行定点替换标量。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

LOCK_SUFFIX = ".lock"
_DEFAULT_TIMEOUT = 10.0
_POLL = 0.02


class LockTimeout(RuntimeError):
    """在超时时间内没拿到文件锁。"""


# --------------------------------------------------------------------------
# 文件锁
# --------------------------------------------------------------------------
def _try_lock(fh) -> bool:
    """尝试对已打开的文件句柄加非阻塞排他锁；成功返回 True。"""
    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(target: Path | str, timeout: float = _DEFAULT_TIMEOUT) -> Iterator[None]:
    """对 `target` 加进程间排他锁（锁文件为 `<target>.lock`）。

    用法：`with file_lock(path): ...`。锁是**协同锁**：只有同样走本函数的代码
    才会互相排斥，所以项目内的写操作必须全部走这里。
    """
    target = Path(target)
    lock_path = target.with_name(target.name + LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    deadline = time.monotonic() + max(0.1, timeout)
    got = False
    try:
        while True:
            if _try_lock(fh):
                got = True
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(f"等待文件锁超时：{lock_path}")
            time.sleep(_POLL)
        yield
    finally:
        if got:
            _unlock(fh)
        fh.close()


# --------------------------------------------------------------------------
# 原子写
# --------------------------------------------------------------------------
def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """写临时文件 → fsync → `os.replace` 原子替换。读者永远看到完整内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)   # 同目录内替换，POSIX/Windows 都是原子的
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


# --------------------------------------------------------------------------
# JSON 读写
# --------------------------------------------------------------------------
def read_json(path: Path | str, default: Any = None) -> Any:
    """读 JSON；文件不存在或损坏时返回 `default`（不抛异常）。"""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def write_json(path: Path | str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def update_json(path: Path | str, mutator: Callable[[Any], Any], *,
                default: Any = None, timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """锁内「重读 → 改 → 原子写回」，返回改后的对象。

    `mutator` 接收当前磁盘上的**最新**内容（不是调用方内存里的旧副本），
    返回值即写回内容；原地修改并返回 None 也支持（此时写回被原地改的对象）。
    """
    path = Path(path)
    with file_lock(path, timeout=timeout):
        fresh = read_json(path, default)
        result = mutator(fresh)
        to_write = fresh if result is None else result
        write_json(path, to_write)
        return to_write


# --------------------------------------------------------------------------
# YAML：保留注释的定点改写
# --------------------------------------------------------------------------
_KEY_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_\-]+):(?P<rest>.*)$")
_COMMENT_SPLIT = re.compile(r"\s+#")


def _format_scalar(value: Any) -> str:
    """把 Python 值格式化成 YAML 标量文本（字符串一律单引号，安全）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_scalar(v) for v in value) + "]"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _split_value_comment(rest: str) -> tuple[str, str]:
    """把 `value  # 注释` 拆成 (value, '  # 注释')，注释原样保留。"""
    rest = rest.rstrip()
    if not rest:
        return "", ""
    m = _COMMENT_SPLIT.search(rest)
    if m:
        return rest[: m.start()].rstrip(), rest[m.start():]
    return rest, ""


def patch_yaml_scalars(path: Path | str, updates: dict[str, Any], *,
                       timeout: float = _DEFAULT_TIMEOUT) -> dict[str, bool]:
    """按点号路径改 YAML 标量，**不动注释与顺序**。

    `updates` 形如 `{"steamdt.api_key": "sk-xx", "alerts.enabled": True}`
    （只支持两级：`节.键`）。返回 `{路径: 是否命中已有行}`；未命中的会追加到
    所属小节末尾（小节不存在则新建）。
    """
    path = Path(path)
    with file_lock(path, timeout=timeout):
        try:
            original = path.read_text(encoding="utf-8-sig")
        except OSError:
            original = ""
        lines = original.splitlines()
        hit: dict[str, bool] = {}

        for dotted, value in updates.items():
            section, _, key = dotted.partition(".")
            if not key:                     # 只给了节名 → 跳过，避免写坏结构
                continue
            new_line = f"  {key}: {_format_scalar(value)}"
            section_at = None
            for idx, line in enumerate(lines):
                m = _KEY_LINE.match(line)
                if m and m.group("indent") == "" and m.group("key") == section:
                    section_at = idx
                    break
            if section_at is None:          # 小节不存在 → 文件末尾新建
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"{section}:")
                lines.append(new_line)
                hit[dotted] = False
                continue

            end = len(lines)
            for idx in range(section_at + 1, len(lines)):
                stripped = lines[idx].strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not lines[idx].startswith((" ", "\t")):   # 下一个顶级键
                    end = idx
                    break

            found = False
            for idx in range(section_at + 1, end):
                m = _KEY_LINE.match(lines[idx])
                if not (m and len(m.group("indent")) >= 2 and m.group("key") == key):
                    continue
                indent = m.group("indent")
                raw_value, comment = _split_value_comment(m.group("rest"))
                drop_to = idx + 1
                if not raw_value:
                    # 值是块状写法（块序列 / 嵌套映射），续行缩进比键更深，必须一起删掉，
                    # 否则替换后会留下孤立的 `- 9` 行，整个 YAML 直接解析失败。
                    base = len(indent)
                    while drop_to < end:
                        nxt = lines[drop_to]
                        if not nxt.strip():
                            break
                        lead = len(nxt) - len(nxt.lstrip())
                        # 块序列的 `- x` 允许与键同缩进（YAML 合法写法），所以单独放行
                        if lead <= base and not nxt.lstrip().startswith("-"):
                            break
                        if lead < base:
                            break
                        drop_to += 1
                lines[idx] = f"{indent}{key}: {_format_scalar(value)}{comment}"
                del lines[idx + 1: drop_to]
                found = True
                break
            if not found:
                lines.insert(end, new_line)
            hit[dotted] = found

        atomic_write_text(path, "\n".join(lines) + "\n")
    return hit


def read_yaml(path: Path | str, default: Any = None) -> Any:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError):
        return {} if default is None else default
    return {} if data is None else data


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


# --------------------------------------------------------------------------
# 密钥掩码
# --------------------------------------------------------------------------
def mask_secret(value: Any, keep: int = 4) -> str:
    """`sk-abcdefgh1234` → `sk-****1234`。空值返回空串，前端拿不到明文。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep:
        return "*" * len(text)
    head = ""
    if text.startswith("sk-") or "-" in text[:6]:
        head = text.split("-", 1)[0] + "-"
    return f"{head}{'*' * 4}{text[-keep:]}"


def backup_file(path: Path | str, keep: int = 10) -> Path | None:
    """写前备份（同目录 `.bak.<时间戳-毫秒-PID>`），最多保留 `keep` 份。

    时间戳必须带毫秒 + PID：早期版本只到秒，同一秒内连续写两次会**覆盖掉前一个备份**，
    等于把唯一能回滚的那份也弄丢了。
    """
    path = Path(path)
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{stamp}-{int(time.time() * 1000) % 1000:03d}-{os.getpid()}")
    try:
        bak.write_bytes(path.read_bytes())
    except OSError:
        return None
    olds = sorted(path.parent.glob(f"{path.name}.bak.*"))
    if len(olds) > keep:
        for old in olds[:-keep]:
            with contextlib.suppress(OSError):
                old.unlink()
    return bak
