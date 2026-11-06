"""大盘环境判断：趋势判定、风险等级、操作总基调。

支持两种数据源：
1. 开放平台大盘K线（日K/周K OHLC）—— analyze_market
2. 官网公开统计（指数/涨跌类型/真实成交额/情绪分）—— analyze_market_stats
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .indicators import (add_ma_columns, ma_alignment, series_momentum_ratio,
                         trend_judge, volume_proxy)


@dataclass
class MarketEnvironment:
    """大盘环境结论。"""
    trend: str = "震荡趋势"            # 上升趋势 / 震荡趋势 / 下降趋势
    risk: str = "中风险"               # 低风险 / 中风险 / 高风险
    base_tone: str = ""                # 操作总基调
    index_value: float | None = None   # 大盘指数
    diff_yesterday: float | None = None
    diff_ratio: float | None = None    # 较昨日涨跌幅（%）
    ma_state: str = ""                 # 日K均线排列
    volume_state: str = ""             # 量能状态
    volume_source: str = ""            # 量能数据来源说明
    sentiment_score: float | None = None   # 平台情绪分（仅参考，不参与决策）
    weekly_trend: str = ""             # 周K中期趋势
    rise_fall_type: str = ""           # 涨跌类型 UP/DOWN
    rise_fall_days: int = 0            # 连续涨跌天数
    # 大盘近5日涨跌幅（%）——「相对强度」的基准：单品近5日 − 大盘近5日 = 跑赢多少。
    # 加它的原因（2026-10-01 用户提的场景「单品破趋势独涨」）：原先评分只有单品自身趋势
    # 加一个全局大盘罚分，**独涨与随大流下跌受到同样的市场惩罚**，引擎识别不出"跑赢大盘"。
    ret_5d: float | None = None
    evidence: list[str] = field(default_factory=list)

    def block_add(self) -> bool:
        """大盘是否屏蔽补仓（高风险或下降趋势）。"""
        return self.risk == "高风险" or self.trend == "下降趋势"

    def enrich_scraper(self, summary: dict, txn: pd.DataFrame | None) -> None:
        """用官网公开统计补充：真实成交额/成交量（量能）、情绪分（参考）、涨跌类型。"""
        m = summary.get("marketIndexTrend") or {}
        self.rise_fall_type = str(m.get("riseFallType") or "")
        self.rise_fall_days = int(m.get("riseFallDays") or 0)
        perf = summary.get("transPerformanceTrend") or {}
        score = perf.get("performanceScore")
        if score is not None:
            self.sentiment_score = float(score)
            self.evidence.append(f"平台情绪分：{self.sentiment_score:.0f}（仅参考，不参与决策）")
        if txn is not None and not txn.empty:
            self._volume_from_transaction(txn)
        if self.rise_fall_type:
            self.evidence.append(
                f"指数涨跌类型：{'上涨' if self.rise_fall_type == 'UP' else '下跌'}，"
                f"已连续 {self.rise_fall_days} 天")

    def _volume_from_transaction(self, txn: pd.DataFrame) -> None:
        """基于官网真实成交额/成交量判断量能，覆盖振幅代理结论。"""
        try:
            amount = series_momentum_ratio(txn["turnover"])
            count = series_momentum_ratio(txn["volume"])
            if amount["ratio"] is not None:
                self.volume_state = amount["state"]
                self.volume_source = "官网真实成交额（近5日均/前20日均）"
                self.evidence.append(
                    f"真实量能：成交额动量比 {amount['ratio']:.2f}（{amount['state']}），"
                    f"成交量动量比 {count['ratio']:.2f}（{count['state']}）")
        except Exception as exc:
            self.evidence.append(f"成交额统计解析失败: {exc}")


def analyze_market(daily: pd.DataFrame, weekly: pd.DataFrame,
                   index: dict, ma_windows: list[int], vol_cfg: dict) -> MarketEnvironment:
    """综合日K、周K、大盘指数，输出环境结论。"""
    env = MarketEnvironment()
    idx_val = index.get("broadMarketIndex")
    env.index_value = float(idx_val) if idx_val is not None else None
    env.diff_yesterday = index.get("diffYesterday")
    env.diff_ratio = index.get("diffYesterdayRatio")

    # ---- 日K分析 ----
    if daily.empty or len(daily) < max(ma_windows) + 5:
        env.trend = "数据不足"
        env.risk = "中风险"
        env.base_tone = "大盘K线数据不足，默认按中风险谨慎操作"
        env.evidence.append("日K数据不足，无法判定趋势")
        return env

    d = add_ma_columns(daily, ma_windows)
    env.ma_state = ma_alignment(d, ma_windows)
    env.trend = trend_judge(d, ma_windows)
    close = d["close"]

    # ---- 大盘近5日涨跌（相对强度的基准；数据不足则留空，评分会跳过该因子）----
    if len(close) >= 6:
        try:
            c_prev, c_now = float(close.iloc[-6]), float(close.iloc[-1])
            if c_prev > 0:
                env.ret_5d = round((c_now / c_prev - 1) * 100, 2)
                env.evidence.append(f"大盘近5日 {env.ret_5d:+.2f}%")
        except (TypeError, ValueError):
            env.ret_5d = None

    # 量能（振幅代理）
    vol = volume_proxy(daily, vol_cfg.get("volume_expand_ratio", 1.3),
                       vol_cfg.get("volume_shrink_ratio", 0.75),
                       vol_cfg.get("stagnation_return", 0.01))
    env.volume_state = vol["state"]

    # ---- 周K中期方向 ----
    if not weekly.empty and len(weekly) >= max(ma_windows) + 3:
        w = add_ma_columns(weekly, ma_windows)
        env.weekly_trend = trend_judge(w, ma_windows)
        env.evidence.append(f"周K中期趋势：{env.weekly_trend}")

    # ---- 风险等级 ----
    ma5 = float(d[f"ma{ma_windows[0]}"].iloc[-1])
    cur = float(close.iloc[-1])
    diff_ratio = env.diff_ratio
    env.evidence.append(f"日K均线：{env.ma_state}；收盘 {cur:.2f}")
    if diff_ratio is not None:
        env.evidence.append(f"大盘指数较昨日 {diff_ratio:+.2f}%")

    if env.trend == "上升趋势":
        env.risk = "低风险" if cur >= ma5 else "中风险"
        if env.weekly_trend == "下降趋势":
            env.risk = "中风险"
            env.evidence.append("日K上升但周K仍弱，按中风险处理")
        elif cur < ma5:
            env.evidence.append("短期回调至MA5下方，注意节奏")
    elif env.trend == "下降趋势":
        env.risk = "高风险"
        env.evidence.append("日K空头格局，以风控为主")
    else:
        env.risk = "中风险"
        env.evidence.append("日K方向不明，谨慎对待")
        if diff_ratio is not None and diff_ratio <= -2.0:
            env.risk = "高风险"
            env.evidence.append(f"单日大跌 {diff_ratio:.2f}%，升级为高风险")

    # ---- 操作总基调 ----
    if env.risk == "低风险":
        env.base_tone = "上升周期、低风险环境：可按规则正常执行补仓与止盈操作"
    elif env.risk == "中风险":
        env.base_tone = "中风险环境：控制仓位、谨慎操作，补仓仅限强支撑企稳信号，止盈可适度提前"
    else:
        env.base_tone = ("下降周期 / 高风险：不补仓摊平成本（弱市加仓摊平是亏损扩大的主要原因）；"
                         "新开只接受强信号（信心 ≥3）的小仓参与，弱信号一律观望；"
                         "优先持仓风控，触发止损条件果断执行")
    env.evidence.append(f"量能（振幅代理）：{env.volume_state}")

    return env


def analyze_market_stats(summary: dict, txn: pd.DataFrame | None,
                         ma_windows: list[int], vol_cfg: dict) -> MarketEnvironment:
    """基于官网公开统计判断大盘环境（无需 API Key / 无需K线）。

    输入：
      summary: /index/trend/v1/summary 的 data（含 marketIndexTrend、
               transPerformanceTrend、onlinePlayerTrend）
      txn:     每日成交额/成交量 DataFrame（可选）
    输出与 analyze_market 一致的 MarketEnvironment。
    """
    env = MarketEnvironment()
    m = summary.get("marketIndexTrend") or {}
    env.index_value = float(m.get("broadMarketIndex")) if m.get("broadMarketIndex") is not None else None
    env.diff_yesterday = m.get("diffYesterday")
    env.diff_ratio = m.get("diffYesterdayRatio")
    env.rise_fall_type = str(m.get("riseFallType") or "")
    env.rise_fall_days = int(m.get("riseFallDays") or 0)
    env.volume_source = "官网真实成交额（近5日均/前20日均）"

    diff = env.diff_ratio if env.diff_ratio is not None else 0.0
    if env.index_value is not None:
        env.evidence.append(f"大盘指数 {env.index_value:.2f}（较昨日 {diff:+.2f}%）")

    # ---- 今日逐小时走势（短周期均线与斜率） ----
    hourly = m.get("historyMarketIndexList") or []
    short_dir = ""
    if isinstance(hourly, list) and len(hourly) >= 6:
        vals = [float(r[1]) for r in hourly if isinstance(r, (list, tuple)) and len(r) >= 2
                and r[1] not in (None, "")]
        if len(vals) >= 6:
            s = pd.Series(vals)
            last = s.iloc[-1]
            ma5 = s.tail(5).mean()
            slope5 = (s.iloc[-1] - s.iloc[-6]) / s.iloc[-6] if s.iloc[-6] else 0.0
            if slope5 > 0.001 and last >= ma5:
                short_dir = "日内偏强"
            elif slope5 < -0.001 and last <= ma5:
                short_dir = "日内偏弱"
            else:
                short_dir = "日内震荡"
            env.evidence.append(f"今日指数走势：{short_dir}（最新 {last:.2f}）")

    # ---- 情绪分（仅参考） ----
    perf = summary.get("transPerformanceTrend") or {}
    score = perf.get("performanceScore")
    if score is not None:
        env.sentiment_score = float(score)
        env.evidence.append(f"平台情绪分：{env.sentiment_score:.0f}（仅参考，不参与决策）")

    # ---- 真实量能 ----
    if txn is not None and not txn.empty:
        env._volume_from_transaction(txn)
    else:
        env.volume_state = "数据不足"

    # ---- 趋势判定（涨跌类型 + 日内走势 + 幅度） ----
    up = env.rise_fall_type == "UP"
    down = env.rise_fall_type == "DOWN"
    if down and env.rise_fall_days >= 1 and diff < 0:
        env.trend = "下降趋势"
        env.evidence.append(f"指数连续下跌 {env.rise_fall_days} 天，短期空头占优")
    elif up and diff >= 0:
        env.trend = "上升趋势"
        env.evidence.append(f"指数连续上涨 {env.rise_fall_days} 天，短期多头占优")
    else:
        env.trend = "震荡趋势"
        env.evidence.append("指数涨跌互现，方向不明")

    # 日内走势对趋势的修正
    if short_dir == "日内偏强" and env.trend == "下降趋势" and abs(diff) < 0.3:
        env.trend = "震荡趋势"
        env.evidence.append("日内止跌企稳，下降趋势暂缓")
    if short_dir == "日内偏弱" and env.trend == "上升趋势" and abs(diff) < 0.3:
        env.trend = "震荡趋势"
        env.evidence.append("日内走弱，上行动能不足")

    # ---- 风险等级 ----
    if env.trend == "下降趋势":
        env.risk = "高风险"
    elif env.trend == "上升趋势":
        env.risk = "低风险"
    else:
        env.risk = "中风险"
    if diff <= -1.5:
        env.risk = "高风险"
        env.evidence.append(f"单日跌幅 {diff:.2f}%，风险升级")

    # ---- 操作总基调 ----
    if env.risk == "低风险":
        env.base_tone = "上升周期、低风险环境：可按规则正常执行补仓与止盈操作"
    elif env.risk == "中风险":
        env.base_tone = "中风险环境：控制仓位、谨慎操作，补仓仅限强支撑企稳信号，止盈可适度提前"
    else:
        env.base_tone = ("下降周期 / 高风险：不补仓摊平成本（弱市加仓摊平是亏损扩大的主要原因）；"
                         "新开只接受强信号（信心 ≥3）的小仓参与，弱信号一律观望；"
                         "优先持仓风控，触发止损条件果断执行")

    return env
