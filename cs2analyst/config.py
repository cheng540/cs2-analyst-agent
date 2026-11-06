"""配置加载：YAML 配置 + 默认值合并，全流程参数集中管理。"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("cs2analyst.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认配置：用户 config.yaml 中缺失的键自动补齐
DEFAULTS: dict[str, Any] = {
    "steamdt": {
        "base_url": "https://open.steamdt.com",
        "api_key": "",
        "timeout": 20,
        "retry": 3,
        "kline_days": 200,
        "platform": "ALL",
        "special_style": "",
        "request_gap": 1.2,
    },
    "analysis": {
        "ma_windows": [5, 10, 20],
        "percentile_window_days": 180,
        "position_low": 30,
        "position_high": 70,
        "support_lookback_days": 60,
        "break_lookback_days": 20,       # 「有效跌破前支撑」的回看窗口
        "swing_window": 3,
        "support_tolerance": 0.02,
        "break_confirm_days": 2,
        "volume_expand_ratio": 1.3,
        "volume_shrink_ratio": 0.75,
        "stagnation_return": 0.01,
    },
    "decision": {
        "stop_loss_pct": 0.30,
        "hold_loss_pct": 0.15,
        "take_profit_pct": 0.20,
        "max_add_position_weight": 0.15,
        "add_ratio": 0.25,
        "heavy_weight_threshold": 0.30,
        "liquidity_low": 300,
        "liquidity_high": 1000,
        "snapshot_liq_high": 150,   # 快照模式流动性阈值：当日成交数 >=150 高
        "snapshot_liq_low": 40,     # 当日成交数 <40 低
        "allow_add_risk_levels": ["低风险", "中风险"],
    },
    "schedule": {
        "enabled": False,
        "hour": 17,
        "minute": 30,
        "run_missed": True,
        "timezone": "Asia/Shanghai",
    },
    # ---- 三类报告各自独立开关（客户可自己关掉不想要的）----
    # 用「扁平键 + HH:MM 字符串」而不是嵌套字典：
    #   1) 网页写配置只支持两级键（reports.daily_enabled），嵌套会写坏；
    #   2) 客户手改 config.yaml 时，"17:30" 比 hour/minute 两个键直观。
    "reports": {
        "daily_enabled": True,
        "daily_time": "17:30",
        "noon_enabled": True,
        "noon_time": "12:10",
        "weekly_enabled": True,
        "weekly_day": 6,             # 0=周一 … 6=周日
        "weekly_time": "20:00",
    },
    "paths": {
        "data_dir": "data",
        "report_dir": "reports",
        "log_dir": "logs",
        "inventory_file": "config/inventory.json",
    },
    "report": {
        "author": "CS2 持仓智能分析 Agent",
        "heading_font": "微软雅黑",
        "body_font": "微软雅黑",
    },
    "sim": {
        "enabled": True,               # 每日流水线是否运行模拟盘
        "initial_capital": 3000.0,     # 模拟资金（元）
        "fee_rate": 0.01,              # 卖出手续费（悠悠有品：出售收 1%，买入不收）
        "withdraw_fee_rate": 0.01,     # 钱包提现手续费（仅提现时发生；不提现继续买则不产生）
        "hold_days_min": 7,            # T+7：买入后最短持有天数（锁定期内不可卖出）
        "dual_track": True,            # 双轨对照：A 轨纯规则 / B 轨规则+AI 研判
        "min_cash_ratio": 0.15,        # 常态最低现金储备
        "high_risk_cash_ratio": 0.30,  # 大盘高风险时最低现金储备
        "max_position_pct": 25.0,      # 单票仓位上限（%总权益）
        "watchlist_file": "config/sim_watchlist.json",
        "state_file": "data/sim/state_a.json",
        "state_file_b": "data/sim/state_b.json",
    },
    "llm": {
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        # 显式钉住模型：不要用 deepseek-chat（已退役的别名，实际是 Flash 且不开思考）
        "model": "deepseek-flash",
        "timeout": 180,
        "max_tokens": 4000,
        "max_output_cap": 32000,   # 空正文/截断时自动加预算的上限
        "temperature": 0.4,
        # 分场景配置（profile）：不同用途用不同模型与思考模式，2026-09-24 实测后确定
        "profiles": {
            "judgement": {"model": "deepseek-flash", "thinking": True, "max_tokens": 12000},
            "chat": {"model": "deepseek-flash", "thinking": False, "max_tokens": 4000},
            "alert": {"model": "deepseek-flash", "thinking": False, "max_tokens": 1200},
            "review": {"model": "deepseek-v4-pro", "thinking": True, "max_tokens": 8000},
        },
    },
    "intel": {
        "enabled": True,
        "news_appid": 730,       # CS2（Steam 新闻 / 官方更新公告）
        "news_count": 12,
        "news_maxlen": 500,
        "anomaly_page_size": 20,
    },
    "notify": {
        "enabled": True,
        "channel": "wecom",      # 推送渠道：wecom（企业微信机器人）
        "wecom_webhook": "",
        "only_if_actions": False,
    },
    "chat": {
        "enabled": True,
        "max_questions_per_day": 50,   # 每日提问上限（防额度失控）
        "history_turns": 8,            # 携带的历史轮数
    },
    "alerts": {
        "enabled": True,
        # 巡检间隔（分钟）：客户可以自己在网页上改，强烈建议 >= 15。
        # SteamDT 批量取价接口是 1 次/分钟，间隔太短会直接吃 4005 报错。
        "interval_minutes": 60,
        "times": [0, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],  # 旧的固定时间点（兼容保留）
        "drop_pct_vs_last": 3.0,           # 较上次检查跌幅
        "drop_pct_vs_yesterday": 5.0,      # 较昨日跌幅
        "rise_pct_vs_last": 4.0,           # 较上次检查涨幅
        "rise_pct_vs_yesterday": 6.0,      # 较昨日涨幅
        "sell_count_change_pct": 30.0,     # 在售量骤变幅度
        "loss_alert_pct": 40.0,            # 亏损扩大到该比例提醒
        "profit_alert_pct": 20.0,          # 盈利达到该比例提醒
        "break_support": True,             # 跌破最近支撑位提醒
        "signal_alert": True,              # 系统给出「买入/加仓」建议时即时提醒
        # ---- 分级推送（巡检加密后防止告警疲劳）----
        "urgent_drop_pct": 4.0,            # 单次跌幅达此值 → 立即推送；3~4% 进 12/22 点汇总
        "urgent_rise_pct": 6.0,            # 单次涨幅达此值 → 立即推送
        "summary_times": [12, 22],         # 这两个整点的巡检额外发「盘中概览」
        "urgent_max_per_kind_per_day": 3,  # 跌/涨紧急消息每日上限，超出折叠进汇总
        "confirm_urgent": True,            # 跌/涨/破位需连续两次巡检确认（治单次跳变）
        "confirm_window_minutes": 90,      # 两次确认的有效间隔
        "confirm_tolerance_pct": 1.0,      # 确认时允许的价格收复幅度（%）
        "cooldown_hours": 24,              # 同标的同类信号的滚动去重窗口（小时）
        "fallback_max_items": 8,           # 批量接口失败时官网兜底最多抓几个
        "batch_retry_wait": 65,            # 批量接口失败后等待重试秒数（1 次/分钟限流）
        "intraday_keep_days": 180,         # 日内价格曲线保留天数
        "ai_commentary": True,             # 是否附带 AI 点评
        "max_items": 30,
        "watch_top_n": 5,                  # 观察池取最接近买点的前 N 个
        "outlier_pct": 60.0,               # 与上次价格偏差超此比例视为脏数据
        "interval_seconds": 1.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base 的同名键。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class AppConfig:
    """应用配置对象（点号访问，如 cfg.decision.stop_loss_pct）。"""

    steamdt: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    schedule: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    intel: dict = field(default_factory=dict)
    notify: dict = field(default_factory=dict)
    chat: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    config_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "config.yaml")

    def data_dir(self) -> Path:
        return (PROJECT_ROOT / self.paths["data_dir"]).resolve()

    def report_dir(self) -> Path:
        return (PROJECT_ROOT / self.paths["report_dir"]).resolve()

    def log_dir(self) -> Path:
        return (PROJECT_ROOT / self.paths["log_dir"]).resolve()

    def inventory_path(self) -> Path:
        return (PROJECT_ROOT / self.paths["inventory_file"]).resolve()

    def sim_state_path(self) -> Path:
        return (PROJECT_ROOT / self.sim.get("state_file", "data/sim/state_a.json")).resolve()

    def sim_state_path_b(self) -> Path:
        return (PROJECT_ROOT / self.sim.get("state_file_b", "data/sim/state_b.json")).resolve()

    def has_llm_key(self) -> bool:
        return bool(str(self.llm.get("api_key", "")).strip())

    def sim_watchlist_path(self) -> Path:
        return (PROJECT_ROOT / self.sim.get("watchlist_file", "config/sim_watchlist.json")).resolve()

    def has_api_key(self) -> bool:
        return bool(str(self.steamdt.get("api_key", "")).strip())


def _apply_env_overrides(merged: dict) -> dict:
    """环境变量覆盖敏感配置（便于开源 / 分享代码时不带密钥）。

    支持的变量（存在且非空时优先于 config.yaml）：
        STEAMDT_API_KEY     → steamdt.api_key
        DEEPSEEK_API_KEY    → llm.api_key
        WECOM_WEBHOOK       → notify.wecom_webhook
    """
    mapping = (
        ("STEAMDT_API_KEY", ("steamdt", "api_key")),
        ("DEEPSEEK_API_KEY", ("llm", "api_key")),
        ("WECOM_WEBHOOK", ("notify", "wecom_webhook")),
    )
    for env_name, (section, key) in mapping:
        val = os.environ.get(env_name, "").strip()
        if not val:
            continue
        merged.setdefault(section, {})[key] = val
        log.info("已用环境变量 %s 覆盖配置 %s.%s", env_name, section, key)
    return merged


def load_config(path: Path | str | None = None) -> AppConfig:
    """加载 YAML 配置并与默认值合并（环境变量优先）。"""
    cfg_path = Path(path) if path else (PROJECT_ROOT / "config" / "config.yaml")
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
            if isinstance(loaded, dict):
                raw = loaded
    merged = _apply_env_overrides(_deep_merge(DEFAULTS, raw))
    cfg = AppConfig(
        steamdt=merged["steamdt"],
        analysis=merged["analysis"],
        decision=merged["decision"],
        schedule=merged["schedule"],
        paths=merged["paths"],
        report=merged["report"],
        sim=merged["sim"],
        llm=merged["llm"],
        intel=merged["intel"],
        notify=merged["notify"],
        chat=merged["chat"],
        alerts=merged["alerts"],
        reports=merged["reports"],
        config_path=cfg_path,
    )
    # 目录自动创建
    for d in (cfg.data_dir(), cfg.report_dir(), cfg.log_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
