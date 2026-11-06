"""生成离线演示数据（合成行情 + 演示持仓），覆盖四种决策类型：
止损（亏损线/占比过高/破位放量）、止盈（压力位放量滞涨/达标）、
补仓（强支撑缩量止跌）、持有（震荡/无信号），以及一个数据缺失条目。

K线为合成随机游走 + 场景化尾部修饰，力求让内置决策引擎
在离线模式下完整展示每种建议类型。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

# ------------------------------------------------------------
# K线合成工具
# ------------------------------------------------------------

def _bars_to_rows(bars: list[tuple[float, float, float, float, float]],
                  end_ts: int) -> list[list]:
    """[(o,c,h,l) ...] -> [[ts,o,c,h,l] ...] 按日递减。"""
    n = len(bars)
    rows = []
    for i, (o, c, h, l) in enumerate(bars):
        ts = end_ts - (n - 1 - i) * 86400
        rows.append([ts, round(o, 4), round(c, 4), round(h, 4), round(l, 4)])
    return rows


def _base_walk(rng: np.random.Generator, n: int, start: float, drift: float,
               vol: float, mode: str = "walk", mean: float | None = None,
               period: float = 9.0) -> list[float]:
    """生成基础价格序列（随机游走 / 正弦震荡）。"""
    prices = [start]
    for i in range(1, n):
        if mode == "range":
            base = (mean if mean is not None else start) * (1 + 0.035 * math.sin(i / period))
            next_p = base * (1 + rng.normal(0, vol))
        else:
            next_p = prices[-1] * (1 + drift + rng.normal(0, vol))
        prices.append(max(next_p, 1.0))
    return prices


def _from_prices(prices: list[float], amp: float, rng: np.random.Generator,
                 last_amp: float | None = None, last_n: int = 5) -> list[tuple]:
    """价格序列 -> OHLC 列表；最后 last_n 根可指定不同振幅（放量/缩量）。"""
    bars = []
    n = len(prices)
    for i in range(n):
        cur_amp = amp
        if last_amp is not None and i >= n - last_n:
            cur_amp = last_amp
        half = cur_amp / 2
        o = prices[i - 1] if i > 0 else prices[i]
        c = prices[i]
        h = max(o, c) * (1 + half * rng.random())
        l = min(o, c) * (1 - half * rng.random())
        bars.append((o, c, h, l))
    return bars


def make_item_kline(seed: int, start: float, pattern: str,
                    support: float | None = None, resistance: float | None = None,
                    n: int = 220, cur: float | None = None) -> list[list]:
    """按场景生成单品日K。返回 [[ts,o,c,h,l]...]（时间升序）。"""
    rng = np.random.default_rng(seed)
    body_n = n - (20 if pattern == "resistance_stall" else 12)

    if pattern == "downtrend":
        # 全程阴跌，保证空头排列；start 需按目标末端价反推
        prices = _base_walk(rng, body_n, start, -0.0035, 0.010)
        tail, p = [], prices[-1]
        for _ in range(12):
            p = p * (1 - 0.004 - rng.random() * 0.005)
            tail.append(p)
        prices += tail
        bars = _from_prices(prices, 0.014, rng)
    elif pattern == "uptrend":
        prices = _base_walk(rng, body_n, start, 0.004, 0.010)
        tail, p = [], prices[-1]
        for _ in range(12):
            p = p * (1 + 0.003 + rng.random() * 0.004)
            tail.append(p)
        prices += tail
        bars = _from_prices(prices, 0.012, rng)
    elif pattern == "range":
        prices = _base_walk(rng, body_n + 12, start, 0.0, 0.009, mode="range",
                            mean=start, period=8.0)
        bars = _from_prices(prices, 0.011, rng)
    elif pattern == "approach_support":
        assert support is not None
        # 高位震荡后回落，最后 12 根贴着支撑缩量企稳（近3根微幅回升=止跌）
        hi = support * 1.18
        prices = _base_walk(rng, body_n, hi, -0.002, 0.010, mode="range", mean=hi * 0.98)
        tail, p = [], prices[-1]
        target = support * 1.004
        for i in range(12):
            p += (target - p) * 0.35
            p = max(p, support * 1.000)
            if i >= 9:  # 最后3根微幅回升（3日止跌）
                p *= (1 + 0.0008 + rng.random() * 0.0015)
            tail.append(p)
        prices += tail
        bars = _from_prices(prices, 0.012, rng, last_amp=0.004)  # 尾部缩量
    elif pattern == "break_support":
        assert support is not None
        # 先围绕支撑上方 6% 震荡（形成清晰支撑带），尾段先回抽后连续放量破位
        prices = _base_walk(rng, body_n, support * 1.06, 0.0, 0.009, mode="range",
                            mean=support * 1.06, period=8.0)
        tail, p = [], prices[-1]
        target = support * 1.02
        for i in range(6):
            p += (target - p) * 0.4
            tail.append(p)
        for i in range(6):
            p = support * (0.99 - 0.007 * i)   # 最后2根 < 支撑*0.98
            tail.append(p)
        prices += tail
        bars = _from_prices(prices, 0.012, rng, last_amp=0.045)  # 尾部放量下跌
    elif pattern == "resistance_stall":
        assert resistance is not None
        # 先在中枢震荡上行至压力位下方，尾段贴近压力位横盘且振幅放大（放量滞涨）
        prices = _base_walk(rng, body_n, resistance * 0.92, 0.0, 0.009, mode="range",
                            mean=resistance * 0.92, period=9.0)
        tail, p = [], prices[-1]
        target = resistance * 0.990
        for i in range(20):
            p += (target - p) * 0.12          # 缓慢逼近压力位，避免形成大跳空振幅
            p *= (1 + rng.normal(0, 0.0008))
            tail.append(p)
        prices += tail
        bars = _from_prices(prices, 0.008, rng, last_amp=0.030)  # 末端放量滞涨
    else:
        prices = _base_walk(rng, body_n + 12, start, 0.0, 0.010)
        bars = _from_prices(prices, 0.012, rng)

    if cur is not None:  # 校准最后一根收盘价（仅小幅修正）
        o, _, h, l = bars[-1]
        bars[-1] = (o, round(cur, 4), max(h, cur), min(l, cur))
    return _bars_to_rows(bars, int(datetime.now().timestamp()))


def make_broad_kline(seed: int, start: float = 1000.0, n: int = 300) -> list[list]:
    """合成大盘日K：中枢震荡，尾段走平收口（中风险环境）。"""
    rng = np.random.default_rng(seed)
    prices = _base_walk(rng, n - 40, start, 0.0004, 0.006, mode="range",
                        mean=start, period=15)
    tail, p = [], prices[-1]
    for _ in range(40):  # 尾段缓慢收敛至中枢 → 均线缠绕、震荡判定
        p += (start - p) * 0.1
        p *= (1 + rng.normal(0, 0.002))
        tail.append(p)
    prices += tail
    bars = _from_prices(prices, 0.009, rng)
    return _bars_to_rows(bars, int(datetime.now().timestamp()))


# ------------------------------------------------------------
# 场景定义
# ------------------------------------------------------------

def _build_items() -> tuple[list[dict], dict[str, dict]]:
    """返回 (持仓列表, 行情数据字典)。"""
    # 字段: (name, seed, pattern, start, support, resistance, cur, buy_price, qty, liquidity, include)
    specs = [
        # 1. 🟢轻仓补仓：回落至强支撑 + 缩量止跌，亏损 <15%
        ("AK-47 | Redline (Field-Tested)", 11, "approach_support", 118.0, 95.0, None, 95.8,
         110.0, 5, "mid", True),
        # 2. 🔴果断止损A：亏损≥30% + 下降趋势 + 低流动性
        ("AWP | Asiimov (Battle-Scarred)", 22, "downtrend", 630.0, None, None, 279.5,
         430.0, 2, "low", True),
        # 3. 🟢分批止盈：盈利≥20% + 压力位放量滞涨
        ("★ Karambit | Doppler (Factory New)", 33, "resistance_stall", 8800.0, None, 9600.0, 9550.0,
         7600.0, 1, "high", True),
        # 4. 🔴果断止损C：占比过高 + 下降趋势
        ("M4A4 | 龙王 (Minimal Wear)", 44, "downtrend", 3360.0, None, None, 1498.0,
         1700.0, 5, "mid", True),
        # 5. 🔴果断止损B：有效跌破支撑 + 放量下跌
        ("AK-47 | 红线 (Minimal Wear)", 55, "break_support", 470.0, 445.0, None, 425.0,
         500.0, 3, "mid", True),
        # 6. 🟡持有观望：震荡区间
        ("USP-S | 印花集 (Factory New)", 66, "range", 600.0, None, None, 601.5,
         580.0, 2, "high", True),
        # 7. 🟡持有观望：震荡（轻度亏损）
        ("Glock-18 | 水灵 (Factory New)", 77, "range", 66.0, None, None, 65.3,
         70.0, 10, "mid", True),
        # 8. 🟡持有观望：上升趋势但无明确信号
        ("Desert Eagle | 印花集 (Field-Tested)", 88, "uptrend", 104.0, None, None, 250.0,
         240.0, 1, "mid", True),
        # 9. 数据缺失演示（行情数据中故意不包含该名称）
        ("★ 蝴蝶刀 | 多普勒 (Factory New)", 99, "uptrend", 9000.0, None, None, 9200.0,
         8800.0, 1, "high", False),
    ]
    holdings, item_klines, batch_rows, avg7_files = [], {}, [], {}
    for name, seed, pattern, start, sup, res, cur, buy, qty, liq, include in specs:
        holdings.append({
            "market_hash_name": name,
            "wear": name.split("(")[-1].rstrip(")") if "(" in name else "",
            "quantity": qty,
            "buy_price": buy,
            "buy_date": "2026-02-20",
            "platform": "YOUPIN",
            "special_style": "",
            "note": "",
        })
        if not include:
            continue
        if liq == "high":
            sc, bc = [420, 330, 260, 200], [180, 120, 90, 60]
        elif liq == "mid":
            sc, bc = [260, 180, 140, 90], [90, 60, 40, 30]
        else:
            sc, bc = [60, 50, 40, 25], [15, 10, 8, 5]
        rows = []
        for pf, s, b in zip(["YOUPIN", "BUFF", "C5", "STEAM"], sc, bc):
            rows.append({
                "platform": pf,
                "platformItemId": f"demo_{pf}_{seed}",
                "sellPrice": round(cur * 1.008, 2),
                "sellCount": s,
                "biddingPrice": round(cur * 0.96, 2),
                "biddingCount": b,
                "updateTime": int(datetime.now().timestamp()),
            })
        batch_rows.append({"marketHashName": name, "dataList": rows})
        avg7_files[name] = {"marketHashName": name, "avgPrice": round(cur, 2),
                            "dataList": [{"platform": "YOUPIN", "avgPrice": round(cur, 2)}]}
        item_klines[name] = {"data": make_item_kline(
            seed, start, pattern, sup, res, cur=cur)}
    return holdings, {"item_klines": item_klines, "batch_rows": batch_rows, "avg7": avg7_files}


def _safe_name(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return keep[:120]


def generate_demo_data(out_dir: Path, inventory_path: Path) -> None:
    """生成全部演示数据文件（先清空旧数据，避免残留文件干扰）。"""
    import shutil
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    holdings, data = _build_items()

    # 单品K线 + 7日均价
    for name, payload in data["item_klines"].items():
        safe = _safe_name(name)
        (out_dir / f"item_kline_{safe}_2.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    for name, payload in data["avg7"].items():
        safe = _safe_name(name)
        (out_dir / f"avg7_{safe}.json").write_text(
            json.dumps({"data": payload}, ensure_ascii=False), encoding="utf-8")

    # 批量价格
    (out_dir / "batch_price.json").write_text(
        json.dumps({"success": True, "data": data["batch_rows"]}, ensure_ascii=False),
        encoding="utf-8")

    # 大盘日K / 周K / 指数
    daily = make_broad_kline(1)
    weekly = daily[::5]
    (out_dir / "broad_kline_2.json").write_text(
        json.dumps({"success": True, "data": daily}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "broad_kline_3.json").write_text(
        json.dumps({"success": True, "data": weekly}, ensure_ascii=False), encoding="utf-8")
    last_close = daily[-1][2]
    index_payload = {
        "success": True,
        "data": {
            "broadMarketIndex": round(last_close, 2),
            "updateTime": int(datetime.now().timestamp()),
            "diffYesterday": 0.42,
            "diffYesterdayRatio": 0.04,
            "historyMarketIndexList": [[d[0], d[2]] for d in daily[-60:]],
        },
    }
    (out_dir / "broad_index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False), encoding="utf-8")

    # 演示持仓
    inventory_path = Path(inventory_path)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"演示持仓已写入：{inventory_path}")
