"""看模拟盘 A/B 双轨的状态（现金、持仓、最近日志、成交），不调接口。

用法： python tools/peek_sim.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    for label, path in (("A轨（纯规则）", cfg.sim_state_path()),
                        ("B轨（规则+AI）", cfg.sim_state_path_b())):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"--- {label}: 读取失败 {exc}")
            continue
        cash = float(d.get("cash") or 0)
        pos = d.get("positions") or {}
        journal = d.get("journal") or []
        trades = d.get("trades") or d.get("history") or []
        init = float(d.get("initial_capital") or 0)
        net = float(journal[-1].get("net_value") or cash) if journal else cash
        print(f"--- {label}")
        print(f"    初始 ¥{init:,.0f} | 现金 ¥{cash:,.2f} | 净值 ¥{net:,.2f}"
              f"（{(net / init - 1) * 100:+.2f}%）| 日志 {len(journal)} 天 | 成交 {len(trades)} 笔")
        for name, p in pos.items():
            print(f"    持仓：{name} × {p.get('qty')}  成本 ¥{p.get('avg_cost')}"
                  f"  买入日 {p.get('buy_date')}  锁定至 {p.get('locked_until')}")
        for t in trades[-6:]:
            print(f"    成交：{t.get('date')} {t.get('action')} {t.get('name')}"
                  f" × {t.get('qty')} @ ¥{t.get('price')}")
        if journal:
            last = journal[-1]
            print(f"    最近一天 {last.get('date')}：净值 ¥{last.get('net_value')}")
            thoughts = str(last.get("thoughts") or "").strip()
            if thoughts:
                print(f"    手记：{thoughts[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
