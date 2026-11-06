"""单品深度分析：把持仓 + K线 + 价格快照汇总为结构化分析结论。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..datasource.inventory import Holding
from .indicators import (add_ma_columns, effective_break, ma_alignment,
                         position_percentile, support_resistance, trend_judge,
                         volume_proxy)


@dataclass
class ItemAnalysis:
    """单品分析结果。"""
    holding: Holding
    data_ok: bool = True
    missing_reason: str = ""
    snapshot_mode: bool = False       # 官网快照模式（无历史K线，部分指标不可用）
    manual_price_based: bool = False  # 手动估值模式（贴纸等无API行情品类）
    # 行情
    cur_price: float | None = None
    avg7_price: float | None = None
    kline_len: int = 0
    # 盈亏
    total_cost: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    weight: float = 0.0              # 占组合市值比例（由 pipeline 回填）
    # 技术面
    trend: str = "数据不足"
    ma_state: str = "数据不足"
    ma_values: dict[str, float | None] = field(default_factory=dict)
    position_pct: float | None = None
    position_label: str = "数据不足"
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    strong_support: float | None = None
    broken_support: float | None = None
    near_support: bool = False
    near_resistance: bool = False
    # 流动性
    liquidity_grade: str = "数据不足"     # 高 / 中 / 低
    sell_count: int = 0
    bid_count: int = 0
    liquidity_trend: str = ""            # 存量近14日变化描述
    # 快照模式附加字段（官网公开数据）
    survive_num: int | None = None       # 存世量
    holders_num: int | None = None       # 持有人数
    turnover_rate: float | None = None   # 换手率(%)
    transaction_count: int | None = None # 当日成交数
    cash_ratio: float | None = None      # 求购/在售比
    price_rows: list[dict] = field(default_factory=list)  # 各平台价格
    # 量价
    volume_state: str = "数据不足"
    amp_ratio: float | None = None
    ret_5d: float | None = None
    ret_3d: float | None = None
    ret_30d: float | None = None
    ret_6m: float | None = None
    stagnation: bool = False
    vol_up_down: bool = False
    vol_up_stall: bool = False
    shrink_stop_falling: bool = False
    break_support: bool = False
    proxy_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.holding.market_hash_name,
            "wear": self.holding.wear,
            "quantity": self.holding.quantity,
            "buy_price": self.holding.buy_price,
            "buy_date": self.holding.buy_date,
            # 用户确认「已过 T+7 锁定期」（buy_date 缺失时的兜底）→ 让 AI 研判别说"锁定期未知"
            "unlocked": bool(getattr(self.holding, "unlocked", False)),
            "cur_price": self.cur_price,
            "avg7_price": self.avg7_price,
            "total_cost": round(self.total_cost, 2),
            "market_value": round(self.market_value, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct * 100, 2),
            "weight": round(self.weight * 100, 2),
            "trend": self.trend,
            "ma_state": self.ma_state,
            "position_pct": self.position_pct,
            "position_label": self.position_label,
            "supports": self.supports,
            "resistances": self.resistances,
            "nearest_support": self.nearest_support,
            "nearest_resistance": self.nearest_resistance,
            "strong_support": self.strong_support,
            "broken_support": self.broken_support,
            "liquidity_grade": self.liquidity_grade,
            "sell_count": self.sell_count,
            "bid_count": self.bid_count,
            "volume_state": self.volume_state,
            "amp_ratio": self.amp_ratio,
            "ret_5d": self.ret_5d,
            "ret_3d": self.ret_3d,
            "data_ok": self.data_ok,
            "missing_reason": self.missing_reason,
            "snapshot_mode": self.snapshot_mode,
        }


def grade_liquidity(sell_count: int, bid_count: int, low: int, high: int) -> str:
    """流动性等级：基于在售+求购总量。"""
    total = sell_count + bid_count
    if total >= high:
        return "高"
    if total >= low:
        return "中"
    return "低"


def _pick_current_price(price_rows: list[dict], avg7: dict | None,
                        fallback_close: float | None) -> tuple[float | None, float | None]:
    """选择当前市场价：优先最低在售价（变现视角，最保守），
    其次7天全平台均价，最后K线收盘。avg7 作为参考价单独返回。"""
    avg = None
    if avg7 and avg7.get("avgPrice"):
        avg = float(avg7["avgPrice"])
    sells = [float(r["sellPrice"]) for r in price_rows
             if r and r.get("sellPrice") and float(r["sellPrice"]) > 0]
    min_sell = min(sells) if sells else None
    cur = min_sell or avg or fallback_close
    return cur, avg


def _manual_valuation(result: ItemAnalysis, holding: Holding, reason: str) -> ItemAnalysis:
    """无行情数据品类（贴纸等）：按 inventory.json 中的 manual_price 估值，
    技术面指标全部标注不可用，仍参与组合盈亏核算。"""
    result.manual_price_based = True
    result.cur_price = float(holding.manual_price)
    qty = holding.quantity
    result.total_cost = round(holding.buy_price * qty, 2)
    result.market_value = round(result.cur_price * qty, 2)
    result.pnl = round(result.market_value - result.total_cost, 2)
    if holding.buy_price > 0:
        result.pnl_pct = result.cur_price / holding.buy_price - 1
    result.trend = "无行情数据"
    result.ma_state = "无行情数据"
    result.position_label = "无行情数据"
    result.liquidity_grade = "数据不足"
    result.volume_state = "数据不足"
    result.missing_reason = reason
    return result


def analyze_item(holding: Holding, kline: pd.DataFrame,
                 price_rows: list[dict], avg7: dict | None,
                 liquidity_history: list[dict] | None,
                 cfg_analysis: dict, cfg_decision: dict,
                 extra: dict | None = None) -> ItemAnalysis:
    """对单个持仓做完整技术分析。任何异常都返回 data_ok=False 而不抛出。

    extra: 可选补充数据（官网快照兜底时传入），支持键：
           transaction_count（当日成交数）、survive_num（存世量）
    """
    result = ItemAnalysis(holding=holding)
    try:
        if kline is None or kline.empty or len(kline) < 20:
            if holding.manual_price and holding.manual_price > 0:
                return _manual_valuation(
                    result, holding,
                    "该品类无 API 行情数据（如贴纸类），按手动估值计算，无技术分析")
            result.data_ok = False
            result.missing_reason = f"K线数据不足（{len(kline) if kline is not None else 0} 根）"
            return result

        result.kline_len = len(kline)
        windows = cfg_analysis["ma_windows"]
        df = add_ma_columns(kline, windows)
        close = df["close"]

        # ---- 当前价格与盈亏 ----
        result.cur_price, result.avg7_price = _pick_current_price(
            price_rows, avg7, float(close.iloc[-1]))
        if result.cur_price is None:
            result.cur_price = float(close.iloc[-1])
        qty = holding.quantity
        result.total_cost = round(holding.buy_price * qty, 2)
        result.market_value = round(result.cur_price * qty, 2)
        result.pnl = round(result.market_value - result.total_cost, 2)
        if holding.buy_price > 0:
            result.pnl_pct = (result.cur_price / holding.buy_price - 1)

        # ---- 趋势 / 均线 ----
        result.ma_state = ma_alignment(df, windows)
        result.trend = trend_judge(df, windows)
        last = df.iloc[-1]
        result.ma_values = {f"MA{w}": (round(float(last[f'ma{w}']), 4)
                                       if not pd.isna(last[f"ma{w}"]) else None)
                            for w in windows}

        # ---- 位置百分位 ----
        result.position_pct, result.position_label = position_percentile(
            close, cfg_analysis["percentile_window_days"],
            cfg_analysis["position_low"], cfg_analysis["position_high"])

        # ---- 支撑压力 ----
        sr = support_resistance(df, cfg_analysis["support_lookback_days"],
                                cfg_analysis["swing_window"],
                                cfg_analysis["support_tolerance"],
                                cfg_analysis.get("break_lookback_days", 20))
        result.supports = sr["supports"]
        result.resistances = sr["resistances"]
        result.nearest_support = sr["nearest_support"]
        result.nearest_resistance = sr["nearest_resistance"]
        result.strong_support = sr["strong_support"]
        result.broken_support = sr["broken_support"]

        cur = result.cur_price
        tol = cfg_analysis["support_tolerance"]
        if result.nearest_support is not None:
            result.near_support = cur <= result.nearest_support * (1 + tol)
        if result.nearest_resistance is not None:
            result.near_resistance = cur >= result.nearest_resistance * (1 - tol)

        # ---- 有效跌破（跌破现价上方的近期前支撑带） ----
        result.break_support = effective_break(
            close, result.broken_support, cfg_analysis["support_tolerance"],
            cfg_analysis["break_confirm_days"])

        # ---- 量价 ----
        vol = volume_proxy(kline, cfg_analysis["volume_expand_ratio"],
                           cfg_analysis["volume_shrink_ratio"],
                           cfg_analysis["stagnation_return"])
        result.volume_state = vol["state"]
        result.amp_ratio = vol["amp_ratio"]
        result.ret_5d = vol["ret_5d"]
        result.ret_3d = vol["ret_3d"]
        result.stagnation = vol["stagnation"]
        result.vol_up_down = vol["vol_up_down"]
        result.vol_up_stall = vol["vol_up_stall"]
        result.shrink_stop_falling = vol["shrink_stop_falling"]
        result.proxy_note = vol["proxy_note"]

        # ---- 流动性 ----
        sells = [int(r.get("sellCount") or 0) for r in price_rows]
        bids = [int(r.get("biddingCount") or 0) for r in price_rows]
        if sum(sells) + sum(bids) > 0:
            result.sell_count = sum(sells)
            result.bid_count = sum(bids)
            result.liquidity_grade = grade_liquidity(
                result.sell_count, result.bid_count,
                cfg_decision["liquidity_low"], cfg_decision["liquidity_high"])
            result.liquidity_trend = _liquidity_trend(
                holding.market_hash_name, result.sell_count, liquidity_history)
        elif extra and extra.get("transaction_count") is not None:
            # 挂单量缺失 → 用官网快照的当日成交数分级（真实成交数据）
            tc = int(extra["transaction_count"])
            result.transaction_count = tc
            result.survive_num = _to_int(extra.get("survive_num"))
            liq_high = cfg_decision.get("snapshot_liq_high", 150)
            liq_low = cfg_decision.get("snapshot_liq_low", 40)
            result.liquidity_grade = "高" if tc >= liq_high else ("中" if tc >= liq_low else "低")
            result.liquidity_trend = "按当日成交数评估（挂单量接口不可用）"
        else:
            result.liquidity_grade = "数据不足"  # 无数据不惩罚
    except Exception as exc:  # 单品异常不影响整体
        result.data_ok = False
        result.missing_reason = f"分析异常: {exc}"
    return result


def _liquidity_trend(name: str, current_sell: int, history: list[dict] | None) -> str:
    """基于历史快照判断在售量变化趋势（存量上升≈挂单增多/出货意愿增强）。"""
    if not history:
        return ""
    series = []
    for snap in history:
        rows = snap.get(name)
        if rows:
            total = sum(int(r.get("sellCount") or 0) for r in rows)
            if total > 0:
                series.append(total)
    if len(series) < 2 or current_sell <= 0:
        return ""
    avg_prev = sum(series[:-1]) / len(series[:-1])
    change = (current_sell - avg_prev) / avg_prev
    if change >= 0.3:
        return "在售量明显上升"
    if change <= -0.3:
        return "在售量明显下降"
    return "在售量平稳"


# ------------------------------------------------------------
# 官网快照模式分析（无需 API Key：单品详情页 SSR 数据）
# ------------------------------------------------------------

def _to_int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def analyze_item_snapshot(detail: dict, holding: Holding,
                          cfg_analysis: dict, cfg_decision: dict) -> ItemAnalysis:
    """官网快照模式单品分析。

    数据来源：单品详情页 SSR 载荷（真实在售价/存世量/成交量/量比/各周期涨跌）。
    无历史K线：均线、支撑压力、位置百分位不可用，对应字段以「快照模式」标注；
    趋势由 7日/30日/6个月 涨跌幅判定，量能使用官网「量比」（真实成交量比值）。
    """
    result = ItemAnalysis(holding=holding, snapshot_mode=True)
    try:
        price_rows_raw = detail.get("sellingPriceList") or []
        if not isinstance(price_rows_raw, list) or not price_rows_raw:
            result.data_ok = False
            result.missing_reason = "快照数据缺少价格列表"
            return result
        # 防御性清洗：仅保留「字典结构 + 正数价格」的有效平台行，按价格升序
        clean_rows: list[dict] = []
        for r in price_rows_raw:
            if isinstance(r, dict):
                p = _to_float(r.get("price"))
                if p and p > 0:
                    clean_rows.append({k: r.get(k) for k in ("platform", "platformName",
                                                             "price", "updateTime", "link")})
        if not clean_rows:
            result.data_ok = False
            result.missing_reason = "快照数据价格解析失败"
            return result
        # 异常值过滤：个别平台会返回占位价（如 0.1），用中位数相对阈值剔除
        vals_sorted = sorted(_to_float(r.get("price")) or 0 for r in clean_rows)
        median = vals_sorted[len(vals_sorted) // 2]
        floor = median * 0.3 if median > 0 else 0.0
        clean_rows = [r for r in clean_rows if (_to_float(r.get("price")) or 0) >= floor]
        clean_rows.sort(key=lambda x: _to_float(x.get("price")) or 0)
        if not clean_rows:
            result.data_ok = False
            result.missing_reason = "快照数据价格解析失败"
            return result
        result.price_rows = clean_rows
        prices = [_to_float(r.get("price")) for r in clean_rows]
        prices = [p for p in prices if p and p > 0]
        if not prices:
            result.data_ok = False
            result.missing_reason = "快照数据价格解析失败"
            return result
        result.cur_price = round(min(prices), 2)

        # ---- 盈亏 ----
        qty = holding.quantity
        result.total_cost = round(holding.buy_price * qty, 2)
        result.market_value = round(result.cur_price * qty, 2)
        result.pnl = round(result.market_value - result.total_cost, 2)
        if holding.buy_price > 0:
            result.pnl_pct = result.cur_price / holding.buy_price - 1

        # ---- 各周期涨跌（真实成交价变化） ----
        result.ret_3d = _to_float(detail.get("diff1Day"))
        result.ret_5d = _to_float(detail.get("diff7Day"))
        result.ret_30d = _to_float(detail.get("diff30Day"))
        result.ret_6m = _to_float(detail.get("diff6Month"))

        # ---- 趋势判定 ----
        d30 = result.ret_30d if result.ret_30d is not None else 0.0
        d6m = result.ret_6m if result.ret_6m is not None else 0.0
        if d6m > 5 and d30 > 0:
            result.trend = "上升趋势"
        elif d6m < -5 and d30 < 0:
            result.trend = "下降趋势"
        else:
            result.trend = "震荡趋势"
        result.ma_state = "快照模式（无K线）"
        result.position_label = "快照模式（无历史区间）"

        # ---- 量能：官网量比（真实成交量比值） ----
        vol_ratio = _to_float(detail.get("volumeRatio"))
        result.amp_ratio = vol_ratio
        if vol_ratio is None:
            result.volume_state = "数据不足"
        else:
            expand = cfg_analysis["volume_expand_ratio"]
            shrink = cfg_analysis["volume_shrink_ratio"]
            if vol_ratio >= expand:
                result.volume_state = "放量"
            elif vol_ratio <= shrink:
                result.volume_state = "缩量"
            else:
                result.volume_state = "平量"
            d7 = result.ret_5d if result.ret_5d is not None else 0.0
            d1 = result.ret_3d if result.ret_3d is not None else 0.0
            result.stagnation = abs(d7) <= cfg_analysis["stagnation_return"]
            result.vol_up_down = vol_ratio >= expand and d7 < -cfg_analysis["stagnation_return"]
            result.vol_up_stall = vol_ratio >= expand and abs(d7) <= cfg_analysis["stagnation_return"]
            result.shrink_stop_falling = vol_ratio <= shrink and d1 >= 0
            result.proxy_note = "量能来源：官网「量比」（今日成交量/近5日均量，真实成交数据）"

        # ---- 流动性：当日成交数 + 存世量/换手率佐证 ----
        result.transaction_count = _to_int(detail.get("transactionCount"))
        result.survive_num = _to_int(detail.get("surviveNum"))
        result.holders_num = _to_int(detail.get("holdersNum"))
        result.turnover_rate = _to_float(detail.get("turnoverRate"))
        result.cash_ratio = _to_float(detail.get("cashRatio"))
        tc = result.transaction_count if result.transaction_count is not None else 0
        liq_high = cfg_decision.get("snapshot_liq_high", 150)
        liq_low = cfg_decision.get("snapshot_liq_low", 40)
        if tc >= liq_high:
            result.liquidity_grade = "高"
        elif tc >= liq_low:
            result.liquidity_grade = "中"
        else:
            result.liquidity_grade = "低"
        result.sell_count = result.survive_num if result.survive_num is not None else 0
        result.bid_count = 0

        # ---- 支撑压力/破位：快照模式不可用 ----
        result.break_support = False
        result.supports = []
        result.resistances = []
        result.near_support = False
        result.near_resistance = False
    except Exception as exc:
        result.data_ok = False
        result.missing_reason = f"快照解析异常: {exc}"
    return result
