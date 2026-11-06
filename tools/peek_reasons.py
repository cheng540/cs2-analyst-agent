"""打印最新快照里各条的建议 + 判定理由（不调接口）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "data" / "analysis"
    files = sorted(root.glob("*.json"))
    if not files:
        print("没有分析快照")
        return 1
    path = files[-1]
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"快照：{path.name}")
    for it in (data.get("items") or []):
        print(f"\n{it.get('中文名') or it.get('饰品')}")
        print(f"  建议：{it.get('系统建议')}  信心 {it.get('信心度')}  评分 {it.get('评分')}"
              f"  盈亏 {it.get('盈亏%')}%")
        for k in ("趋势", "均线排列", "位置", "量价", "流动性", "最近支撑", "最近压力",
                  "已跌破的前支撑", "近3日涨跌%", "近5日涨跌%"):
            if it.get(k) not in (None, ""):
                print(f"    {k}：{it.get(k)}")
        for r in (it.get("理由") or []):
            print(f"    · 理由：{r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
