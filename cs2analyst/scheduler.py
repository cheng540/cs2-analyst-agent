"""本地常驻调度：按配置自动跑日报 / 午报 / 周报 / 盘中巡检。

与 cron 的分工：
    · 服务器上更稳的是 cron（`tools/install_cron.py` 生成，统一调 run_scheduled.py）；
    · 客户电脑上不想折腾 crontab，直接 `python main.py schedule` 常驻即可。
两者共用 `cs2analyst/policy.py` 判开关、`cs2analyst/tasks.py` 干活，行为一致。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import AppConfig
from .logging_setup import get_logger
from .policy import (
    KIND_LABELS, alerts_enabled, alerts_interval_minutes, describe_plan, report,
)
from .tasks import run_task

log = get_logger(__name__)

TASK_LABELS = {"daily": "日报", "noon": "午报", "weekly": "周报", "alerts": "盘中巡检"}
# 巡检只在白天到午夜之间跑：夜里没有成交量，白跑还消耗接口额度
ALERT_ACTIVE_HOURS = range(9, 24)


def _retry(cfg: AppConfig, task: str, attempts: int = 2) -> Callable[[], None]:
    """任务包装：异常记日志并重试一次（网络抖动很常见）。"""

    def job() -> None:
        for attempt in range(1, attempts + 1):
            try:
                run_task(cfg, task)
                return
            except Exception as exc:            # noqa: BLE001 - 调度线程不能死
                log.error("%s第 %d/%d 次执行失败：%s", TASK_LABELS[task], attempt, attempts, exc)
                if attempt < attempts:
                    log.info("60 秒后重试…")
                    time.sleep(60)
        log.error("%s重试耗尽，等下一个周期", TASK_LABELS[task])

    return job


def _alerts_job(cfg: AppConfig) -> Callable[[], None]:
    inner = _retry(cfg, "alerts", attempts=1)

    def job() -> None:
        if datetime.now().hour not in ALERT_ACTIVE_HOURS:
            log.debug("非巡检时段，跳过")
            return
        inner()

    return job


def build_scheduler(cfg: AppConfig) -> BackgroundScheduler:
    """按配置构建调度器：关掉的报告不注册任务。"""
    tz = cfg.schedule.get("timezone", "Asia/Shanghai")
    sched = BackgroundScheduler(timezone=tz)
    grace = 3600 if cfg.schedule.get("run_missed", True) else 60

    for kind in ("daily", "noon", "weekly"):
        conf = report(cfg, kind)
        if not conf["enabled"]:
            log.info("%s 已关闭，不注册调度", KIND_LABELS[kind])
            continue
        if kind == "weekly":
            trigger = CronTrigger(day_of_week=conf["weekday"], hour=conf["hour"],
                                  minute=conf["minute"], timezone=tz)
        else:
            trigger = CronTrigger(hour=conf["hour"], minute=conf["minute"], timezone=tz)
        sched.add_job(
            _retry(cfg, kind), trigger, id=kind, name=KIND_LABELS[kind],
            misfire_grace_time=grace, coalesce=True, max_instances=1,
        )

    if alerts_enabled(cfg):
        minutes = alerts_interval_minutes(cfg)
        sched.add_job(
            _alerts_job(cfg), IntervalTrigger(minutes=minutes), id="alerts", name="盘中巡检",
            misfire_grace_time=300, coalesce=True, max_instances=1,
        )
    else:
        log.info("盘中巡检已关闭，不注册调度")

    return sched


def run_scheduler(cfg: AppConfig) -> None:
    """启动调度循环（阻塞）。Ctrl+C 退出。"""
    log.info("当前计划：")
    for line in describe_plan(cfg):
        log.info("  %s", line)

    sched = build_scheduler(cfg)
    if not sched.get_jobs():
        log.warning("所有任务都已关闭：没有任何调度。改 config.yaml 的 reports / alerts 后再启动。")
        return

    sched.start()
    for job in sched.get_jobs():
        log.info("已注册：%s → 下次 %s", job.name, job.next_run_time)
    log.info("调度已启动（时区 %s）。按 Ctrl+C 退出。", cfg.schedule.get("timezone", "Asia/Shanghai"))
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号，调度器停止")
        sched.shutdown(wait=False)
