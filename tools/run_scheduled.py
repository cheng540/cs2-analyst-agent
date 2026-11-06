#!/usr/bin/env python3
"""定时任务统一入口：先看配置开关，再决定跑不跑。

为什么要有它：
    客户可能只要日报、不要午报；或者关掉巡检只留周报。
    以前四个 cron 行各自写死调用一个脚本，想关掉只能去删 crontab。
    现在 cron 统一调用本脚本，开关在 config.yaml / 网页「设置」里改，脚本自己跳过。

用法：
    python tools/run_scheduled.py --task daily          # 日报（完整流水线 + 推送）
    python tools/run_scheduled.py --task noon           # 午报（盘中概览 + 体检 Word）
    python tools/run_scheduled.py --task weekly         # 周报
    python tools/run_scheduled.py --task alerts         # 盘中巡检
    python tools/run_scheduled.py --task daily --dry-run    # 空跑：生成但不推送
    python tools/run_scheduled.py --plan                # 只看当前生效的计划

退出码：0=成功或按配置跳过；1=执行失败。
cron 生成见 tools/install_cron.py。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import get_logger, setup_logging  # noqa: E402
from cs2analyst.policy import describe_plan, should_run  # noqa: E402
from cs2analyst.tasks import run_task  # noqa: E402

log = get_logger("run_scheduled")


def main() -> int:
    ap = argparse.ArgumentParser(description="按配置执行定时任务（开关在 config.yaml）")
    ap.add_argument("--task", choices=["daily", "noon", "weekly", "alerts"], help="要执行的任务")
    ap.add_argument("--dry-run", action="store_true", help="空跑：生成但不推送")
    ap.add_argument("--plan", action="store_true", help="只打印当前生效的计划")
    ap.add_argument("--force", action="store_true", help="忽略开关强制执行（排查用）")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir())

    if args.plan or not args.task:
        print("当前生效的报告与巡检计划：")
        for line in describe_plan(cfg):
            print("  " + line)
        if not args.task:
            print("\n用 --task daily|noon|weekly|alerts 指定要跑哪一个。")
        return 0

    ok, reason = should_run(cfg, args.task)
    if not ok and not args.force:
        log.info("[%s] 跳过：%s", args.task, reason)
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] {args.task} 跳过：{reason}")
        return 0
    if not ok:
        log.warning("[%s] 配置里已关闭，但用了 --force，仍然执行", args.task)

    return run_task(cfg, args.task, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
