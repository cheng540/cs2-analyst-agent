"""从真实配置生成可安全分享的配置模板（清空密钥）。

用途：把代码发给别人 / 传到公开仓库前，用它生成一份不含密钥的 config.example.yaml。

用法：
    python tools/make_config_example.py            # 生成 config/config.example.yaml
    python tools/make_config_example.py --check    # 只检查当前配置里有无密钥残留（不生成）

环境变量优先：STEAMDT_API_KEY / DEEPSEEK_API_KEY / WECOM_WEBHOOK
（设置了环境变量时，config.yaml 里可以留空，适合服务器与 CI）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "config" / "config.yaml"
DST = ROOT / "config" / "config.example.yaml"

# 需要清空的敏感字段
SECRET_PATTERNS = (
    r'(api_key:\s*)".*?"',
    r'(wecom_webhook:\s*)".*?"',
)
# 密钥残留特征：sk- 开头的长串、32/64 位十六进制串
LEAK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}|(?<![A-Za-z0-9])[0-9a-f]{32}(?![A-Za-z0-9])")

HEADER = """# ============================================================
# 配置模板（可安全分享 / 提交到公开仓库）
#
# 用法：复制为 config/config.yaml 后填入自己的 Key；
# 或直接用环境变量提供（优先于本文件，适合服务器 / CI）：
#     STEAMDT_API_KEY / DEEPSEEK_API_KEY / WECOM_WEBHOOK
# ============================================================

"""


def build_example(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = re.sub(pat, r'\1""', text)
    return text


def find_leaks(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if LEAK_RE.search(ln)]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成不含密钥的配置模板 / 检查密钥残留")
    ap.add_argument("--check", action="store_true", help="只检查残留，不生成文件")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"❌ 找不到 {SRC}")
        return 1
    text = SRC.read_text(encoding="utf-8")
    leaks = find_leaks(text)

    if args.check:
        if leaks:
            print(f"⚠️ 检测到 {len(leaks)} 处疑似密钥：")
            for ln in leaks:
                print("   " + ln[:100])
            print("\n提示：这些行不要提交到公开仓库；已提交过的话请到平台重置 Key。")
            return 1
        print("✅ 未检测到明文密钥")
        return 0

    example = HEADER + build_example(text)
    left = find_leaks(example)
    if left:
        print("⚠️ 生成的模板里仍有疑似密钥，已中止：")
        for ln in left:
            print("   " + ln[:100])
        return 1
    DST.write_text(example, encoding="utf-8")
    print(f"✅ 已生成 {DST.relative_to(ROOT)}（密钥已清空，可安全分享）")
    if leaks:
        print(f"ℹ️ 原配置里有 {len(leaks)} 处密钥——它们只保留在 config/config.yaml（已被 .gitignore 排除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
