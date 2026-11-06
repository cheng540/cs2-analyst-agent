"""官网公开统计解析与统计模式大盘判定单元测试。"""
import unittest

import pandas as pd

from cs2analyst.analysis.indicators import series_momentum_ratio
from cs2analyst.analysis.market import analyze_market_stats
from cs2analyst.datasource.steamdt_scraper import parse_daily_rows


class TestParseDailyRows(unittest.TestCase):
    def test_basic(self):
        rows = [["2026-09-21", "100.5", "10"], ["2026-09-22", "120.0", "12"],
                ["2026-09-22", "130.0", "13"]]  # 重复日期保留后者
        df = parse_daily_rows(rows, ["turnover", "volume"])
        self.assertEqual(len(df), 2)
        self.assertEqual(float(df.iloc[-1]["turnover"]), 130.0)
        self.assertEqual(list(df.columns), ["date", "turnover", "volume"])

    def test_bad_rows_skipped(self):
        rows = [["bad-date", "1", "2"], [12345, "3"], ["2026-09-22", None, "5"]]
        df = parse_daily_rows(rows, ["turnover", "volume"])
        self.assertEqual(len(df), 1)
        self.assertTrue(pd.isna(df.iloc[0]["turnover"]))


class TestSeriesMomentumRatio(unittest.TestCase):
    def test_states(self):
        s = pd.Series([10.0] * 20 + [20.0] * 5)
        r = series_momentum_ratio(s, 5, 20, 1.3, 0.75)
        self.assertEqual(r["state"], "放量")
        self.assertAlmostEqual(r["ratio"], 1.6, places=1)  # 前20日窗口含近5日
        s2 = pd.Series([20.0] * 20 + [10.0] * 5)
        r2 = series_momentum_ratio(s2, 5, 20, 1.3, 0.75)
        self.assertEqual(r2["state"], "缩量")
        s3 = pd.Series([10.0] * 25)
        r3 = series_momentum_ratio(s3, 5, 20, 1.3, 0.75)
        self.assertEqual(r3["state"], "平量")

    def test_insufficient(self):
        r = series_momentum_ratio(pd.Series([1.0, 2.0]), 5, 20)
        self.assertEqual(r["state"], "数据不足")


def _summary(up: bool, days: int = 2, diff: float = 0.4, score: float = 41.0,
             hourly=None) -> dict:
    if hourly is None:
        base = 790.0 if up else 796.0
        step = 0.2 if up else -0.2
        hourly = [[1790000000 + i * 3600, base + i * step] for i in range(24)]
    return {
        "marketIndexTrend": {
            "broadMarketIndex": 795.45,
            "diffYesterday": diff,
            "diffYesterdayRatio": diff,
            "riseFallType": "UP" if up else "DOWN",
            "riseFallDays": days,
            "historyMarketIndexList": hourly,
        },
        "transPerformanceTrend": {"performanceScore": score, "diffYesterdayScore": 12},
    }


def _txn(up_volume: bool = True) -> pd.DataFrame:
    """30天成交额序列，最后5天放量/缩量。"""
    vals = [100.0] * 25 + ([150.0] * 5 if up_volume else [60.0] * 5)
    dates = pd.date_range(end=pd.Timestamp("2026-09-23"), periods=30)
    return pd.DataFrame({"date": dates, "turnover": vals, "volume": [v * 10 for v in vals]})


class TestAnalyzeMarketStats(unittest.TestCase):
    def test_up_market(self):
        env = analyze_market_stats(_summary(up=True), _txn(True), [5, 10, 20], {})
        self.assertEqual(env.trend, "上升趋势")
        self.assertEqual(env.risk, "低风险")
        self.assertEqual(env.volume_state, "放量")
        self.assertFalse(env.block_add())
        self.assertEqual(env.sentiment_score, 41.0)

    def test_down_market(self):
        env = analyze_market_stats(_summary(up=False, days=3, diff=-0.8), _txn(False),
                                   [5, 10, 20], {})
        self.assertEqual(env.trend, "下降趋势")
        self.assertEqual(env.risk, "高风险")
        self.assertTrue(env.block_add())
        self.assertEqual(env.volume_state, "缩量")

    def test_crash_upgrades_risk(self):
        env = analyze_market_stats(_summary(up=True, diff=-2.5), _txn(True), [5, 10, 20], {})
        self.assertEqual(env.risk, "高风险")

    def test_intraday_reversal_softens_trend(self):
        # 连跌但日内强势反转（尾盘拉升）→ 震荡
        hourly = [[1790000000 + i * 3600, 800 - i * 0.5] for i in range(18)]
        hourly += [[1790000000 + (18 + i) * 3600, 791 + i * 0.8] for i in range(6)]
        env = analyze_market_stats(_summary(up=False, days=1, diff=-0.2, hourly=hourly),
                                   _txn(True), [5, 10, 20], {})
        self.assertEqual(env.trend, "震荡趋势")

    def test_no_txn(self):
        env = analyze_market_stats(_summary(up=True), None, [5, 10, 20], {})
        self.assertEqual(env.volume_state, "数据不足")
        self.assertEqual(env.trend, "上升趋势")


class TestEnrichScraper(unittest.TestCase):
    def test_enrich_overrides_volume(self):
        from cs2analyst.analysis.market import MarketEnvironment
        env = MarketEnvironment(trend="震荡趋势", risk="中风险", volume_state="平量")
        env.enrich_scraper(_summary(up=True), _txn(True))
        self.assertEqual(env.volume_state, "放量")
        self.assertEqual(env.sentiment_score, 41.0)
        self.assertEqual(env.rise_fall_type, "UP")
        self.assertTrue(any("情绪分" in e for e in env.evidence))


class TestAnalyzeMarketRet5d(unittest.TestCase):
    """大盘近5日涨跌（相对强度的基准，2026-10-01 新增）。"""

    def _daily(self, closes: list[float]) -> pd.DataFrame:
        n = len(closes)
        dates = pd.date_range(end=pd.Timestamp("2026-10-01"), periods=n)
        return pd.DataFrame({
            "ts": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
        })

    def test_ret_5d_computed(self):
        from cs2analyst.analysis.market import analyze_market
        closes = [100.0] * 25 + [110.0]        # 5 日前 100 → 现在 110
        env = analyze_market(self._daily(closes), pd.DataFrame(), {}, [5, 10, 20], {})
        self.assertAlmostEqual(env.ret_5d, 10.0, places=1)
        self.assertTrue(any("近5日" in e for e in env.evidence))

    def test_ret_5d_negative(self):
        from cs2analyst.analysis.market import analyze_market
        closes = [100.0] * 25 + [90.0]
        env = analyze_market(self._daily(closes), pd.DataFrame(), {}, [5, 10, 20], {})
        self.assertAlmostEqual(env.ret_5d, -10.0, places=1)

    def test_ret_5d_unknown_without_kline(self):
        from cs2analyst.analysis.market import MarketEnvironment
        self.assertIsNone(MarketEnvironment().ret_5d)   # 缺数据 → 评分跳过该因子


if __name__ == "__main__":
    unittest.main()
