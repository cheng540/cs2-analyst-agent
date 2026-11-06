"""四类定时任务的具体执行体（日报 / 午报 / 周报 / 巡检）。

被两处复用：
    · `tools/run_scheduled.py`（cron 调用）
    · `cs2analyst/scheduler.py`（`main.py schedule` 常驻进程）
两处都只做「什么时候触发」，具体做什么在这里，避免两份实现跑偏。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT, AppConfig
from .logging_setup import get_logger

log = get_logger(__name__)

TOOLS_DIR = PROJECT_ROOT / "tools"


def run_daily(cfg: AppConfig, dry_run: bool = False) -> int:
    """日报：完整流水线（分析 → 决策 → AI 研判 → Word → 推送）。"""
    if dry_run:
        # 复用现成的不推送脚本（它会把 webhook 清空，绝不会发到手机上）
        return _run_script("gen_daily_once.py", [])
    from .pipeline import PipelineError, run_pipeline

    try:
        result = run_pipeline(cfg)
    except PipelineError as exc:
        log.error("[daily] 流水线失败：%s", exc)
        return 1
    log.info("[daily] 完成：%s", result.get("report_path"))
    print(f"日报已生成：{result.get('report_path')}")
    return 0


def run_alerts(cfg: AppConfig, dry_run: bool = False, summary: bool = False) -> int:
    """巡检。`summary=True` 时强制这一刻发「盘中概览 + 体检 Word」（即午报）。"""
    from .alerts.checker import AlertChecker

    if summary:
        # checker 靠 alerts.summary_times 判断当前整点要不要汇总，这里临时改成当前小时，
        # 其余逻辑（分级推送、AI 点评、Word 体检表）完全复用。
        cfg.alerts = dict(cfg.alerts, summary_times=[datetime.now().hour])

    run = AlertChecker(cfg).run(push=not dry_run)
    if run.error:
        log.error("[alerts] %s", run.error)
        print(f"巡检失败：{run.error}")
        return 1
    log.info("[alerts] 完成：检查 %d 个标的，模式=%s", run.checked, run.mode or "未推送")
    print(f"巡检完成：{run.checked} 个标的，推送类型={run.mode or '无'}")
    return 0


def run_weekly(cfg: AppConfig, dry_run: bool = False) -> int:
    """周报：双轨净值 / 持仓 / 已实现交易 / 运行健康。"""
    return _run_script("weekly_report.py", ["--dry-run"] if dry_run else [])


def _run_script(name: str, argv: list[str]) -> int:
    """调用 tools/ 下的单文件脚本（它们用 sys.argv + main() 的写法）。"""
    path = TOOLS_DIR / name
    if not path.exists():
        log.error("找不到脚本：%s", path)
        return 1
    import runpy

    old = sys.argv
    sys.argv = [name] + argv
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:          # 脚本用 sys.exit 返回码
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = old
    return 0


def run_task(cfg: AppConfig, task: str, dry_run: bool = False) -> int:
    """按名字执行（daily/noon/weekly/alerts），供调度器与 CLI 共用。"""
    if task == "daily":
        return run_daily(cfg, dry_run)
    if task == "noon":
        return run_alerts(cfg, dry_run=dry_run, summary=True)
    if task == "weekly":
        return run_weekly(cfg, dry_run)
    if task == "alerts":
        return run_alerts(cfg, dry_run=dry_run)
    raise ValueError(f"未知任务：{task}")
