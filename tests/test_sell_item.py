"""已实现卖出记录工具单元测试（离线）。"""
import unittest

from tools.sell_item import compute_sale, match_holding, normalize


class TestMatchHolding(unittest.TestCase):
    ITEMS = [
        {"market_hash_name": "Five-SeveN | Silver Quartz (Factory New)", "quantity": 1,
         "note": "中文名：FN57 | 银白石英 (崭新出厂)"},
        {"market_hash_name": "FAMAS | ZX Spectron (Factory New)", "quantity": 2,
         "lots": [164, 234], "note": "中文名：法玛斯 | ZX81 彩色 (崭新出厂)"},
        {"market_hash_name": "AWP | Black Nile (Factory New)", "quantity": 1, "note": ""},
    ]

    def test_match_by_chinese_name(self):
        it = match_holding(self.ITEMS, "银白石英")
        self.assertEqual(it["market_hash_name"], "Five-SeveN | Silver Quartz (Factory New)")

    def test_match_by_chinese_name_with_spaces(self):
        self.assertIsNotNone(match_holding(self.ITEMS, "法玛斯 ZX81"))

    def test_match_by_english_name(self):
        it = match_holding(self.ITEMS, "AWP | Black Nile (Factory New)")
        self.assertEqual(it["quantity"], 1)

    def test_match_by_partial_english(self):
        self.assertIsNotNone(match_holding(self.ITEMS, "black nile"))

    def test_match_when_chinese_name_after_parenthesis(self):
        """回归：中文名里的辨识词可能在括号后面（SG 553 (纪念品) | 白骨），
        按括号截断就永远匹配不到。"""
        items = [{"market_hash_name": "SG 553 | Bleached (Factory New)", "quantity": 2,
                  "note": "中文名：SG 553 (纪念品) | 白骨 (崭新出厂)；用户 2026-09-24 决定卖出"}]
        self.assertIsNotNone(match_holding(items, "白骨"))
        self.assertIsNotNone(match_holding(items, "SG 553"))

    def test_cn_of_keeps_full_name(self):
        from tools.sell_item import cn_of
        self.assertEqual(cn_of("中文名：SG 553 (纪念品) | 白骨 (崭新出厂)；备注"),
                         "SG 553 (纪念品) | 白骨 (崭新出厂)")
        self.assertEqual(cn_of(""), "")
        self.assertEqual(cn_of("没有中文名"), "")

    def test_not_found(self):
        self.assertIsNone(match_holding(self.ITEMS, "不存在的饰品"))

    def test_normalize_strips_punctuation(self):
        self.assertEqual(normalize("AWP | Black Nile (Factory New)"), "awpblacknilefactorynew")


class TestComputeSale(unittest.TestCase):
    def test_whole_position_without_lots(self):
        item = {"market_hash_name": "X", "quantity": 2, "buy_price": 5.97}
        r = compute_sale(item, 2, 2.77, 0.01, None)
        self.assertEqual(r["cost"], 11.94)
        self.assertEqual(r["gross"], 5.54)
        self.assertEqual(r["fee"], 0.06)          # 5.54 × 1% = 0.0554 → 0.06
        self.assertEqual(r["net"], 5.48)
        self.assertEqual(r["pnl"], -6.46)
        self.assertEqual(r["remain_lots"], [])

    def test_partial_sale_recomputes_avg(self):
        """卖掉其中一件后，剩下那件的成本价必须重算，否则盈亏永久算错。"""
        item = {"market_head_name": "X", "market_hash_name": "X",
                "quantity": 2, "buy_price": 199.0, "lots": [164, 234]}
        r = compute_sale(item, 1, 162.8, 0.01, None)       # 默认先进先出 → 卖 ¥164 那件
        self.assertEqual(r["sold_lots"], [164.0])
        self.assertEqual(r["remain_lots"], [234.0])
        self.assertEqual(r["new_avg"], 234.0)
        self.assertEqual(r["cost"], 164.0)

    def test_lot_selection_by_price(self):
        item = {"market_hash_name": "X", "quantity": 2, "buy_price": 199.0, "lots": [164, 234]}
        r = compute_sale(item, 1, 162.8, 0.01, "234")
        self.assertEqual(r["sold_lots"], [234.0])
        self.assertEqual(r["remain_lots"], [164.0])
        self.assertEqual(r["new_avg"], 164.0)
        self.assertLess(r["pnl"], -70)                     # 卖高价那件 → 实亏更大

    def test_lot_count_mismatch_falls_back_to_avg(self):
        item = {"market_hash_name": "X", "quantity": 3, "buy_price": 100.0, "lots": [90, 110]}
        r = compute_sale(item, 2, 100.0, 0.01, None)
        self.assertEqual(r["cost"], 200.0)                 # 按均价 100 × 2

    def test_qty_exceeds_holding(self):
        item = {"market_hash_name": "X", "quantity": 1, "buy_price": 10.0}
        with self.assertRaises(SystemExit):
            compute_sale(item, 2, 10.0, 0.01, None)

    def test_unknown_lot_price(self):
        item = {"market_hash_name": "X", "quantity": 2, "buy_price": 199.0, "lots": [164, 234]}
        with self.assertRaises(SystemExit):
            compute_sale(item, 1, 100.0, 0.01, "999")

    def test_pnl_pct_uses_net_proceeds(self):
        item = {"market_hash_name": "X", "quantity": 1, "buy_price": 100.0}
        r = compute_sale(item, 1, 110.0, 0.01, None)
        self.assertEqual(r["net"], 108.9)                  # 110 - 1.10（卖出费 1%）
        self.assertEqual(r["pnl"], 8.9)                    # 净到手减成本，不是 10
        self.assertEqual(r["pnl_pct"], 8.9)


if __name__ == "__main__":
    unittest.main()
