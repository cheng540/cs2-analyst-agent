#!/usr/bin/env python3
"""发布前脱敏自检：扫一遍仓库里有没有密钥、webhook、服务器地址、真实持仓。

用法：
    python tools/check_secrets.py            # 扫描，发现问题返回码 1
    python tools/check_secrets.py --verbose  # 同时列出被跳过的文件

为什么单独写一个脚本而不是靠 `git grep`：
1. 这个项目发出去时可能还没 `git init`，`git grep` 什么都扫不到；
2. 光扫关键词不够——真正的风险是「配置文件被误提交」，所以要显式列出跳过清单，
   让人一眼看出哪些文件本来就不该进仓库；
3. 需要一个能在测试里跑的退出码，防止以后有人手滑把 key 写进代码。

⚠️ 本脚本只做静态扫描，不能代替「去服务商后台吊销并重置」。
   如果密钥曾经提交过，删文件没用——历史里还在。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 扫描时直接跳过的目录 / 文件（这些本来就属于个人数据或运行时产物）
SKIP_DIRS = {
    ".git", "data", "logs", "reports", "reports_from_cloud", "求职材料",
    "venv", ".venv", "env", "__pycache__", ".idea", ".vscode", "node_modules",
    ".workbuddy", ".pytest_cache", "dist", "build",
}
SKIP_FILE_PATTERNS = (
    "config/config.yaml",          # 含真实 Key
    "config/report_server.json",   # 含网页账号密码
    "config/inventory.json",       # 真实持仓
    "config/inventory_demo.json",  # 演示持仓（合成数据，其实可以提交，这里保守跳过）
    "*.bak.*",                     # 写配置时的自动备份，含 Key
    "*.docx", "*.pdf", "*.png", "*.jpg", "*.zip",
    ".env", "*.env",
)

# 已知含服务器信息的内部文档：不是"泄漏"，但发布前必须删掉
INTERNAL_DOCS = {
    "cs2持仓助手交接.md": "内部交接文档（含服务器 IP / SSH / 运维命令），发布前删除",
    "PROJECT_MEMO.md": "内部项目备忘（含真实持仓与决策过程），发布前按需删除或改写",
}

# (名称, 正则, 说明)
PATTERNS = [
    ("DeepSeek/OpenAI 风格 Key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
     "模型 API Key 明文"),
    ("企业微信 webhook", re.compile(r"qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9\-]{8,}"),
     "机器人地址泄露 = 别人能往你的群发消息"),
    ("服务器 IP", re.compile(r"\b124\.220\.\d{1,3}\.\d{1,3}\b"), "生产服务器地址"),
    ("私钥内容", re.compile(r"BEGIN (RSA|OPENSSH|EC) PRIVATE KEY"), "私钥被贴进了代码"),
    ("Windows 用户目录路径", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.\-]+"),
     "暴露本机用户名（顺带暴露 home 下的配置文件路径）"),
    ("明文密码字段", re.compile(r'"password"\s*:\s*"(?!change-me|\*{3,}|)[^"]{4,}"'),
     "配置文件里的明文密码"),
    ("SteamDT Key", re.compile(r'"api_key"\s*:\s*"([a-f0-9]{32})"'),
     "32 位十六进制密钥（SteamDT 风格）"),
]

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".sh", ".ps1", ".cmd",
                 ".js", ".css", ".html", ".toml", ".ini", ".cfg", ".example", ""}


def _skipped(rel: str, name: str) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(rel, pat) for pat in SKIP_FILE_PATTERNS)


def iter_files(verbose: bool = False):
    """遍历待检文件，产出 (相对路径, 文本)；被跳过的文件在 verbose 时打印。"""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts[:-1]):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if _skipped(rel, path.name):
            if verbose:
                print(f"  跳过（个人数据/备份）：{rel}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        yield rel, text


def scan(verbose: bool = False) -> tuple[list[tuple[str, int, str, str]], list[str]]:
    """返回 (违规列表, 内部文档提示)。违规项为 (文件, 行号, 规则名, 行内容摘要)。"""
    hits: list[tuple[str, int, str, str]] = []
    notes: list[str] = []
    for rel, text in iter_files(verbose=verbose):
        if rel in INTERNAL_DOCS:
            notes.append(f"{rel}：{INTERNAL_DOCS[rel]}")
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if "check_secrets" in line or line.lstrip().startswith("# 已知"):
                continue
            if "check-secrets:ignore" in line:      # 行内豁免（例如测试里的假 key）
                continue
            for name, pattern, _why in PATTERNS:
                if pattern.search(line):
                    snippet = line.strip()[:110]
                    hits.append((rel, line_no, name, snippet))
                    break
    return hits, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前脱敏自检")
    ap.add_argument("--verbose", "-v", action="store_true", help="列出被跳过的文件")
    args = ap.parse_args()

    print(f"扫描目录：{ROOT}")
    hits, notes = scan(verbose=args.verbose)

    if notes:
        print("\n内部文档提醒（不是泄漏，但发布前要处理）：")
        for note in notes:
            print(f"  · {note}")

    if hits:
        print(f"\n发现 {len(hits)} 处可疑内容：")
        for rel, line_no, name, snippet in hits:
            print(f"  ✗ {rel}:{line_no}  [{name}]  {snippet}")
        print("\n处理办法：")
        print("  1) 真实密钥/地址从代码与文档里删掉，改走 config/config.yaml 或环境变量；")
        print("  2) 去服务商后台**吊销并重新生成**密钥（只删文件没用，历史里还在）；")
        print("  3) 确认 .gitignore 覆盖了 config/config.yaml、config/*.bak.*、data/、logs/、reports/。")
        return 1

    print("\n✓ 没有发现密钥 / webhook / 服务器地址 / 明文密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
