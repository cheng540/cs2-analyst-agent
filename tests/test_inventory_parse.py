"""粘贴文本 → 持仓候选：解析器测试。

这层的定位是「给出初值 + 标出可疑点，人确认后才入库」，所以测试除了验证解析结果，
还要验证**该报警的地方确实报警**——漏报比解析错更危险（错的数字会静默进报告）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.inventory_parse import (
    load_name_lookup, parse_inventory_text, parse_line,
)


class ParseLineTest(unittest.TestCase):
    def test_tab_separated(self):
        item = parse_line("AK-47 | Redline (Field-Tested)\t1\t169.00")
        self.assertEqual(item["market_hash_name"], "AK-47 | Redline (Field-Tested)")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["buy_price"], 169.0)
        self.assertEqual(item["confidence"], "high")

    def test_currency_and_x_prefix(self):
        item = parse_line("AWP | Asiimov (Battle-Scarred) x2 ¥128.5")
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["buy_price"], 128.5)

    def test_quantity_with_unit(self):
        item = parse_line("USP-S | Printstream (Factory New) 237.9 3件")
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["buy_price"], 237.9)

    def test_total_price_three_numbers(self):
        """「3 92 276」= 3 件 × 92 = 276，别把 276 当单价。"""
        item = parse_line("Glove Case 3 92 276")
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["buy_price"], 92.0)
        self.assertTrue(any("3 件" in w for w in item["warnings"]))

    def test_two_numbers_qty_then_price(self):
        item = parse_line("AK-47 | Vulcan (Field-Tested) 2 1690")
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["buy_price"], 1690.0)

    def test_big_integer_is_price_not_qty(self):
        """单价 1690 但没写数量时，不能把 1690 当数量。"""
        item = parse_line("AK-47 | Redline (Field-Tested) 1690")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["buy_price"], 1690.0)

    def test_small_single_integer_warns(self):
        item = parse_line("P250 | Sand Dune (Factory New) 5")
        self.assertTrue(any("数量" in w for w in item["warnings"]))

    def test_missing_wear_warns_for_skins_only(self):
        item = parse_line("AK-47 | Redline 169")
        self.assertTrue(any("磨损档" in w for w in item["warnings"]))
        # 武器箱没有磨损档，不该报
        case = parse_line("Glove Case 2 92")
        self.assertNotIn("磨损档", "".join(case["warnings"]))

    def test_chinese_with_ascii_warns(self):
        item = parse_line("AK-47 | 红线 (久经沙场) 169")
        self.assertTrue(any("中英文混排" in w or "官方英文名" in w for w in item["warnings"]))

    def test_pure_chinese_is_low(self):
        item = parse_line("随便写的中文名 100")
        self.assertEqual(item["confidence"], "low")
        self.assertEqual(item["matched_by"], "未识别")

    def test_armed_variant_conflict(self):
        """备注写纪念品、英文名不是 Souvenir → 必须提示（历史踩过的坑）。"""
        item = parse_line("纪念品 AK-47 | Redline (Field-Tested) 169")
        joined = "".join(item["warnings"])
        self.assertIn("纪念品", joined)

    def test_header_returns_none(self):
        self.assertIsNone(parse_line("名称\t数量\t单价"))
        self.assertIsNone(parse_line("饰品 名称 价格 数量"))

    def test_decor_lines_skipped(self):
        for line in ("★★★ 分隔线 ★★★", "=====", "----------", "备注：以下是我手上的"):
            self.assertIsNone(parse_line(line), line)

    def test_empty(self):
        self.assertIsNone(parse_line("   "))

    def test_fullwidth_conversion(self):
        item = parse_line("ＡＫ－４７ | Redline (Field-Tested)　１　１６９")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["buy_price"], 169.0)


class ParseTextTest(unittest.TestCase):
    def test_mixed_block(self):
        text = """
名称\t数量\t单价
AK-47 | Redline (Field-Tested) 1 169
AWP | Asiimov (Battle-Scarred) x1 ¥128.5
★★★ 分隔线 ★★★
Glove Case 3 92 276
"""
        result = parse_inventory_text(text)
        names = [i["market_hash_name"] for i in result["items"]]
        self.assertEqual(len(names), 3)
        self.assertEqual(result["summary"]["skipped_lines"], 2)   # 表头 + 装饰行

    def test_merge_multiple_batches(self):
        """同名多行 = 分批买入：数量相加、均价、逐件成本。"""
        text = "Glove Case 2 92\nGlove Case 1 90\n"
        result = parse_inventory_text(text)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["lots"], [92.0, 92.0, 90.0])
        self.assertAlmostEqual(item["buy_price"], 91.33, places=2)
        self.assertTrue(any("合并" in w for w in item["warnings"]))

    def test_same_price_merge_has_no_lots_noise(self):
        text = "AK-47 | Redline (Field-Tested) 1 169\nAK-47 | Redline (Field-Tested) 1 169\n"
        item = parse_inventory_text(text)["items"][0]
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["buy_price"], 169.0)

    def test_missing_price_counted(self):
        text = "AK-47 | Redline (Field-Tested)\nAWP | Asiimov (Battle-Scarred) 128\n"
        summary = parse_inventory_text(text)["summary"]
        self.assertEqual(summary["missing_price"], 1)
        self.assertEqual(summary["total"], 2)

    def test_need_review_counts_non_high(self):
        text = "AK-47 | Redline (Field-Tested) 1 169\n随便写的中文名 100\n"
        summary = parse_inventory_text(text)["summary"]
        self.assertEqual(summary["need_review"], 1)

    def test_empty_text(self):
        result = parse_inventory_text("")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["summary"]["total"], 0)

    def test_lots_dropped_when_mismatch(self):
        """一行有价、一行没价 → 合并后 lots 与数量对不上，应丢弃让系统退回均价。"""
        text = "Glove Case 2 92\nGlove Case 1\n"
        item = parse_inventory_text(text)["items"][0]
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item.get("lots"), [])


class NameLookupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_two_source_shapes(self):
        self._write("data/name_map.json",
                    {"map": {"AK-47 | Redline (Field-Tested)": "AK-47 | 红线 (久经沙场)"}})
        self._write("config/name_overrides.json",
                    {"Glove Case": "手套武器箱", "_说明": "手工补充"})
        lookup = load_name_lookup(self.root)
        self.assertEqual(lookup.get("AK-47 | 红线 (久经沙场)"), "AK-47 | Redline (Field-Tested)")
        self.assertEqual(lookup.get("手套武器箱"), "Glove Case")
        self.assertNotIn("_说明", lookup.values())

    def test_lookup_without_wear_suffix(self):
        """用户常只写「AK-47 | 红线」不带磨损，也要能反查到。"""
        self._write("data/name_map.json",
                    {"map": {"AK-47 | Redline (Field-Tested)": "AK-47 | 红线 (久经沙场)"}})
        item = parse_line("AK-47 | 红线 169", load_name_lookup(self.root))
        self.assertEqual(item["market_hash_name"], "AK-47 | Redline (Field-Tested)")
        self.assertEqual(item["matched_by"], "中文名反查")
        self.assertEqual(item["confidence"], "high")

    def test_missing_files_are_fine(self):
        self.assertEqual(load_name_lookup(self.root), {})

    def test_broken_json_is_fine(self):
        path = self.root / "data" / "name_map.json"
        path.parent.mkdir(parents=True)
        path.write_text("{不是 json", encoding="utf-8")
        self.assertEqual(load_name_lookup(self.root), {})


if __name__ == "__main__":
    unittest.main()
