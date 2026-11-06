"""Nuxt 载荷解析器与快照模式单品分析单元测试。"""
import unittest

from cs2analyst.analysis.item import analyze_item_snapshot
from cs2analyst.datasource.inventory import Holding
from cs2analyst.datasource.nuxt_payload import (extract_item_dict,
                                                parse_payload_from_html,
                                                resolve_nuxt_payload)

CFG_A = {"volume_expand_ratio": 1.3, "volume_shrink_ratio": 0.75,
         "stagnation_return": 1.0}
CFG_D = {"snapshot_liq_high": 150, "snapshot_liq_low": 40}


def make_payload_html() -> str:
    """构造与官网同构的最小 Nuxt 载荷页面。"""
    payload = [
        ["ShallowReactive", 1],
        {"data": 2, "serverRendered": 8},
        {"$fetchKey": 3},
        {"success": 8, "data": 4, "errorCode": 5, "errorMsg": 6, "errorData": 6,
         "errorCodeStr": 6},
        {"marketHashName": "AK-47 | Redline (Field-Tested)",
         "itemId": "123456",
         "surviveNum": "150000",
         "holdersNum": "90000",
         "turnoverRate": 0.12,
         "volumeRatio": 1.5,
         "transactionCount": "300",
         "cashRatio": 0.8,
         "diff1Day": 0.5,
         "diff7Day": -3.2,
         "diff30Day": -8.0,
         "diff6Month": -15.0,
         "specialStyle": [],
         "sellingPriceList": [
             {"platform": "buff", "platformName": "BUFF", "price": 99.5, "updateTime": 1790150000},
             {"platform": "youpin", "platformName": "悠悠", "price": 100.2, "updateTime": 1790150000},
         ],
         "trendList": []},
        0, "", True,
    ]
    import json
    return f'<html><head><script type="application/json" id="__NUXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></head><body></body></html>'


class TestNuxtPayload(unittest.TestCase):
    def test_resolver(self):
        payload = [["ShallowReactive", 1], {"a": 2, "b": 4}, {"x": 3}, {"y": 5},
                   "keep-string", 42]
        out = resolve_nuxt_payload(payload)
        # a -> {"x": {"y": 42}}；b -> "keep-string"
        self.assertEqual(out["a"]["x"]["y"], 42)
        self.assertEqual(out["b"], "keep-string")

    def test_parse_and_extract(self):
        root = parse_payload_from_html(make_payload_html())
        self.assertIn("data", root)
        item = extract_item_dict(make_payload_html())
        self.assertEqual(item["marketHashName"], "AK-47 | Redline (Field-Tested)")
        self.assertEqual(len(item["sellingPriceList"]), 2)

    def test_missing_payload_raises(self):
        with self.assertRaises(ValueError):
            parse_payload_from_html("<html>no payload</html>")


class TestAnalyzeItemSnapshot(unittest.TestCase):
    def _detail(self, **overrides) -> dict:
        detail = {
            "marketHashName": "AK-47 | Redline (Field-Tested)",
            "surviveNum": "150000", "holdersNum": "90000", "turnoverRate": 0.12,
            "volumeRatio": 1.5, "transactionCount": "300", "cashRatio": 0.8,
            "diff1Day": 0.5, "diff7Day": -3.2, "diff30Day": -8.0, "diff6Month": -15.0,
            "sellingPriceList": [
                {"platform": "buff", "price": 99.5},
                {"platform": "youpin", "price": 100.2},
            ],
        }
        detail.update(overrides)
        return detail

    def test_basic_snapshot(self):
        h = Holding(market_hash_name="X", quantity=2, buy_price=120.0)
        a = analyze_item_snapshot(self._detail(), h, CFG_A, CFG_D)
        self.assertTrue(a.data_ok)
        self.assertTrue(a.snapshot_mode)
        self.assertEqual(a.cur_price, 99.5)          # 最低在售价
        self.assertAlmostEqual(a.pnl_pct, 99.5 / 120 - 1)
        self.assertEqual(a.trend, "下降趋势")         # 30日 -8%、6月 -15%
        self.assertEqual(a.volume_state, "放量")      # 量比 1.5
        self.assertTrue(a.vol_up_down)               # 量比>=1.3 且 7日 -3.2%
        self.assertEqual(a.liquidity_grade, "高")     # 当日成交 300
        self.assertEqual(a.survive_num, 150000)
        self.assertEqual(a.ma_state, "快照模式（无K线）")
        self.assertFalse(a.break_support)

    def test_snapshot_shrink_stop_falling(self):
        h = Holding(market_hash_name="X", quantity=1, buy_price=100.0)
        detail = self._detail(volumeRatio=0.5, diff1Day=0.3, diff7Day=-0.5,
                              diff30Day=1.0, diff6Month=3.0)
        a = analyze_item_snapshot(detail, h, CFG_A, CFG_D)
        self.assertEqual(a.volume_state, "缩量")
        self.assertTrue(a.shrink_stop_falling)
        self.assertEqual(a.trend, "震荡趋势")

    def test_snapshot_low_liquidity(self):
        h = Holding(market_hash_name="X", quantity=1, buy_price=100.0)
        a = analyze_item_snapshot(self._detail(transactionCount="12"), h, CFG_A, CFG_D)
        self.assertEqual(a.liquidity_grade, "低")

    def test_snapshot_missing_prices(self):
        h = Holding(market_hash_name="X", quantity=1, buy_price=100.0)
        a = analyze_item_snapshot(self._detail(sellingPriceList=[]), h, CFG_A, CFG_D)
        self.assertFalse(a.data_ok)

    def test_snapshot_filters_junk_price(self):
        """个别平台返回占位价（0.1），应被中位数相对阈值剔除。"""
        h = Holding(market_hash_name="X", quantity=1, buy_price=100.0)
        detail = self._detail(sellingPriceList=[
            {"platform": "c5", "price": 0.1},
            {"platform": "buff", "price": 99.5},
            {"platform": "youpin", "price": 100.2},
        ])
        a = analyze_item_snapshot(detail, h, CFG_A, CFG_D)
        self.assertTrue(a.data_ok)
        self.assertEqual(a.cur_price, 99.5)
        self.assertEqual(len(a.price_rows), 2)


class TestSnapshotDecision(unittest.TestCase):
    CFG = {"max_position_pct": 25.0}

    def test_deep_bear_snapshot_clears(self):
        """快照模式：深亏 + 下降趋势 + 低流动性 + 放量下跌 → 清仓。"""
        from cs2analyst.analysis.market import MarketEnvironment
        from cs2analyst.engine.decision import Action, decide
        h = Holding(market_hash_name="X", quantity=1, buy_price=200.0)
        detail = {
            "sellingPriceList": [{"platform": "buff", "price": 130.0}],
            "transactionCount": "10", "volumeRatio": 1.6,
            "diff1Day": -2.0, "diff7Day": -9.0, "diff30Day": -12.0, "diff6Month": -20.0,
        }
        a = analyze_item_snapshot(detail, h, CFG_A, CFG_D)
        d = decide(MarketEnvironment(trend="震荡趋势", risk="中风险"), a, self.CFG, 10000.0)
        self.assertEqual(d.action, Action.CLEAR)

    def test_snapshot_can_buy_on_real_signals(self):
        """快照模式不再机械禁买：量比/涨跌等真实信号支持时允许介入。"""
        from cs2analyst.analysis.market import MarketEnvironment
        from cs2analyst.engine.decision import Action, decide
        h = Holding(market_hash_name="X", quantity=0, buy_price=0.0)
        detail = {
            "sellingPriceList": [{"platform": "buff", "price": 99.0}],
            "transactionCount": "300", "volumeRatio": 0.5,
            "diff1Day": 0.4, "diff7Day": 0.3, "diff30Day": 6.0, "diff6Month": 12.0,
        }
        a = analyze_item_snapshot(detail, h, CFG_A, CFG_D)
        d = decide(MarketEnvironment(trend="上升趋势", risk="低风险"), a, self.CFG, 10000.0)
        self.assertIn(d.action, (Action.BUY, Action.HOLD))


if __name__ == "__main__":
    unittest.main()
