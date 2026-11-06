"""技术指标单元测试。"""
import unittest

import pandas as pd

from cs2analyst.analysis import indicators as ind


def mk_df(prices: list[float], amp: float = 0.01) -> pd.DataFrame:
    """由收盘价序列构造 OHLC DataFrame。"""
    rows = []
    for i, c in enumerate(prices):
        o = prices[i - 1] if i > 0 else c
        h = max(o, c) * (1 + amp / 2)
        l = min(o, c) * (1 - amp / 2)
        rows.append({"ts": i * 86400, "open": o, "close": c, "high": h, "low": l})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["ts"], unit="s")
    return df


class TestMovingAverage(unittest.TestCase):
    def test_sma_basic(self):
        s = pd.Series([1, 2, 3, 4, 5], dtype=float)
        got = ind.sma(s, 3)
        self.assertTrue(pd.isna(got.iloc[0]))
        self.assertAlmostEqual(got.iloc[2], 2.0)
        self.assertAlmostEqual(got.iloc[4], 4.0)


class TestMaAlignment(unittest.TestCase):
    def test_bullish(self):
        # 稳定上行：MA5 > MA10 > MA20，收盘在 MA5 上方
        prices = [100 + i * 1.0 for i in range(60)]
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.ma_alignment(df, [5, 10, 20]), "多头排列")

    def test_bearish(self):
        prices = [200 - i * 1.0 for i in range(60)]
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.ma_alignment(df, [5, 10, 20]), "空头排列")

    def test_crossing(self):
        # 正弦震荡 → 交叉缠绕
        import math
        prices = [100 + 8 * math.sin(i / 5) for i in range(60)]
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.ma_alignment(df, [5, 10, 20]), "交叉缠绕")


class TestTrendJudge(unittest.TestCase):
    def test_up(self):
        prices = [100 + i * 1.0 for i in range(80)]
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.trend_judge(df, [5, 10, 20]), "上升趋势")

    def test_down(self):
        prices = [200 - i * 1.0 for i in range(80)]
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.trend_judge(df, [5, 10, 20]), "下降趋势")

    def test_range(self):
        # 震荡序列末端收敛回中枢 → 均线缠绕、无趋势
        import math
        prices = [100 + 5 * math.sin(i / 4) for i in range(60)] + [100.0] * 20
        df = ind.add_ma_columns(mk_df(prices), [5, 10, 20])
        self.assertEqual(ind.trend_judge(df, [5, 10, 20]), "震荡趋势")


class TestPositionPercentile(unittest.TestCase):
    def test_high_mid_low(self):
        closes = pd.Series([100 + i for i in range(100)], dtype=float)
        pct, label = ind.position_percentile(closes, 60, 30, 70)
        self.assertAlmostEqual(pct, 100.0, places=1)
        self.assertEqual(label, "高位")
        closes2 = pd.Series([100 - i for i in range(100)], dtype=float)
        pct2, label2 = ind.position_percentile(closes2, 60, 30, 70)
        self.assertAlmostEqual(pct2, 0.0, places=1)
        self.assertEqual(label2, "低位")
        # 震荡序列末端回到区间中枢 → 中位
        import math
        mid = [100 + 4 * math.sin(i / 5) for i in range(80)] + [100.0]
        _, label3 = ind.position_percentile(pd.Series(mid, dtype=float), 60, 30, 70)
        self.assertEqual(label3, "中位")


class TestSupportResistance(unittest.TestCase):
    def _range_df(self, mean: float = 100.0, n: int = 120) -> pd.DataFrame:
        import math
        prices = [mean + 6 * math.sin(i / 6) for i in range(n)]
        return ind.add_ma_columns(mk_df(prices, amp=0.005), [5, 10, 20])

    def test_support_below_resistance_above(self):
        df = self._range_df()
        sr = ind.support_resistance(df, 60, 3)
        cur = float(df["close"].iloc[-1])
        self.assertIsNotNone(sr["nearest_support"])
        self.assertLess(sr["nearest_support"], cur)
        self.assertIsNotNone(sr["nearest_resistance"])
        self.assertGreater(sr["nearest_resistance"], cur)
        self.assertIsNotNone(sr["strong_support"])

    def test_broken_support_after_breakdown(self):
        # 前段在 100 附近震荡（支撑带 ~94），尾段放量跌破到 85：
        # 震荡低点（94）应被识别为已跌破的前支撑
        import math
        prices = [100 + 6 * math.sin(i / 3) for i in range(95)]
        prices += [94, 91, 88, 86, 85]
        df = ind.add_ma_columns(mk_df(prices, amp=0.01), [5, 10, 20])
        sr = ind.support_resistance(df, 60, 3, break_lookback=20)
        cur = float(df["close"].iloc[-1])
        self.assertIsNotNone(sr["broken_support"])
        self.assertGreater(sr["broken_support"], cur)
        # 有效跌破：近2日收盘 < 前支撑*0.98
        self.assertTrue(ind.effective_break(df["close"], sr["broken_support"], 0.02, 2))


class TestEffectiveBreak(unittest.TestCase):
    def test_break_true(self):
        closes = pd.Series([100, 99, 98, 97, 96], dtype=float)
        self.assertTrue(ind.effective_break(closes, broken_support=101.0,
                                            tolerance=0.02, confirm_days=2))
        self.assertFalse(ind.effective_break(closes, broken_support=98.0,
                                             tolerance=0.02, confirm_days=2))

    def test_no_broken_support(self):
        self.assertFalse(ind.effective_break(pd.Series([1.0, 2.0]), None, 0.02, 2))

    def test_insufficient_days(self):
        self.assertFalse(ind.effective_break(pd.Series([90.0]), 100.0, 0.02, 3))


class TestVolumeProxy(unittest.TestCase):
    def _df(self, prices: list[float], last_amp: float = 0.01) -> pd.DataFrame:
        df = mk_df(prices, amp=0.01)
        # 重设最后5根振幅
        n = len(df)
        for i in range(n - 5, n):
            row = df.iloc[i]
            df.at[i, "high"] = max(row["open"], row["close"]) * (1 + last_amp / 2)
            df.at[i, "low"] = min(row["open"], row["close"]) * (1 - last_amp / 2)
        return df

    def test_expansion_down(self):
        prices = [100 - i * 0.5 for i in range(30)]
        vol = ind.volume_proxy(self._df(prices, last_amp=0.05), 1.3, 0.75, 0.01)
        self.assertEqual(vol["state"], "放量")
        self.assertTrue(vol["vol_up_down"])

    def test_shrink_stop_falling(self):
        prices = [100 - i * 0.5 for i in range(25)] + [87.5, 87.6, 87.7, 87.8, 87.9]
        vol = ind.volume_proxy(self._df(prices, last_amp=0.002), 1.3, 0.75, 0.01)
        self.assertEqual(vol["state"], "缩量")
        self.assertTrue(vol["shrink_stop_falling"])

    def test_stall_at_resistance(self):
        prices = [80 + i * 1.0 for i in range(25)] + [104.2, 104.3, 104.2, 104.4, 104.3]
        vol = ind.volume_proxy(self._df(prices, last_amp=0.05), 1.3, 0.75, 0.01)
        self.assertEqual(vol["state"], "放量")
        self.assertTrue(vol["vol_up_stall"])


if __name__ == "__main__":
    unittest.main()
