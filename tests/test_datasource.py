"""持仓加载与数据解析单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from cs2analyst.datasource.inventory import load_inventory
from cs2analyst.datasource.steamdt import (SteamDTClient, _safe_name,
                                           parse_kline_rows)


class TestInventoryLoad(unittest.TestCase):
    def test_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "inv.json"
            p.write_text(json.dumps([
                {"market_hash_name": "AK-47 | Redline (Field-Tested)", "quantity": 2,
                 "buy_price": 96.5, "wear": "FT"},
                {"market_hash_name": "AWP | Asiimov (BS)", "quantity": 1, "buy_price": 268},
            ], ensure_ascii=False), encoding="utf-8")
            items = load_inventory(p)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].quantity, 2)
            self.assertEqual(items[0].total_cost, 193.0)

    def test_csv(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "inv.csv"
            p.write_text(
                "market_hash_name,wear,quantity,buy_price,buy_date,platform,special_style,note\n"
                "AK-47 | Redline (FT),FT,3,96.5,2026-03-01,YOUPIN,,\n",
                encoding="utf-8-sig")
            items = load_inventory(p)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].quantity, 3)
            self.assertEqual(items[0].wear, "FT")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_inventory(Path(tempfile.gettempdir()) / "not_exist_inventory.json")

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "inv.json"
            p.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_inventory(p)


class TestParseKlineRows(unittest.TestCase):
    def test_basic(self):
        rows = [[1000, 10, 11, 12, 9], [2000, 11, 12, 13, 10]]
        df = parse_kline_rows(rows)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df["ts"]), [1000, 2000])
        self.assertEqual(df.iloc[-1]["close"], 12)

    def test_ms_timestamps(self):
        rows = [[1000000000000, 10, 11, 12, 9]]
        df = parse_kline_rows(rows)
        self.assertEqual(df.iloc[0]["ts"], 1000000000)

    def test_dedupe_and_sort(self):
        rows = [[2000, 1, 2, 3, 4], [1000, 5, 6, 7, 8], [2000, 9, 9, 9, 9]]
        df = parse_kline_rows(rows)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[-1]["close"], 9)  # 后者覆盖

    def test_empty_and_bad_rows(self):
        self.assertTrue(parse_kline_rows([]).empty)
        df = parse_kline_rows([[None, 1, 2, 3, 4], [1, 2, 3], ["x", 1, 2, 3, 4]])
        self.assertTrue(df.empty)

    def test_safe_name(self):
        self.assertEqual(_safe_name('★ Karambit | Doppler (Factory New)'),
                         '__Karambit___Doppler__Factory_New_')


class TestOfflineClient(unittest.TestCase):
    def test_missing_offline_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            client = SteamDTClient({"api_key": ""}, offline_dir=Path(td))
            with self.assertRaises(RuntimeError):
                client.item_kline("Some | Item (FN)", 2)

    def test_offline_never_hits_network(self):
        with tempfile.TemporaryDirectory() as td:
            client = SteamDTClient({"api_key": "", "base_url": "http://127.0.0.1:1",
                                    "timeout": 1}, offline_dir=Path(td))
            with self.assertRaises(RuntimeError):
                client.batch_price(["X"])


class TestMarketAnalysis(unittest.TestCase):
    def test_insufficient_data_defaults(self):
        from cs2analyst.analysis.market import analyze_market
        empty = pd.DataFrame(columns=["ts", "open", "close", "high", "low"])
        env = analyze_market(empty, empty, {}, [5, 10, 20], {})
        self.assertEqual(env.trend, "数据不足")
        self.assertEqual(env.risk, "中风险")


if __name__ == "__main__":
    unittest.main()
