"""本地完整跑一遍日报流程（**不推送**），用于改完代码后确认不会把当晚的日报弄崩。

用法：
    python tools/gen_daily_once.py

注意：
- 会把 webhook 清空，所以不会打扰用户；报告写到本地 reports/；
- 会顺带刷新本地模拟盘与快照（服务器上的状态不受影响，之后 sync_from_server 会覆盖回来）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402
from cs2analyst.pipeline import run_pipeline  # noqa: E402


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir())
    cfg.notify["wecom_webhook"] = ""          # 只生成报告，绝不推送
    result = run_pipeline(cfg)
    p = result["portfolio"]
    print(f"[mode] {result['mode']}")
    print(f"[market] {result['market']['trend']} / {result['market']['risk']}")
    print(f"[portfolio] value={p['total_value']:.2f} pnl={p['total_pnl']:+.2f} "
          f"({p['pnl_pct']:+.2f}%)")
    print(f"[decisions] {len(result['decisions'])}")
    print(f"[report] {result['report_path']}")
    if result.get("missing"):
        print(f"[missing] {', '.join(result['missing'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
