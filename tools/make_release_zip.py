#!/usr/bin/env python3
"""打发布包：把可公开的代码打成 zip，自动剔除密钥、真实持仓、服务器信息。

用法：
    python tools/make_release_zip.py                 # 输出到 dist/cs2-analyst-<版本>-<日期>.zip
    python tools/make_release_zip.py --check-only    # 只检查会打进去什么，不生成 zip

打进去的标准是「别人 clone 下来能直接跑」：
    ✅ 代码、测试、示例配置、knowledge/、README
    ❌ config/config.yaml（真实 Key）、config/inventory.json（真实持仓）、
       config/report_server.json（网页密码）、config/*.bak.*、*.lock
    ❌ data/ logs/ reports/（个人数据与产物）
    ❌ 交接文档、项目备忘、求职材料（内部资料）
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.1"

EXCLUDE_DIRS = {
    "data", "logs", "reports", "reports_from_cloud", "求职材料",
    ".git", ".idea", ".vscode", ".workbuddy", "__pycache__", "dist",
    "venv", ".venv", "env", "node_modules", ".pytest_cache",
}

EXCLUDE_FILES = (
    "config/config.yaml",
    "config/config.yaml.*",        # .lock / .bak.<时间戳>
    "config/report_server.json",
    "config/inventory.json",
    "config/inventory_demo.json",
    "config/name_overrides.json",  # 含个人饰品名对照
    "config/sim_watchlist.json",   # 个人观察池
    "cs2持仓助手交接.md",           # 内部文档：含服务器 IP / SSH
    "PROJECT_MEMO.md",             # 内部备忘：含真实持仓与决策
    ".env",
    "*.pyc", "*.pyo", "*.zip", "*.docx", "*.pdf",
)

# 与 tools/check_secrets.py 保持一致的关键词（发布包不能带这些）
SECRET_PATTERNS = [
    ("模型 Key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("企业微信 webhook", re.compile(r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9\-]{8,}")),
    ("服务器 IP", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("私钥内容", re.compile(r"BEGIN (RSA|OPENSSH|EC) PRIVATE KEY")),
    ("Windows 用户路径", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.\-]+")),
]
# 允许出现的「像 IP 但不是个人信息」的地址
IP_ALLOW = {"127.0.0.1", "0.0.0.0", "1.2.3.4"}
# 内网/保留地址常出现在测试里，不是个人信息
IP_ALLOW_PREFIX = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                   "172.2", "172.30.", "172.31.", "0.", "255.")


def wanted(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return False
    if parts and parts[0] in EXCLUDE_DIRS:
        return False
    return not any(fnmatch.fnmatch(rel, pat) for pat in EXCLUDE_FILES)


def collect() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if wanted(rel):
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def scan(files: list[Path]) -> list[str]:
    """在打包前扫一遍：宁可拒绝打包，也不要发出去才发现。"""
    problems: list[str] = []
    for path in files:
        if path.suffix.lower() not in {".py", ".md", ".txt", ".json", ".yaml", ".yml",
                                       ".sh", ".ps1", ".cmd", ".js", ".css", ".html",
                                       ".toml", ".ini", ".example", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for line_no, line in enumerate(text.splitlines(), 1):
            if "check-secrets:ignore" in line or "check_secrets" in line:
                continue
            for name, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if name == "服务器 IP" and (match.group() in IP_ALLOW
                                             or match.group().startswith(IP_ALLOW_PREFIX)):
                    continue
                problems.append(f"{rel}:{line_no} [{name}] {line.strip()[:90]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="打发布包（自动剔除敏感文件）")
    ap.add_argument("--check-only", action="store_true", help="只打印清单并检查，不生成 zip")
    ap.add_argument("--out", default="", help="输出 zip 路径")
    args = ap.parse_args()

    files = collect()
    total_kb = sum(f.stat().st_size for f in files) / 1024
    print(f"待打包 {len(files)} 个文件，约 {total_kb:.0f} KB")

    by_top: dict[str, int] = {}
    for f in files:
        top = f.relative_to(ROOT).parts[0]
        by_top[top] = by_top.get(top, 0) + 1
    for top, count in sorted(by_top.items()):
        print(f"  {top:<22} {count:>4}")

    problems = scan(files)
    if problems:
        print(f"\n✗ 发现 {len(problems)} 处不该发布的内容，已中止：")
        for item in problems:
            print("   ", item)
        return 1
    print("\n✓ 敏感信息检查通过（无 Key / webhook / 服务器 IP / 本机路径）")

    if args.check_only:
        return 0

    out = Path(args.out) if args.out else (
        ROOT / "dist" / f"cs2-analyst-v{VERSION}-{dt.date.today():%Y%m%d}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            zf.write(path, arcname=(Path("cs2-analyst") / path.relative_to(ROOT)).as_posix())

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\n✓ 已生成：{out}（{size_mb:.2f} MB，{len(files)} 个文件）")
    print("  解压后进入 cs2-analyst/ 目录，执行：")
    print("    pip install -r requirements.txt")
    print("    python main.py demo                      # 离线跑通，不需要任何 Key")
    print("    python tools/report_server.py            # 控制台 http://127.0.0.1:8899/app/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
