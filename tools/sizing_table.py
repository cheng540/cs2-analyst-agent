"""按当前组合权益，算出每个标的在各级信心度下「能不能执行」。

为什么要它（2026-10-01 用户追问）：
    组合只有约 ¥1,800，而饰品单件常常 ¥60~¥700。按百分比算出的标准仓位
    （信心 2 → 3.6% ≈ ¥65）经常买不到一件。用户直接问：
    「那以后我都只能买 36 块钱的东西了？」
    这个表回答的就是：**哪些标的、在什么信号强度下真的能买（1 件起）**。

用法：
    python tools/sizing_table.py                # 用最新分析快照里的现价与组合权益
    python tools/sizing_table.py --equity 1800  # 手动指定权益（组合市值 + 钱包现金）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.engine.decision import _CONVICTION_WEIGHT  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=0.0, help="组合权益（默认取快照总市值）")
    ap.add_argument("--max-position", type=float, default=25.0, help="单标的上限%%")
    ap.add_argument("--risk", default="高风险", choices=["高风险", "中风险", "低风险"])
    args = ap.parse_args()

    cfg = load_config()
    files = sorted((cfg.data_dir() / "analysis").glob("*.json"))
    if not files:
        print("没有分析快照")
        return 1
    snap = json.loads(files[-1].read_text(encoding="utf-8"))
    equity = args.equity or float((snap.get("portfolio") or {}).get("总市值") or 0)
    if equity <= 0:
        print("权益未知，请用 --equity 指定")
        return 1

    risk_coef = {"高风险": 0.6, "中风险": 1.0, "低风险": 1.0}[args.risk]
    print(f"快照 {files[-1].name} | 权益 ¥{equity:,.2f} | 单标的上限 {args.max_position:g}%"
          f" = ¥{equity * args.max_position / 100:,.0f} | 大盘系数 ×{risk_coef}\n")

    print("各级信心度的标准仓位：")
    for c in (1, 2, 3, 4, 5):
        base = _CONVICTION_WEIGHT.get(c, 6.0) * risk_coef
        w = min(base, args.max_position)
        print(f"  信心 {c}：{w:5.1f}% ≈ ¥{equity * w / 100:>7,.0f}")
    print()

    print(f"{'饰品':<34}{'单价':>10}{'一件占%':>9}   首个可执行的信心底")
    print("-" * 82)
    rows = []
    for it in (snap.get("items") or []):
        price = float(it.get("现价") or 0)
        name = str(it.get("中文名") or it.get("饰品") or "")
        if price <= 0:
            continue
        unit_w = price / equity * 100
        first = None
        for c in (1, 2, 3, 4, 5):
            w = min(_CONVICTION_WEIGHT.get(c, 6.0) * risk_coef, args.max_position)
            if equity * w / 100 >= price:
                first = c
                break
        rows.append((unit_w, name, price, first))
    for unit_w, name, price, first in sorted(rows):
        tag = f"信心 {first}（≥¥{equity * min(_CONVICTION_WEIGHT.get(first, 6.0) * risk_coef, args.max_position) / 100:,.0f}）" \
            if first else "❌ 一件就超过单标的上限，不会建议买"
        print(f"{name[:33]:<34}¥{price:>9,.2f}{unit_w:>8.1f}%   {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
