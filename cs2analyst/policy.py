"""运行策略：三类报告的开关与巡检间隔，全项目唯一的判断点。

为什么单独抽一层：
    「日报/午报/周报要不要跑」「几点跑」「巡检多久一次」原来散落在
    三份 cron、两个 sh 脚本、一个 APScheduler 里，客户改一次要动四处。
    现在只有这里说了算，cron / 本地调度 / 手动命令都来问它。

配置形状（config.yaml）：
    reports:
      daily:  {enabled: true, hour: 17, minute: 30}
      noon:   {enabled: true, hour: 12, minute: 10}
      weekly: {enabled: true, weekday: 6, hour: 20, minute: 0}   # 0=周一 … 6=周日
    alerts:
      enabled: true
      interval_minutes: 60
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import AppConfig

KINDS = ("daily", "noon", "weekly")
KIND_LABELS = {"daily": "日报", "noon": "午报", "weekly": "周报"}
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 巡检间隔下限：SteamDT 批量取价 1 次/分钟，太密只会一直吃 4005
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 24 * 60


def clamp_interval(minutes: Any) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return 60
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, value))


def parse_hhmm(text: str, default_hour: int, default_minute: int) -> tuple[int, int]:
    """`"17:30"` → (17, 30)。格式不对就用默认值，不抛异常（配置写错不该让程序起不来）。"""
    raw = str(text or "").strip().replace("：", ":")
    if ":" in raw:
        head, _, tail = raw.partition(":")
        try:
            hour, minute = int(head), int(tail)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except ValueError:
            pass
    return default_hour, default_minute


def report(cfg: AppConfig, kind: str) -> dict:
    """取某个报告的生效配置（扁平键优先，也兼容旧的嵌套写法）。"""
    raw = cfg.reports or {}
    nested = raw.get(kind)
    nested = nested if isinstance(nested, dict) else {}

    if kind == "daily":
        enabled = nested.get("enabled", raw.get("daily_enabled", True))
        if "hour" in nested:
            hour, minute = int(nested["hour"]), int(nested.get("minute", 30))
        else:
            hour, minute = parse_hhmm(raw.get("daily_time", ""),
                                      int(cfg.schedule.get("hour", 17)),
                                      int(cfg.schedule.get("minute", 30)))
        return {"enabled": bool(enabled), "hour": hour, "minute": minute}

    if kind == "noon":
        enabled = nested.get("enabled", raw.get("noon_enabled", True))
        hour, minute = (int(nested["hour"]), int(nested.get("minute", 10))) if "hour" in nested \
            else parse_hhmm(raw.get("noon_time", ""), 12, 10)
        return {"enabled": bool(enabled), "hour": hour, "minute": minute}

    if kind == "weekly":
        enabled = nested.get("enabled", raw.get("weekly_enabled", True))
        if "hour" in nested:
            weekday = int(nested.get("weekday", 6))
            hour, minute = int(nested["hour"]), int(nested.get("minute", 0))
        else:
            weekday = int(raw.get("weekly_day", 6))
            hour, minute = parse_hhmm(raw.get("weekly_time", ""), 20, 0)
        return {"enabled": bool(enabled), "weekday": weekday % 7, "hour": hour, "minute": minute}

    raise ValueError(f"未知报告类型：{kind}")


def report_enabled(cfg: AppConfig, kind: str) -> bool:
    return report(cfg, kind)["enabled"]


def alerts_enabled(cfg: AppConfig) -> bool:
    return bool((cfg.alerts or {}).get("enabled", True))


def alerts_interval_minutes(cfg: AppConfig) -> int:
    return clamp_interval((cfg.alerts or {}).get("interval_minutes", 60))


def should_run(cfg: AppConfig, task: str) -> tuple[bool, str]:
    """`task` ∈ daily / noon / weekly / alerts。返回 (是否该跑, 原因)。

    定时脚本用它决定跳过：关掉的功能不执行，也不推送、不占用接口额度。
    """
    if task == "alerts":
        if not alerts_enabled(cfg):
            return False, "盘中巡检已在配置里关闭（alerts.enabled=false）"
        return True, f"巡检开启，间隔 {alerts_interval_minutes(cfg)} 分钟"
    if task not in KINDS:
        return False, f"未知任务：{task}"
    if not report_enabled(cfg, task):
        return False, f"{KIND_LABELS[task]}已在配置里关闭"
    return True, f"{KIND_LABELS[task]}开启"


def describe_plan(cfg: AppConfig) -> list[str]:
    """一行一条的人话描述，给日志、网页「设置」页和自检脚本用。"""
    lines = []
    for kind in KINDS:
        conf = report(cfg, kind)
        if kind == "weekly":
            when = f"每{WEEKDAY_CN[conf['weekday']]} {conf['hour']:02d}:{conf['minute']:02d}"
        else:
            when = f"每天 {conf['hour']:02d}:{conf['minute']:02d}"
        lines.append(f"{KIND_LABELS[kind]}：{'开' if conf['enabled'] else '关'}（{when}）")
    interval = alerts_interval_minutes(cfg)
    if alerts_enabled(cfg):
        lines.append(f"盘中巡检：开（每 {interval} 分钟）")
    else:
        lines.append("盘中巡检：关")
    return lines


def next_run_hint(cfg: AppConfig, kind: str, now: datetime | None = None) -> str:
    """下次触发的粗略提示（只看时分，不处理跨日细节）。"""
    now = now or datetime.now()
    conf = report(cfg, kind)
    if not conf["enabled"]:
        return "已关闭"
    if kind == "weekly":
        days = (conf["weekday"] - now.weekday()) % 7
        if days == 0 and (now.hour, now.minute) >= (conf["hour"], conf["minute"]):
            days = 7
        return f"{days} 天后 {conf['hour']:02d}:{conf['minute']:02d}"
    target = now.replace(hour=conf["hour"], minute=conf["minute"], second=0, microsecond=0)
    if target <= now:
        return f"明天 {conf['hour']:02d}:{conf['minute']:02d}"
    return f"今天 {conf['hour']:02d}:{conf['minute']:02d}"
