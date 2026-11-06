"""看一眼最新分析快照里每件的「系统建议」（不调接口）。"""
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
    print(f"快照：{path.name} | 大盘 {data.get('market', {}).get('趋势')} / "
          f"{data.get('market', {}).get('风险等级')}")
    for it in (data.get("items") or []):
        print(f"  {str(it.get('系统建议')):<10} {it.get('中文名') or it.get('饰品')}"
              f"  盈亏 {it.get('盈亏%')}%  位置 {it.get('位置')}  信心 {it.get('信心度')}")
    for w in (data.get("watchlist") or [])[:6]:
        print(f"  [关注] {str(w.get('系统建议')):<10} {w.get('饰品')}  评分 {w.get('评分')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
