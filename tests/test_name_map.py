"""名称映射（英文 → 中文）单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.datasource.name_map import NameResolver


class FakeClient:
    """模拟开放平台客户端：/base 返回中英对照，可统计调用次数。"""

    def __init__(self, items=None, fail=False):
        self.items = items or [
            {"name": "AK-47 | 红线 (久经沙场)", "marketHashName": "AK-47 | Redline (Field-Tested)"},
            {"name": "手套武器箱", "marketHashName": "Glove Case"},
        ]
        self.calls = 0
        self.fail = fail

    def _request(self, method, path, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("4005 限额")
        return {"success": True, "data": self.items}


class FakeScraper:
    def __init__(self, mapping=None, fail=False):
        self.mapping = mapping or {"AWP | Black Nile (Factory New)": "AWP | 尼罗河沃土 (崭新出厂)"}
        self.calls = 0
        self.fail = fail

    def item_detail(self, name):
        self.calls += 1
        if self.fail or name not in self.mapping:
            raise ValueError("页面无数据")
        return {"name": self.mapping[name]}


class TestNameResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "name_map.json"
        self.overrides = Path(self.tmp.name) / "overrides.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_fetch_and_cache(self):
        client = FakeClient()
        r = NameResolver(self.cache, client=client)
        out = r.resolve_many(["AK-47 | Redline (Field-Tested)", "Glove Case"])
        self.assertEqual(out["AK-47 | Redline (Field-Tested)"], "AK-47 | 红线 (久经沙场)")
        self.assertEqual(out["Glove Case"], "手套武器箱")
        self.assertEqual(client.calls, 1)
        # 第二次解析应命中缓存，不再调用接口
        r2 = NameResolver(self.cache, client=client)
        out2 = r2.resolve_many(["AK-47 | Redline (Field-Tested)"])
        self.assertEqual(out2["AK-47 | Redline (Field-Tested)"], "AK-47 | 红线 (久经沙场)")
        self.assertEqual(client.calls, 1)

    def test_overrides_priority(self):
        self.overrides.write_text(json.dumps(
            {"Glove Case": "手套箱（自定义）"}), encoding="utf-8")
        r = NameResolver(self.cache, overrides_path=self.overrides, client=FakeClient())
        out = r.resolve_many(["Glove Case"])
        self.assertEqual(out["Glove Case"], "手套箱（自定义）")

    def test_scraper_fallback(self):
        client = FakeClient()
        scraper = FakeScraper()
        r = NameResolver(self.cache, client=client, scraper=scraper)
        out = r.resolve_many(["AWP | Black Nile (Factory New)"])
        self.assertEqual(out["AWP | Black Nile (Factory New)"], "AWP | 尼罗河沃土 (崭新出厂)")
        self.assertEqual(scraper.calls, 1)

    def test_graceful_on_failures(self):
        client = FakeClient(fail=True)
        scraper = FakeScraper(fail=True)
        r = NameResolver(self.cache, client=client, scraper=scraper)
        out = r.resolve_many(["Unknown | Skin (Factory New)"])
        self.assertEqual(out["Unknown | Skin (Factory New)"], "")

    def test_cache_persisted(self):
        r = NameResolver(self.cache, client=FakeClient())
        r.resolve_many(["Glove Case"])
        data = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertIn("Glove Case", data["map"])


class TestReportDisplayName(unittest.TestCase):
    def test_display_name_and_text_fix(self):
        from cs2analyst.report.docx_report import ReportGenerator
        gen = ReportGenerator(name_map={
            "AWP | Black Nile (Factory New)": "AWP | 尼罗河沃土 (崭新出厂)"})
        self.assertEqual(gen._dn("AWP | Black Nile (Factory New)"),
                         "AWP | 尼罗河沃土 (崭新出厂)（AWP | Black Nile (Factory New)）")
        # 无中文对照时只显示英文
        self.assertEqual(gen._dn("Unknown | Skin (FN)"), "Unknown | Skin (FN)")
        # 文本内嵌名称替换
        txt = gen._fix_text("建议建仓 AWP | Black Nile (Factory New)，目标仓位 12%")
        self.assertIn("AWP | 尼罗河沃土 (崭新出厂)（AWP | Black Nile (Factory New)）", txt)


if __name__ == "__main__":
    unittest.main()
