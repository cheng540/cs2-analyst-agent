"""修正已实现账本里**手续费口径过期**的记录（费率变更/早期误记时用）。

背景：
    早期账本（银白石英、白骨）是按 **卖出 2.5%** 记的 —— 那是当时的未经验证假设。
    实测/用户确认后，悠悠有品真实口径是：**买入 0%、卖出 1%（进钱包）、提现另 1%**
    （合计约 1.99%）。已实现盈亏是"净到手 - 成本"，费率错则 pnl 也错。

它做什么：
    1. 读 data/real_trades.json；
    2. 逐条比对 fee_rate 与当前配置的 sim.fee_rate，不一致的按
       fee = round(gross × 新费率, 2) → net = gross - fee → pnl = net - cost 重算；
    3. 在 note 末尾追加一行修正说明（保留原有备注，不覆盖历史信息）；
    4. 默认只预览，加 --apply 才写入。

用法：
    python tools/fix_fee_rate.py            # 预览
    python tools/fix_fee_rate.py --apply    # 写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402

TRADES_REL = Path("real_trades.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = ap.parse_args()

    cfg = load_config()
    fee_rate = float(cfg.sim.get("fee_rate", 0.01))
    path = cfg.data_dir() / TRADES_REL
    if not path.exists():
        print(f"找不到账本：{path}")
        return 1

    trades = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    total_delta = 0.0
    for t in trades:
        old = float(t.get("fee_rate") if t.get("fee_rate") is not None else fee_rate)
        if abs(old - fee_rate) < 1e-9:
            continue
        gross = float(t.get("gross") or 0)
        cost = float(t.get("cost") or 0)
        fee = round(gross * fee_rate, 2)
        net = round(gross - fee, 2)
        pnl = round(net - cost, 2)
        pnl_pct = round((net / cost - 1) * 100, 2) if cost else 0.0
        print(f"{t.get('date')}  {t.get('name_cn') or t.get('name')}")
        print(f"   费率 {old*100:g}% → {fee_rate*100:g}%："
              f"手续费 ¥{t.get('fee')} → ¥{fee}；净到手 ¥{t.get('net')} → ¥{net}；"
              f"已实现 ¥{t.get('pnl'):+,.2f} → ¥{pnl:+,.2f}（{t.get('pnl_pct'):+.2f}% → {pnl_pct:+.2f}%）")
        total_delta += pnl - float(t.get("pnl") or 0)
        t["fee_rate"] = fee_rate
        t["fee"] = fee
        t["net"] = net
        t["pnl"] = pnl
        t["pnl_pct"] = pnl_pct
        extra = f"；手续费口径已按悠悠实际（卖出 {fee_rate*100:g}%）修正（原记 {old*100:g}%）"
        if "手续费口径已按" not in str(t.get("note") or ""):
            t["note"] = f"{t.get('note') or ''}{extra}".lstrip("；")
        changed += 1

    total = sum(float(x.get("pnl") or 0) for x in trades)
    print(f"\n需修正 {changed} 笔，合计已实现盈亏变化 ¥{total_delta:+,.2f}")
    print(f"修正后：{len(trades)} 笔，累计已实现 ¥{total:+,.2f}")
    if not changed:
        print("（账本已是最新口径，无需改动）")
        return 0
    if not args.apply:
        print("\n[预览] 未写入。确认无误后加 --apply")
        return 0
    path.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] 已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
