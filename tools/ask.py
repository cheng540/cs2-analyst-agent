"""命令行问答工具（与网页问答共用同一套工具与校验逻辑）。

用法：
    python tools/ask.py "AWP 二西莫夫现在值得买吗？"
    python tools/ask.py --tools          # 只列出可用工具
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.chat.assistant import ChatAssistant  # noqa: E402
from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir())
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--tools":
        from cs2analyst.chat.tools import TOOL_SCHEMAS
        for t in TOOL_SCHEMAS:
            fn = t["function"]
            print(f"- {fn['name']}: {fn['description'][:70]}…")
        return 0

    question = " ".join(args)
    print(f"❓ {question}\n")
    assistant = ChatAssistant(cfg)
    result = assistant.ask(question)
    if result.get("error"):
        print(f"❌ {result['error']}")
    print(result.get("answer") or "（无回答）")
    print(f"\n🔧 调用工具：{'、'.join(result.get('tools_used') or []) or '无'}")
    if result.get("unverified_numbers"):
        print(f"⚠️  未核对到的数字：{result['unverified_numbers']}")
    else:
        print("✅ 数字校验：答案中的数字均可在数据源中找到")
    usage = result.get("usage") or {}
    if usage:
        print(f"📊 tokens：{usage.get('total_tokens')}")
    ev = result.get("evidence") or []
    if ev:
        print("\n—— 依据（前 400 字）——")
        for e in ev:
            print(f"[{e['tool']}] {json.dumps(e['result'], ensure_ascii=False)[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
