#!/usr/bin/env python3
"""重建经验库索引（RAG）。

用法：
    python tools/build_rag_index.py              # 重建并打印统计
    python tools/build_rag_index.py --stats      # 只看现有索引统计，不重建
    python tools/build_rag_index.py --query "纪念品和普通版有什么区别"  # 重建后试检索

索引落在 data/rag/index.json（个人数据，不进 git）。
语料来源见 cs2analyst/rag/corpus.py：knowledge/*.md、平台规则、官方公告、
历史研判与「案例卡片」（当时判断 + 之后实际走势）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.rag.store import build_index, load_index, search_knowledge  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="重建经验库索引")
    ap.add_argument("--query", "-q", default="", help="重建后顺便试检索一句")
    ap.add_argument("--stats", action="store_true", help="只读现有索引统计，不重建")
    ap.add_argument("--top", type=int, default=5, help="试检索返回条数")
    args = ap.parse_args()

    if args.stats:
        index = load_index(ROOT, auto_build=False)
        print(f"现有索引：{len(index)} 篇文档")
        return 0

    info = build_index(ROOT)
    print(f"索引已重建：{info['total']} 篇 / {info['chars']:,} 字 → {info['index']}")
    for kind, count in sorted(info["by_kind"].items()):
        print(f"  {kind:<10} {count:>4}")

    if args.query:
        print(f"\n试检索：{args.query}")
        result = search_knowledge(ROOT, args.query, k=args.top)
        for i, hit in enumerate(result.get("results") or [], 1):
            print(f"  {i}. [{hit['kind']}] {hit['title']}  (score {hit['score']})")
            print(f"     {hit['snippet'][:100]}")
        if not result.get("results"):
            print("  （无命中）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
