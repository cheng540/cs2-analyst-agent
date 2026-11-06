"""只看某一类建议的标的（默认「分批减仓」），省得把 8 条全读一遍。

用法：
    python tools/peek_actions.py                # 默认看「分批减仓」
    python tools/peek_actions.py 清仓
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEYS = ("数量", "现价", "盈亏%", "趋势", "位置", "量价", "流动性",
        "最近支撑", "最近压力", "目标仓位%")


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else "分批减仓"
    root = Path(__file__).resolve().parent.parent / "data" / "analysis"
    files = sorted(root.glob("*.json"))
    if not files:
        print("没有分析快照")
        return 1
    snap = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"快照 {files[-1].name} | 筛选：{want}")
    hits = [it for it in (snap.get("items") or []) if want in str(it.get("系统建议") or "")]
    if not hits:
        print("（没有这类建议）")
        return 0
    for it in hits:
        print(f"\n{it.get('系统建议')}  {it.get('中文名') or it.get('饰品')}")
        for k in KEYS:
            if it.get(k) not in (None, ""):
                print(f"    {k}：{it.get(k)}")
        for r in (it.get("理由") or [])[:3]:
            print(f"    · {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
