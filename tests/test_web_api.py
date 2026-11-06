"""Web 控制台 API 测试（全部在临时目录跑，绝不碰真实 config.yaml）。

重点锁定交接文档里的硬约束：
- 密钥不回传明文；
- 写接口只对 127.0.0.1 开放，线上只读模式一律 403；
- 非法配置项/超范围值/掩码值反传都要被拒绝，且**不落盘**；
- 持仓增删改走白名单字段。
"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.web.api import ApiError, ConsoleApi, coerce_config_value

CONFIG_YAML = """# 测试配置
steamdt:
  api_key: 'steamdt-real-key-abcdef'      # SteamDT Key
  timeout: 20
llm:
  api_key: ''
  model: deepseek-flash
  enabled: true
notify:
  enabled: true
  wecom_webhook: ''
alerts:
  enabled: true
  times:
  - 9
  - 12
  - 22
decision:
  stop_loss_pct: 0.3
sim:
  initial_capital: 3000.0
"""

INVENTORY = [{
    "market_hash_name": "Test | Item (Factory New)",
    "wear": "FN", "quantity": 2, "buy_price": 100.0, "lots": [90.0, 110.0],
    "buy_date": "", "platform": "YOUPIN", "special_style": "", "note": "", "unlocked": True,
}]

ANALYSIS = {
    "date": "2026-01-05",
    "market": {"趋势": "下降趋势", "风险等级": "高风险", "量能": "平量",
               "大盘指数": 700.0, "较昨日%": -0.5, "情绪分": 45.0, "操作基调": "不补仓"},
    "portfolio": {"总成本": 200.0, "总市值": 180.0, "总盈亏": -20.0,
                  "盈亏%": -10.0, "条目数": 1, "件数": 2},
    "items": [{
        "饰品": "Test | Item (Factory New)", "中文名": "测试饰品", "数量": 2,
        "成本价": 100.0, "现价": 90.0, "盈亏%": -10.0, "趋势": "下降趋势",
        "位置": "低位（10%）", "流动性": "高", "量价": "缩量",
        "系统建议": "🟡 持有观望", "信心度": 2, "最近支撑": 85.0, "最近压力": 105.0,
        "理由": ["低位"], "目标仓位%": None,
    }],
    "watchlist": [],
}


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._write("config/config.yaml", CONFIG_YAML)
        self._write("config/inventory.json", json.dumps(INVENTORY, ensure_ascii=False))
        self._write("data/analysis/2026-01-05.json", json.dumps(ANALYSIS, ensure_ascii=False))
        self._write("data/llm/2026-01-05.json", json.dumps(
            {"date": "2026-01-05", "market_view": "继续下跌", "portfolio_advice": "别补仓",
             "risk_warnings": ["高风险"], "model": "deepseek-flash"}, ensure_ascii=False))
        self._write("data/market/broad_kline_2.json", json.dumps([
            [1767571200, 710, 712, 715, 708],
            [1767657600, 712, 705, 713, 700],
        ]))
        self._write("data/snapshots/2026-01-05_prices.json", json.dumps({
            "_date": "2026-01-05",
            "Test | Item (Factory New)": [
                {"platform": "C5", "sellPrice": 90.0}, {"platform": "YOUPIN", "sellPrice": 88.0}],
        }, ensure_ascii=False))
        self._write("data/sim/state_a.json", json.dumps({
            "initial_capital": 3000.0, "cash": 3000.0, "positions": {},
            "journal": [{"net_value": 3000.0, "thoughts": "无操作"}]}, ensure_ascii=False))
        self._write("reports/报告_2026-01-05.docx", "fake-docx")
        self.api = ConsoleApi(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def call(self, method, path, query=None, body=None, ip="127.0.0.1"):
        return self.api.dispatch(method, path, query or {}, body or {}, ip)


class TestReadEndpoints(ApiTestBase):
    def test_health(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["latest_analysis"], "2026-01-05")

    def test_summary_shape(self):
        status, data = self.call("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(data["market"]["trend"], "下降趋势")
        self.assertEqual(data["portfolio"]["cost"], 200.0)
        self.assertTrue(data["signals"])
        self.assertEqual(data["judgement"]["market_view"], "继续下跌")

    def test_summary_without_snapshot_returns_404(self):
        for f in (self.root / "data" / "analysis").glob("*.json"):
            f.unlink()
        self.api = ConsoleApi(self.root)          # 清缓存
        status, data = self.call("GET", "/api/summary")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_holdings_merges_inventory_flags(self):
        status, data = self.call("GET", "/api/holdings")
        self.assertEqual(status, 200)
        row = data["rows"][0]
        self.assertEqual(row["name_cn"], "测试饰品")
        self.assertTrue(row["unlocked"])
        self.assertEqual(row["lots"], [90.0, 110.0])
        self.assertEqual(row["support"], 85.0)

    def test_history_series(self):
        status, data = self.call("GET", "/api/history", {"days": 7})
        self.assertEqual(status, 200)
        self.assertTrue(data["market"]["dates"])
        self.assertEqual(len(data["market"]["dates"]), len(data["market"]["index"]))
        self.assertTrue(data["portfolio"]["dates"])
        self.assertEqual(data["portfolio"]["value"][0], 176.0)   # 88 × 2
        self.assertIn("basis", data["portfolio"])

    def test_reports_and_sim(self):
        self.assertTrue(self.call("GET", "/api/reports")[1]["reports"])
        sim = self.call("GET", "/api/sim")[1]
        self.assertTrue(sim["tracks"][0]["available"])
        self.assertFalse(sim["tracks"][1]["available"])

    def test_unknown_endpoint_404(self):
        status, _ = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)


class TestSecretsNeverLeak(ApiTestBase):
    def test_config_view_masks_keys(self):
        status, data = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("steamdt-real-key-abcdef", blob)
        self.assertIn("****", blob)

    def test_config_view_marks_unset_secret(self):
        item = self.call("GET", "/api/config")[1]["groups"]["大模型（DeepSeek）"]["llm.api_key"]
        self.assertFalse(item["set"])
        self.assertEqual(item["value"], "")


class TestWriteGuards(ApiTestBase):
    def test_non_local_ip_forbidden(self):
        status, data = self.call("POST", "/api/config",
                                 body={"updates": {"steamdt.timeout": 30}}, ip="10.0.0.7")
        self.assertEqual(status, 403)
        self.assertIn("本机", data["error"])

    def test_readonly_mode_forbidden(self):
        api = ConsoleApi(self.root, readonly=True)
        status, _ = api.dispatch("POST", "/api/config", {},
                                 {"updates": {"steamdt.timeout": 30}}, "127.0.0.1")
        self.assertEqual(status, 403)

    def test_ipv4_mapped_loopback_allowed(self):
        status, _ = self.call("POST", "/api/config",
                              body={"updates": {"steamdt.timeout": 30}}, ip="::ffff:127.0.0.1")
        self.assertEqual(status, 200)

    def test_inventory_write_blocked_for_remote(self):
        status, _ = self.call("DELETE", "/api/inventory", {"index": 0}, ip="192.168.1.9")
        self.assertEqual(status, 403)
        self.assertEqual(len(json.loads((self.root / "config" / "inventory.json")
                                        .read_text(encoding="utf-8"))), 1)


class TestConfigWrite(ApiTestBase):
    def test_write_updates_value_keeps_comment(self):
        status, data = self.call("POST", "/api/config", body={"updates": {"steamdt.timeout": 45}})
        self.assertEqual(status, 200)
        text = (self.root / "config" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("timeout: 45", text)
        self.assertIn("# SteamDT Key", text)
        self.assertEqual(self.api.cfg().steamdt["timeout"], 45)

    def test_unknown_key_rejected(self):
        status, data = self.call("POST", "/api/config",
                                 body={"updates": {"paths.data_dir": "/etc"}})
        self.assertEqual(status, 400)
        self.assertIn("不允许", data["error"])

    def test_out_of_range_rejected_and_not_written(self):
        before = (self.root / "config" / "config.yaml").read_text(encoding="utf-8")
        status, _ = self.call("POST", "/api/config",
                              body={"updates": {"decision.stop_loss_pct": 5}})
        self.assertEqual(status, 400)
        self.assertEqual(before, (self.root / "config" / "config.yaml").read_text(encoding="utf-8"))

    def test_masked_value_rejected(self):
        status, data = self.call("POST", "/api/config",
                                 body={"updates": {"steamdt.api_key": "****abcd"}})
        self.assertEqual(status, 400)
        self.assertIn("掩码", data["error"])

    def test_backup_created_before_write(self):
        self.call("POST", "/api/config", body={"updates": {"steamdt.timeout": 33}})
        self.assertTrue(list((self.root / "config").glob("config.yaml.bak.*")))

    def test_empty_updates_rejected(self):
        self.assertEqual(self.call("POST", "/api/config", body={"updates": {}})[0], 400)

    def test_two_writes_in_same_second_keep_two_backups(self):
        """同一秒内连写两次，不能把前一个备份覆盖掉（否则唯一回滚点丢了）。"""
        self.call("POST", "/api/config", body={"updates": {"steamdt.timeout": 31}})
        self.call("POST", "/api/config", body={"updates": {"steamdt.timeout": 32}})
        self.assertGreaterEqual(len(list((self.root / "config").glob("config.yaml.bak.*"))), 2)


class TestConfigCoercion(unittest.TestCase):
    def test_bool_variants(self):
        for value in (True, 1, "是", "on", "true"):
            self.assertIs(coerce_config_value("notify.enabled", value), True)
        for value in (False, 0, "否", "off", ""):
            self.assertIs(coerce_config_value("notify.enabled", value), False)

    def test_intlist_from_string(self):
        self.assertEqual(coerce_config_value("alerts.times", "9,12，22"), [9, 12, 22])

    def test_intlist_dedup_and_sort(self):
        self.assertEqual(coerce_config_value("alerts.times", [22, 9, 22]), [9, 22])

    def test_intlist_out_of_range(self):
        with self.assertRaises(ApiError):
            coerce_config_value("alerts.times", [9, 25])

    def test_bad_number(self):
        with self.assertRaises(ApiError):
            coerce_config_value("steamdt.timeout", "abc")

    def test_time_format(self):
        self.assertEqual(coerce_config_value("reports.daily_time", "9:5"), "09:05")
        self.assertEqual(coerce_config_value("reports.noon_time", "12：10"), "12:10")
        with self.assertRaises(ApiError):
            coerce_config_value("reports.daily_time", "25:00")
        with self.assertRaises(ApiError):
            coerce_config_value("reports.daily_time", "下午五点")

    def test_weekly_day_range(self):
        self.assertEqual(coerce_config_value("reports.weekly_day", 0), 0)
        self.assertEqual(coerce_config_value("reports.weekly_day", 6), 6)
        with self.assertRaises(ApiError):
            coerce_config_value("reports.weekly_day", 7)

    def test_interval_range(self):
        self.assertEqual(coerce_config_value("alerts.interval_minutes", 30), 30)
        with self.assertRaises(ApiError):
            coerce_config_value("alerts.interval_minutes", 5)      # 低于下限 15
        with self.assertRaises(ApiError):
            coerce_config_value("alerts.interval_minutes", 5000)


class TestInventoryCrud(ApiTestBase):
    NEW_ITEM = {"market_hash_name": "New | Skin (Minimal Wear)", "quantity": 1,
                "buy_price": 50, "unlocked": False, "note": "新买的"}

    def test_add(self):
        status, data = self.call("POST", "/api/inventory", body={"item": self.NEW_ITEM})
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 2)
        items = self.call("GET", "/api/inventory")[1]["items"]
        self.assertEqual(items[1]["market_hash_name"], "New | Skin (Minimal Wear)")
        self.assertIs(items[1]["unlocked"], False)

    def test_add_requires_name(self):
        status, _ = self.call("POST", "/api/inventory", body={"item": {"quantity": 1}})
        self.assertEqual(status, 400)

    def test_replace(self):
        status, _ = self.call("PUT", "/api/inventory",
                              body={"index": 0, "item": {**INVENTORY[0], "quantity": 3}})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/inventory")[1]["items"][0]["quantity"], 3)

    def test_delete(self):
        self.call("POST", "/api/inventory", body={"item": self.NEW_ITEM})
        status, data = self.call("DELETE", "/api/inventory", {"index": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)

    def test_delete_out_of_range(self):
        self.assertEqual(self.call("DELETE", "/api/inventory", {"index": 99})[0], 404)

    def test_unknown_fields_ignored(self):
        item = dict(self.NEW_ITEM, evil_field="x")
        self.call("POST", "/api/inventory", body={"item": item})
        stored = self.call("GET", "/api/inventory")[1]["items"][-1]
        self.assertNotIn("evil_field", stored)


class TestAsk(ApiTestBase):
    def test_ask_without_key_gives_actionable_error(self):
        status, data = self.call("POST", "/api/ask", body={"question": "大盘怎么样"})
        self.assertEqual(status, 400)
        self.assertIn("DeepSeek", data["error"])

    def test_empty_question(self):
        self.assertEqual(self.call("POST", "/api/ask", body={"question": "  "})[0], 400)


class TestSetupWizard(ApiTestBase):
    def test_status_reports_missing_keys(self):
        data = self.call("GET", "/api/setup")[1]
        self.assertFalse(data["checks"]["llm_key"])
        self.assertIn("llm_key", data["missing"])
        self.assertFalse(data["configured"])

    def test_apply_writes_keys_and_inventory(self):
        body = {
            "steamdt.api_key": "steamdt-new",
            "llm.api_key": "sk-new-key-1234",
            "notify.wecom_webhook": "https://qyapi.example/x",
            "notify.enabled": True,
            "alerts.enabled": True,
            "alerts.times": [9, 12, 22],
            "alerts.summary_times": [12, 22],
            "inventory": [{"market_hash_name": "A | B (Factory New)", "quantity": 1,
                           "buy_price": 10, "unlocked": True}],
        }
        status, data = self.call("POST", "/api/setup", body=body)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["inventory_count"], 1)
        self.assertFalse(data["missing"])
        cfg = self.api.cfg()
        self.assertEqual(cfg.llm["api_key"], "sk-new-key-1234")
        self.assertEqual(cfg.alerts["times"], [9, 12, 22])
        # 返回值里不能回显密钥
        self.assertNotIn("sk-new-key-1234", json.dumps(data, ensure_ascii=False))

    def test_apply_rejects_bad_inventory_without_writing(self):
        before = (self.root / "config" / "config.yaml").read_text(encoding="utf-8")
        status, _ = self.call("POST", "/api/setup", body={
            "llm.api_key": "sk-whatever-1234",
            "inventory": [{"quantity": 1}],          # 缺 market_hash_name
        })
        self.assertEqual(status, 400)
        self.assertEqual(before, (self.root / "config" / "config.yaml").read_text(encoding="utf-8"))

    def test_blank_secret_means_keep(self):
        self.call("POST", "/api/config", body={"updates": {"llm.api_key": "sk-keep-me-9999"}})
        self.call("POST", "/api/setup", body={"llm.api_key": "", "alerts.enabled": False})
        self.assertEqual(self.api.cfg().llm["api_key"], "sk-keep-me-9999")


class TestKnowledgeApi(ApiTestBase):
    def test_stats_empty(self):
        status, data = self.call("GET", "/api/knowledge/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["documents"], 0)

    def test_search_empty_query(self):
        status, data = self.call("GET", "/api/knowledge/search", {"q": ""})
        self.assertEqual(status, 200)
        self.assertIn("error", data)


class TestWatchlist(ApiTestBase):
    """监控标的（观察池）：客户自己加/删要监控的饰品。"""

    def test_empty_list(self):
        status, data = self.call("GET", "/api/watchlist")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.assertIn("持仓会自动监控", data["hint"])

    def test_add_and_persist(self):
        status, data = self.call("POST", "/api/watchlist",
                                 body={"market_hash_name": "AK-47 | Redline (Field-Tested)"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 1)
        stored = json.loads((self.root / "config" / "sim_watchlist.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(stored["items"][0]["market_hash_name"], "AK-47 | Redline (Field-Tested)")
        self.assertEqual(self.call("GET", "/api/watchlist")[1]["count"], 1)

    def test_add_keeps_existing_description(self):
        (self.root / "config" / "sim_watchlist.json").write_text(
            json.dumps({"说明": "我的观察池", "items": []}, ensure_ascii=False), encoding="utf-8")
        self.call("POST", "/api/watchlist", body={"name": "A | B (Factory New)"})
        stored = json.loads((self.root / "config" / "sim_watchlist.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(stored["说明"], "我的观察池")
        self.assertEqual(len(stored["items"]), 1)

    def test_duplicate_rejected(self):
        self.call("POST", "/api/watchlist", body={"name": "A | B (Factory New)"})
        status, data = self.call("POST", "/api/watchlist", body={"name": "A | B (Factory New)"})
        self.assertEqual(status, 400)
        self.assertIn("已经在监控列表", data["error"])

    def test_empty_name_rejected(self):
        self.assertEqual(self.call("POST", "/api/watchlist", body={"name": "  "})[0], 400)

    def test_delete(self):
        self.call("POST", "/api/watchlist", body={"name": "A | B (Factory New)"})
        status, data = self.call("DELETE", "/api/watchlist", {"name": "A | B (Factory New)"})
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)

    def test_delete_missing(self):
        self.assertEqual(self.call("DELETE", "/api/watchlist", {"name": "Not | There"})[0], 404)

    def test_limit(self):
        from cs2analyst.web.api import MAX_WATCHLIST
        items = [{"market_hash_name": f"Item {i} (Factory New)"} for i in range(MAX_WATCHLIST)]
        (self.root / "config" / "sim_watchlist.json").write_text(
            json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
        status, data = self.call("POST", "/api/watchlist", body={"name": "One More"})
        self.assertEqual(status, 400)
        self.assertIn(str(MAX_WATCHLIST), data["error"])

    def test_readonly_forbidden(self):
        api = ConsoleApi(self.root, readonly=True)
        status, _ = api.dispatch("POST", "/api/watchlist", {}, {"name": "X | Y (FN)"}, "127.0.0.1")
        self.assertEqual(status, 403)

    def test_remote_forbidden(self):
        status, _ = self.call("POST", "/api/watchlist", body={"name": "X | Y (FN)"},
                              ip="192.168.1.20")
        self.assertEqual(status, 403)

    def test_backup_created(self):
        self.call("POST", "/api/watchlist", body={"name": "A | B (Factory New)"})
        self.call("POST", "/api/watchlist", body={"name": "C | D (Factory New)"})
        self.assertTrue(list((self.root / "config").glob("sim_watchlist.json.bak.*")))

    def test_legacy_plain_array_tolerated(self):
        (self.root / "config" / "sim_watchlist.json").write_text(
            json.dumps(["Old | Item"]), encoding="utf-8")
        data = self.call("GET", "/api/watchlist")[1]
        self.assertEqual(data["count"], 1)
        self.call("POST", "/api/watchlist", body={"name": "New | Item"})
        stored = json.loads((self.root / "config" / "sim_watchlist.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(len(stored["items"]), 2)


class TestPlanEndpoint(ApiTestBase):
    def test_plan_shape(self):
        status, data = self.call("GET", "/api/plan")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["plans"]), 3)
        self.assertIn("interval_minutes", data["alerts"])
        self.assertTrue(data["lines"])

    def test_plan_reflects_config(self):
        self.call("POST", "/api/config", body={"updates": {"reports.noon_enabled": False,
                                                           "alerts.interval_minutes": 30}})
        data = self.call("GET", "/api/plan")[1]
        noon = [p for p in data["plans"] if p["kind"] == "noon"][0]
        self.assertFalse(noon["enabled"])
        self.assertEqual(data["alerts"]["interval_minutes"], 30)


class TestPasteImport(ApiTestBase):
    """粘贴文本 → 预览 → 确认导入。解析接口绝不能写库，写库必须走 bulk。"""

    TEXT = ("AK-47 | Redline (Field-Tested) 1 169\n"
            "手套武器箱 2 92\n")

    def _inventory(self):
        return json.loads((self.root / "config" / "inventory.json").read_text(encoding="utf-8"))

    def test_parse_does_not_write(self):
        before = self._inventory()
        status, data = self.call("POST", "/api/inventory/parse", body={"text": self.TEXT})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertTrue(data["items"][0]["market_hash_name"])
        self.assertEqual(before, self._inventory(), "解析接口不应该改持仓")

    def test_parse_marks_existing(self):
        # 夹具里的持仓是 Test | Item (Factory New)
        status, data = self.call("POST", "/api/inventory/parse",
                                 body={"text": "Test | Item (Factory New) 1 100"})
        self.assertEqual(status, 200)
        self.assertTrue(data["items"][0]["exists"])
        self.assertEqual(data["summary"]["already_exists"], 1)

    def test_parse_empty_text(self):
        self.assertEqual(self.call("POST", "/api/inventory/parse", body={"text": "  "})[0], 400)

    def test_parse_garbage(self):
        status, _ = self.call("POST", "/api/inventory/parse", body={"text": "=====\n----\n"})
        self.assertEqual(status, 400)

    def test_parse_too_long(self):
        status, _ = self.call("POST", "/api/inventory/parse", body={"text": "x " * 20000})
        self.assertEqual(status, 400)

    def test_bulk_append(self):
        status, data = self.call("POST", "/api/inventory/bulk", body={
            "mode": "append",
            "items": [{"market_hash_name": "New | Skin (Minimal Wear)", "quantity": 2, "buy_price": 50}],
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(self._inventory()), 2)
        self.assertTrue(list((self.root / "config").glob("inventory.json.bak.*")))

    def test_bulk_append_duplicate_rejected(self):
        status, data = self.call("POST", "/api/inventory/bulk", body={
            "mode": "append",
            "items": [{"market_hash_name": "Test | Item (Factory New)", "quantity": 1, "buy_price": 1}],
        })
        self.assertEqual(status, 400)
        self.assertIn("已经存在", data["error"])
        self.assertEqual(len(self._inventory()), 1)

    def test_bulk_replace(self):
        status, data = self.call("POST", "/api/inventory/bulk", body={
            "mode": "replace",
            "items": [{"market_hash_name": "Only | One (Factory New)", "quantity": 1, "buy_price": 9}],
        })
        self.assertEqual(status, 200, data)
        items = self._inventory()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["market_hash_name"], "Only | One (Factory New)")

    def test_bulk_invalid_item_writes_nothing(self):
        before = self._inventory()
        status, _ = self.call("POST", "/api/inventory/bulk", body={
            "mode": "append",
            "items": [{"market_hash_name": "Ok | Item (FN)", "quantity": 1, "buy_price": 1},
                      {"quantity": 1}],                      # 缺名称 → 整单拒绝
        })
        self.assertEqual(status, 400)
        self.assertEqual(before, self._inventory())

    def test_bulk_bad_mode(self):
        status, _ = self.call("POST", "/api/inventory/bulk",
                              body={"mode": "merge", "items": [{"market_hash_name": "A | B (FN)"}]})
        self.assertEqual(status, 400)

    def test_bulk_empty(self):
        self.assertEqual(self.call("POST", "/api/inventory/bulk", body={"items": []})[0], 400)

    def test_bulk_keeps_lots(self):
        self.call("POST", "/api/inventory/bulk", body={
            "mode": "append",
            "items": [{"market_hash_name": "Lot | Item (FN)", "quantity": 2, "buy_price": 91.5,
                       "lots": [92, 91]}],
        })
        item = self._inventory()[-1]
        self.assertEqual(item["lots"], [92.0, 91.0])

    def test_remote_forbidden(self):
        status, _ = self.call("POST", "/api/inventory/parse", body={"text": self.TEXT},
                              ip="10.1.2.3")
        self.assertEqual(status, 403)

    def test_readonly_forbidden(self):
        api = ConsoleApi(self.root, readonly=True)
        status, _ = api.dispatch("POST", "/api/inventory/bulk", {},
                                 {"mode": "append", "items": [{"market_hash_name": "A | B (FN)"}]},
                                 "127.0.0.1")
        self.assertEqual(status, 403)


class TestVisionImport(ApiTestBase):
    """截图识别走 deepseek-flash 多模态；模型调用在测试里被替换掉，不花钱。"""

    PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

    def _with_llm_key(self):
        text = (self.root / "config" / "config.yaml").read_text(encoding="utf-8")
        text = text.replace("  api_key: ''\n  model", "  api_key: 'sk-test-1234'\n  model")
        (self.root / "config" / "config.yaml").write_text(text, encoding="utf-8")
        self.api = ConsoleApi(self.root)

    def test_requires_images(self):
        self.assertEqual(self.call("POST", "/api/inventory/vision", body={})[0], 400)

    def test_at_most_three_images(self):
        status, data = self.call("POST", "/api/inventory/vision",
                                 body={"images": [self.PNG] * 4})
        self.assertEqual(status, 400)
        self.assertIn("3", data["error"])

    def test_no_key_actionable_error(self):
        status, data = self.call("POST", "/api/inventory/vision", body={"images": [self.PNG]})
        self.assertEqual(status, 400)
        self.assertIn("DeepSeek", data["error"])

    def test_success(self):
        from unittest import mock
        self._with_llm_key()
        reply = "AK-47 | Redline (Field-Tested)\t1\t169\n手套武器箱\t2\t92"
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=(reply, {"total_tokens": 10})):
            status, data = self.call("POST", "/api/inventory/vision", body={"images": [self.PNG]})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["source"], "vision")
        self.assertEqual(len(data["items"]), 2)
        self.assertIn("already_exists", data["summary"])
        self.assertIn("vision_raw", data)

    def test_model_reads_nothing(self):
        from unittest import mock
        self._with_llm_key()
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=("NONE", {})):
            status, data = self.call("POST", "/api/inventory/vision", body={"images": [self.PNG]})
        self.assertEqual(status, 400)
        self.assertIn("没有识别到", data["error"])

    def test_llm_failure_maps_to_502(self):
        from unittest import mock
        self._with_llm_key()
        with mock.patch("cs2analyst.vision_ingest.call_model",
                        side_effect=RuntimeError("HTTP 401 invalid key")):
            status, data = self.call("POST", "/api/inventory/vision", body={"images": [self.PNG]})
        self.assertEqual(status, 502)
        self.assertIn("DeepSeek", data["error"])

    def test_remote_forbidden(self):
        status, _ = self.call("POST", "/api/inventory/vision", body={"images": [self.PNG]},
                              ip="10.9.9.9")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
