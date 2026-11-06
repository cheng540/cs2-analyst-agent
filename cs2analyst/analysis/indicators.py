"""技术指标与形态识别（纯函数，输入 pandas DataFrame）。"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ------------------------------------------------------------
# 均线系统
# ------------------------------------------------------------

def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def add_ma_columns(df: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    """为K线 DataFrame 增加 MA 列。"""
    out = df.copy()
    for w in windows:
        out[f"ma{w}"] = sma(out["close"], w)
    return out


def ma_alignment(df: pd.DataFrame, windows: list[int]) -> str:
    """多空排列判断：
    - 多头排列: MA5 > MA10 > MA20 且价格在 MA5 上方
    - 空头排列: MA5 < MA10 < MA20 且价格在 MA5 下方
    - 交叉缠绕: 其他
    """
    if len(df) < max(windows):
        return "数据不足"
    last = df.iloc[-1]
    vals = {w: last.get(f"ma{w}") for w in windows}
    if any(pd.isna(v) for v in vals.values()):
        return "数据不足"
    close = last["close"]
    ordered_up = vals[windows[0]] > vals[windows[1]] > vals[windows[2]]
    ordered_down = vals[windows[0]] < vals[windows[1]] < vals[windows[2]]
    if ordered_up and close >= vals[windows[0]]:
        return "多头排列"
    if ordered_down and close <= vals[windows[0]]:
        return "空头排列"
    return "交叉缠绕"


def trend_judge(df: pd.DataFrame, windows: list[int]) -> str:
    """趋势判定（短期/中期综合）：
    - 上升趋势: 多头排列，或价格站上 MA20 且 MA20 上行
    - 下降趋势: 空头排列，或价格跌破 MA20 且 MA20 下行
    - 震荡趋势: 其余情况
    """
    ma_state = ma_alignment(df, windows)
    if ma_state == "多头排列":
        return "上升趋势"
    if ma_state == "空头排列":
        return "下降趋势"
    if len(df) < max(windows) + 3:
        return "震荡趋势"
    ma20 = df[f"ma{windows[2]}"]
    close = df["close"]
    # MA20 斜率：近 3 日变化
    slope = ma20.iloc[-1] - ma20.iloc[-4]
    if close.iloc[-1] > ma20.iloc[-1] and slope > 0:
        return "上升趋势"
    if close.iloc[-1] < ma20.iloc[-1] and slope < 0:
        return "下降趋势"
    return "震荡趋势"


# ------------------------------------------------------------
# 价格位置
# ------------------------------------------------------------

def position_percentile(closes: pd.Series, window: int,
                        low_th: float = 30, high_th: float = 70) -> tuple[float, str]:
    """当前价在近 N 日历史区间的百分位。返回 (百分位, 位置标签)。"""
    recent = closes.tail(window)
    if len(recent) < 10:
        return float("nan"), "数据不足"
    lo, hi = recent.min(), recent.max()
    cur = closes.iloc[-1]
    if hi <= lo:
        pct = 50.0
    else:
        pct = (cur - lo) / (hi - lo) * 100.0
    if pct <= low_th:
        label = "低位"
    elif pct >= high_th:
        label = "高位"
    else:
        label = "中位"
    return round(float(pct), 1), label


# ------------------------------------------------------------
# 支撑 / 压力位
# ------------------------------------------------------------

def support_resistance(df: pd.DataFrame, lookback: int = 60, swing_window: int = 3,
                       tolerance: float = 0.02,
                       break_lookback: int = 20) -> dict[str, Any]:
    """识别近期关键支撑位与压力位。

    方法：取近 lookback 日，找摆动低点/高点（±swing_window 邻域极值），
    再叠加 MA20 动态支撑。当前价下方最近的摆动低点为「最近支撑」，
    上方最近的摆动高点为「最近压力」。

    另返回 broken_support：现价上方、最近 break_lookback 根K线内出现的
    最近前支撑带（价格自该支撑带跌破），用于「有效跌破支撑」判定。
    """
    if df.empty or len(df) < swing_window * 2 + 2:
        return {"supports": [], "resistances": [], "nearest_support": None,
                "nearest_resistance": None, "strong_support": None, "ma20": None,
                "broken_support": None}
    sub = df.tail(lookback).reset_index(drop=True)
    closes = sub["close"]
    lows = sub["low"].to_numpy()
    highs = sub["high"].to_numpy()
    cur = float(closes.iloc[-1])

    swing_lows: list[tuple[float, int]] = []  # (价位, 相对索引)
    swing_highs: list[float] = []
    n = len(sub)
    for i in range(swing_window, n - swing_window):
        seg_low = lows[i - swing_window:i + swing_window + 1]
        seg_high = highs[i - swing_window:i + swing_window + 1]
        if lows[i] <= seg_low.min():
            swing_lows.append((float(lows[i]), i))
        if highs[i] >= seg_high.max():
            swing_highs.append(float(highs[i]))

    # 去掉过于接近的重复位（相差 <0.5% 合并取平均）
    def cluster(vals: list[float]) -> list[float]:
        out: list[float] = []
        for v in sorted(set(vals)):
            if out and abs(v - out[-1]) / max(out[-1], 1e-9) < 0.005:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out

    lows_c = cluster([v for v, _ in swing_lows])
    highs_c = cluster(swing_highs)

    supports = sorted([v for v in lows_c if v < cur], reverse=True)
    resistances = sorted([v for v in highs_c if v > cur])

    # 现价上方、近 break_lookback 根内的最近前支撑（已被跌破的支撑带）
    broken: float | None = None
    for v, idx in sorted(swing_lows, key=lambda x: x[0], reverse=True):
        if v > cur and idx >= n - break_lookback:
            broken = v
            break

    ma20 = float(sma(closes, 20).iloc[-1]) if len(closes) >= 20 else None
    if ma20 is not None and ma20 < cur:
        supports.append(round(ma20, 4))

    # 强支撑：与 MA20 相距 5% 以内的摆动低点（多信号共振）
    strong: float | None = None
    if ma20 is not None:
        for v in supports:
            if abs(v - ma20) / ma20 <= 0.05:
                strong = round(v, 4)
                break
        if strong is None and supports:
            strong = supports[0]

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # 若数据不足找不到摆动点，用区间极值兜底
    if nearest_support is None and len(sub) >= 20:
        nearest_support = round(float(sub["low"].tail(20).min()), 4)
        supports = [nearest_support]
    if nearest_resistance is None and len(sub) >= 20:
        nearest_resistance = round(float(sub["high"].tail(20).max()), 4)
        resistances = [nearest_resistance]

    return {
        "supports": [round(v, 4) for v in supports[:5]],
        "resistances": [round(v, 4) for v in resistances[:5]],
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "strong_support": strong if strong is not None else nearest_support,
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "broken_support": round(broken, 4) if broken is not None else None,
    }


def effective_break(close: pd.Series, broken_support: float | None, tolerance: float,
                    confirm_days: int) -> bool:
    """判断是否有效跌破前支撑：现价位于前支撑带下方，且最近 N 日收盘
    持续低于 前支撑*(1-容差)。"""
    if broken_support is None or len(close) < confirm_days:
        return False
    threshold = broken_support * (1 - tolerance)
    recent = close.tail(confirm_days)
    return bool((recent < threshold).all())


# ------------------------------------------------------------
# 量价分析（K线接口不含成交量，采用振幅代理 + 在售/求购量）
# ------------------------------------------------------------

def series_momentum_ratio(series: pd.Series, recent: int = 5, prior: int = 20,
                          expand_ratio: float = 1.3, shrink_ratio: float = 0.75) -> dict[str, Any]:
    """一维序列（成交量/成交额/振幅）的近期动量比。

    近 recent 日均值 / 前 prior 日均值：>= expand_ratio 视为放量，
    <= shrink_ratio 视为缩量，否则平量。用于大盘真实成交额/成交量判断。
    """
    s = series.dropna()
    if len(s) < recent + prior:
        return {"state": "数据不足", "ratio": None}
    recent_mean = float(s.tail(recent).mean())
    prior_mean = float(s.tail(prior).mean())
    ratio = recent_mean / prior_mean if prior_mean > 0 else 1.0
    if ratio >= expand_ratio:
        state = "放量"
    elif ratio <= shrink_ratio:
        state = "缩量"
    else:
        state = "平量"
    return {"state": state, "ratio": round(ratio, 2), "recent_mean": recent_mean,
            "prior_mean": prior_mean}


def volume_proxy(df: pd.DataFrame, expand_ratio: float = 1.3,
                 shrink_ratio: float = 0.75, stagnation_return: float = 0.01) -> dict[str, Any]:
    """量价代理分析。

    说明：SteamDT 单品K线不含成交量，以「日内振幅均值」作为活跃度代理：
      近期5日振幅 / 前期20日振幅 >= expand_ratio  => 放量（活跃度放大）
      <= shrink_ratio                               => 缩量（活跃度萎缩）
    并给出 5日涨跌幅、3日涨跌幅，用于「放量下跌 / 缩量止跌 / 放量滞涨」判定。
    """
    if df.empty or len(df) < 20:
        return {"state": "数据不足", "amp_ratio": None, "ret_5d": None, "ret_3d": None,
                "proxy_note": "K线数据不足20根，无法计算量价代理"}
    amp = (df["high"] - df["low"]) / df["close"]
    recent_amp = float(amp.tail(5).mean())
    prior_amp = float(amp.tail(20).mean())
    ratio = recent_amp / prior_amp if prior_amp > 0 else 1.0
    close = df["close"]
    ret_5d = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) >= 6 else 0.0
    ret_3d = float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) >= 4 else 0.0

    if ratio >= expand_ratio:
        state = "放量"
    elif ratio <= shrink_ratio:
        state = "缩量"
    else:
        state = "平量"

    return {
        "state": state,
        "amp_ratio": round(ratio, 2),
        "ret_5d": round(ret_5d * 100, 2),
        "ret_3d": round(ret_3d * 100, 2),
        "stagnation": abs(ret_5d) <= stagnation_return,
        "vol_up_down": ratio >= expand_ratio and ret_5d < -stagnation_return,   # 放量下跌
        "vol_up_stall": ratio >= expand_ratio and abs(ret_5d) <= stagnation_return,  # 放量滞涨
        "shrink_stop_falling": ratio <= shrink_ratio and ret_3d >= 0,           # 缩量止跌
        "proxy_note": "注：SteamDT K线不含成交量，放/缩量为日内振幅代理指标，仅供参考",
    }
