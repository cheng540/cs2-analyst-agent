"""本地/服务器通用的「概览 Word」离线生成器（不推送，只落盘）。

为什么需要：`_send_summary_docx` 只在 12:00/22:00 的巡检里被触发，
要单独验证排版得能手动跑一遍。这个脚本清空 webhook 后走同一条代码路径。

用法（服务器上同样可用）：
    python tools/gen_summary_once.py
输出： reports/盘中概览_<日期>_<小时>时.docx
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.alerts.checker import AlertChecker, AlertRun  # noqa: E402
from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir())
    cfg.notify["wecom_webhook"] = ""          # 只生成文件，绝不推送
    c = AlertChecker(cfg)
    items = c.collect_items()
    prices = c.fetch_prices([i["name"] for i in items])
    alerts = c.evaluate(items, prices)
    run = AlertRun(time=f"{__import__('datetime').datetime.now():%Y-%m-%d %H:%M}",
                   checked=len(prices), expected=len(items))
    c._send_summary_docx(run, [], alerts)
    print(f"[rows] item_table={len(getattr(c, 'item_table', []) or [])} "
          f"alerts={len(alerts)} checked={len(prices)}/{len(items)}")
    # 顺便验证「AI 点评拿到的持仓状态」长什么样（这是用户 2026-09-30 提的那个问题）
    from cs2analyst.alerts.checker import Alert  # noqa: PLC0415
    for r in (getattr(c, "item_table", []) or [])[:3]:
        if not r.get("qty"):
            continue
        a = Alert(name=r["name"], name_cn=r.get("cn", ""), kind="-", detail="",
                  qty=r["qty"], cost=r["cost"], pnl_pct=r.get("pnl_pct"),
                  source=r.get("source", ""), unlocked=bool(r.get("unlocked")))
        print(f"[state] {a.name_cn}：{c._position_state(a)}")
    files = sorted(cfg.report_dir().glob("*.docx"))
    if files:
        print(f"[out] {files[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
