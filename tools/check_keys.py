#!/usr/bin/env python3
"""Key 体检：确认「我明明配过了」到底有没有生效。

它回答四个问题：
    1. 当前进程实际用的是哪把 Key（配置文件 / 环境变量，谁覆盖谁）；
    2. 这把 Key 在服务商那边是否有效（真的打一次接口，不看配置写没写）；
    3. 配置文件和 mtime（什么时候改的，改的是哪份文件）；
    4. SteamDT 的 Key 是否可用。

用法：
    python tools/check_keys.py                 # 体检（会真的调一次接口）
    python tools/check_keys.py --offline       # 只看本地配置，不联网
    python tools/check_keys.py --set-llm-key   # 交互式写入新 Key（输入不回显、不进 shell 历史）

为什么需要"真的调一次"：
    配置文件里写着 Key ≠ Key 有效（可能吊销、过期、复制少一位），
    而界面上的「已配置」只代表非空。401 只有真打接口才会暴露。
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from cs2analyst.config import PROJECT_ROOT, load_config  # noqa: E402
from cs2analyst.web.store import patch_yaml_scalars  # noqa: E402

CFG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_LLM = "DEEPSEEK_API_KEY"
ENV_STEAMDT = "STEAMDT_API_KEY"


def fingerprint(key: str) -> str:
    if not key:
        return "（空）"
    digest = hashlib.sha256(key.encode()).hexdigest()[:6]
    return f"{key[:5]}…{key[-4:]}  长度{len(key)}  sha256:{digest}"


def _mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "文件不存在"


def check_llm(key: str) -> tuple[bool, str]:
    """调一次 /v1/models：只看鉴权是否通过，几乎不花钱。"""
    try:
        resp = requests.get("https://api.deepseek.com/v1/models",
                            headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except Exception as exc:                       # noqa: BLE001
        return False, f"网络不通：{exc}"
    if resp.status_code == 200:
        names = [m.get("id") for m in (resp.json().get("data") or [])]
        return True, f"有效（可用模型：{'、'.join(names[:4]) or '未返回列表'}）"
    if resp.status_code == 401:
        return False, "401 无效：Key 被吊销/过期，或复制时少了字符"
    if resp.status_code == 402:
        return False, "402 余额不足：去 DeepSeek 后台充值"
    return False, f"HTTP {resp.status_code}：{resp.text[:120]}"


def check_steamdt(key: str) -> tuple[bool, str]:
    try:
        resp = requests.get("https://open.steamdt.com/open/cs2/broad/v1/index",
                            headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except Exception as exc:                       # noqa: BLE001
        return False, f"网络不通：{exc}"
    if resp.status_code == 200:
        return True, "有效"
    if resp.status_code in (401, 403):
        return False, f"{resp.status_code} 无效：Key 不对，或该接口未开通权限"
    return False, f"HTTP {resp.status_code}：{resp.text[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Key 体检")
    ap.add_argument("--offline", action="store_true", help="只读本地配置，不联网验证")
    ap.add_argument("--set-llm-key", action="store_true", help="交互式写入新的 DeepSeek Key")
    args = ap.parse_args()

    if args.set_llm_key:
        print("粘贴新的 DeepSeek Key（以 sk- 开头，输入时不显示）：")
        new_key = getpass.getpass("Key: ").strip()
        if not new_key.startswith("sk-") or len(new_key) < 20:
            print("格式不对：应该是 sk- 开头的一长串。")
            return 1
        patch_yaml_scalars(CFG_PATH, {"llm.api_key": new_key})
        print(f"已写入 {CFG_PATH}")
        print("提示：如果服务器上设了环境变量 DEEPSEEK_API_KEY，它会覆盖这个文件，需要改环境变量。")
        return 0

    cfg = load_config()
    llm_key = str(cfg.llm.get("api_key") or "").strip()
    steamdt_key = str(cfg.steamdt.get("api_key") or "").strip()

    print("=" * 62)
    print(f"配置文件：{CFG_PATH}")
    print(f"  最后修改：{_mtime(CFG_PATH)}")
    print(f"  存在    ：{'是' if CFG_PATH.exists() else '否（还没跑过 main.py init？）'}")
    print("-" * 62)
    print(f"DeepSeek Key（生效值）：{fingerprint(llm_key)}")
    print(f"  来源：{'环境变量 ' + ENV_LLM + '（覆盖了文件）' if _env_override(ENV_LLM) else 'config/config.yaml'}")
    print(f"SteamDT Key（生效值） ：{fingerprint(steamdt_key)}")
    print(f"  来源：{'环境变量 ' + ENV_STEAMDT + '（覆盖了文件）' if _env_override(ENV_STEAMDT) else 'config/config.yaml'}")

    if args.offline:
        print("=" * 62)
        print("（--offline：没有联网验证）")
        return 0

    print("-" * 62)
    if llm_key:
        ok, msg = check_llm(llm_key)
        print(f"DeepSeek 实测：{'✓ ' if ok else '✗ '}{msg}")
    else:
        ok = False
        print("DeepSeek 实测：✗ 未配置（AI 研判、问答、识图都会跳过或报错）")

    if steamdt_key:
        s_ok, s_msg = check_steamdt(steamdt_key)
        print(f"SteamDT  实测：{'✓ ' if s_ok else '✗ '}{s_msg}")
    else:
        s_ok = False
        print("SteamDT  实测：✗ 未配置（会降级成官网快照模式，没有历史 K 线）")

    print("=" * 62)
    if not ok:
        print("修复：python tools/check_keys.py --set-llm-key   （输入不回显）")
    return 0 if (ok and s_ok) else 1


def _env_override(name: str) -> bool:
    import os
    return bool(os.environ.get(name, "").strip())


if __name__ == "__main__":
    raise SystemExit(main())
