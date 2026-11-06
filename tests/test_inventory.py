"""持仓加载与分件成本单元测试（离线）。"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.datasource.inventory import Holding, load_inventory


class TestHoldingLots(unittest.TestCase):
    def test_lot_prices_recorded(self):
        h = Holding(market_hash_name="X", quantity=2, buy_price=199.0, lots=[164, 234])
        self.assertEqual(h.lot_prices, [164.0, 234.0])
        self.assertTrue(h.lots_differ)

    def test_lot_prices_fall_back_to_average(self):
        h = Holding(market_hash_name="X", quantity=2, buy_price=199.0)
        self.assertEqual(h.lot_prices, [199.0, 199.0])
        self.assertFalse(h.lots_differ)

    def test_lot_count_mismatch_falls_back(self):
        """分件数对不上数量（如改了数量却忘了改 lots）→ 退回均价，避免算错。"""
        h = Holding(market_hash_name="X", quantity=3, buy_price=100.0, lots=[90, 110])
        self.assertEqual(h.lot_prices, [100.0, 100.0, 100.0])
        self.assertFalse(h.lots_differ)

    def test_same_lots_not_flagged_as_differing(self):
        h = Holding(market_hash_name="X", quantity=2, buy_price=92.5, lots=[92.5, 92.5])
        self.assertFalse(h.lots_differ)

    def test_sell_intent_from_status_or_note(self):
        self.assertTrue(Holding(market_hash_name="X", status="待售").sell_intent)
        self.assertTrue(Holding(market_hash_name="X", note="用户决定卖出（待售）").sell_intent)
        self.assertFalse(Holding(market_hash_name="X").sell_intent)
        self.assertFalse(Holding(market_hash_name="X", status="").sell_intent)

    def test_total_cost_uses_average(self):
        h = Holding(market_hash_name="X", quantity=2, buy_price=199.0, lots=[164, 234])
        self.assertEqual(h.total_cost, 398.0)      # 与分件之和一致


class TestVariantConsistency(unittest.TestCase):
    """名称版本一致性：备注说是纪念品、英文名却是普通版 → 必须警告。

    2026-09-24 真实踩到：注释写着「SG 553 (纪念品) | 白骨」，
    英文名填的是普通版，系统按 ¥2.79 报价（实际纪念品版 ¥1.28）。
    """

    def test_souvenir_mismatch_flagged(self):
        from cs2analyst.datasource.inventory import variant_warnings
        h = Holding(market_hash_name="SG 553 | Bleached (Factory New)",
                    note="中文名：SG 553 (纪念品) | 白骨 (崭新出厂)")
        self.assertTrue(variant_warnings(h))

    def test_souvenir_match_ok(self):
        from cs2analyst.datasource.inventory import variant_warnings
        h = Holding(market_hash_name="Souvenir SG 553 | Bleached (Factory New)",
                    note="中文名：SG 553（纪念品） | 白骨 (崭新出厂)")
        self.assertEqual(variant_warnings(h), [])

    def test_stattrak_and_knife(self):
        from cs2analyst.datasource.inventory import variant_warnings
        self.assertTrue(variant_warnings(Holding(
            market_hash_name="AK-47 | Redline (Field-Tested)", note="中文名：AK-47 | 红线（暗金）")))
        self.assertTrue(variant_warnings(Holding(
            market_hash_name="Karambit | Doppler (Factory New)", note="中文名：★ 蝴蝶刀 | 多普勒")))
        self.assertEqual(variant_warnings(Holding(
            market_hash_name="StatTrak™ AK-47 | Redline (Field-Tested)", note="暗金 红线")), [])

    def test_plain_item_no_warning(self):
        from cs2analyst.datasource.inventory import variant_warnings
        self.assertEqual(variant_warnings(Holding(
            market_hash_name="AWP | Black Nile (Factory New)",
            note="中文名：AWP | 尼罗河沃土 (崭新出厂)")), [])


class TestLoadInventory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inv.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_load_with_lots_and_status(self):
        self._write([
            {"market_hash_name": "A | Skin (Factory New)", "quantity": 2, "buy_price": 199.0,
             "lots": [164, 234], "status": "待售"},
            {"market_hash_name": "B | Skin (Factory New)", "quantity": 1, "buy_price": 10.0},
        ])
        hs = {h.market_hash_name: h for h in load_inventory(self.path)}
        self.assertEqual(hs["A | Skin (Factory New)"].lot_prices, [164.0, 234.0])
        self.assertTrue(hs["A | Skin (Factory New)"].sell_intent)
        self.assertFalse(hs["B | Skin (Factory New)"].sell_intent)

    def test_unknown_fields_ignored_not_crash(self):
        """以前 Holding(**it) 遇到未知字段会直接崩——现在忽略并告警。"""
        self._write([{"market_hash_name": "A | Skin (Factory New)", "quantity": 1,
                      "buy_price": 10.0, "_说明": "临时备注", "typo_field": 1}])
        hs = load_inventory(self.path)
        self.assertEqual(len(hs), 1)

    def test_missing_name_skipped(self):
        self._write([{"market_hash_name": "", "quantity": 1, "buy_price": 1.0},
                     {"market_hash_name": "A | Skin (Factory New)", "quantity": 1, "buy_price": 1.0}])
        self.assertEqual(len(load_inventory(self.path)), 1)

    def test_csv_with_lots(self):
        """CSV 里 lots 本身含逗号，必须用引号包起来，否则会被当成多出来的列。"""
        p = Path(self.tmp.name) / "inv.csv"
        p.write_text('market_hash_name,quantity,buy_price,lots,status\n'
                     'A | Skin (Factory New),2,199,"164,234",待售\n', encoding="utf-8")
        h = load_inventory(p)[0]
        self.assertEqual(h.lot_prices, [164.0, 234.0])
        self.assertTrue(h.sell_intent)

    def test_csv_without_quotes_degrades_safely(self):
        """忘记加引号时不能算错：分件数对不上 → 退回均价。"""
        p = Path(self.tmp.name) / "inv2.csv"
        p.write_text("market_hash_name,quantity,buy_price,lots,status\n"
                     "A | Skin (Factory New),2,199,164,234\n", encoding="utf-8")
        h = load_inventory(p)[0]
        self.assertEqual(h.lot_prices, [199.0, 199.0])


if __name__ == "__main__":
    unittest.main()
