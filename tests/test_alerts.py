"""价格异动告警单元测试（离线）。"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.alerts.checker import AlertChecker
from cs2analyst.config import load_config


class TestAlertChecker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        self.cfg.paths.update({
            "data_dir": str(self.root / "data"),
            "report_dir": str(self.root / "reports"),
            "log_dir": str(self.root / "logs"),
            "inventory_file": str(self.root / "inventory.json"),
        })
        for d in (self.cfg.data_dir(), self.cfg.report_dir()):
            d.mkdir(parents=True, exist_ok=True)
        # 持仓：成本 100 的 A、成本 1000 的 B
        self.cfg.inventory_path().write_text(json.dumps([
            {"market_hash_name": "A | Skin (Factory New)", "quantity": 2, "buy_price": 100.0},
            {"market_hash_name": "B | Skin (Factory New)", "quantity": 1, "buy_price": 1000.0},
        ], ensure_ascii=False), encoding="utf-8")
        self.checker = AlertChecker(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 监控清单 ----------
    def test_collect_items_from_inventory(self):
        items = self.checker.collect_items()
        names = {i["name"] for i in items}
        self.assertIn("A | Skin (Factory New)", names)
        self.assertIn("B | Skin (Factory New)", names)
        self.assertEqual(len(items), 2)

    def test_collect_items_includes_ai_focus(self):
        """AI 明确看多（buy/add）或高信心看空（avoid/reduce≥4）的标的纳入监控。"""
        ld = self.cfg.data_dir() / "llm"
        ld.mkdir(parents=True, exist_ok=True)
        (ld / "2026-09-23.json").write_text(json.dumps({
            "items": {
                "D | Skin (Factory New)": {"stance": "buy", "confidence": 2},   # 看多即纳入
                "E | Skin (Factory New)": {"stance": "avoid", "confidence": 5},  # 高信心看空
                "F | Skin (Factory New)": {"stance": "hold", "confidence": 5},   # 持有→不纳入
                "G | Skin (Factory New)": {"stance": "avoid", "confidence": 3},  # 信心不足→不纳入
            },
        }, ensure_ascii=False), encoding="utf-8")
        names = {i["name"] for i in AlertChecker(self.cfg).collect_items()}
        self.assertIn("D | Skin (Factory New)", names)
        self.assertIn("E | Skin (Factory New)", names)
        self.assertNotIn("F | Skin (Factory New)", names)
        self.assertNotIn("G | Skin (Factory New)", names)

    # ---------- 触发规则 ----------
    def _items(self):
        return [{"name": "A | Skin (Factory New)", "qty": 2, "cost": 100.0, "source": "持仓"},
                {"name": "B | Skin (Factory New)", "qty": 1, "cost": 1000.0, "source": "持仓"}]

    def test_drop_vs_last(self):
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        alerts = self.checker.evaluate(
            self._items(), {"A | Skin (Factory New)": {"price": 96.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "跌" for a in alerts))

    def test_no_alert_on_small_move(self):
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        alerts = self.checker.evaluate(
            self._items(), {"A | Skin (Factory New)": {"price": 99.0, "sell_count": 10}})
        self.assertEqual([a for a in alerts if a.kind in ("跌", "涨")], [])

    def test_rise_vs_last(self):
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        alerts = self.checker.evaluate(
            self._items(), {"A | Skin (Factory New)": {"price": 105.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "涨" for a in alerts))

    def test_sell_count_spike(self):
        self.checker.state["last_counts"] = {"A | Skin (Factory New)": 100}
        self.checker.state["last_counts_kind"] = {"A | Skin (Factory New)": "listings"}
        alerts = self.checker.evaluate(
            self._items(), {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 150,
                                                       "count_kind": "listings"}})
        self.assertTrue(any(a.kind == "在售量" for a in alerts))

    def test_count_rule_skipped_without_kind_baseline(self):
        """升级/重置后若没有口径标记，宁可不比较，也不能拿两种口径硬比。"""
        c = AlertChecker(self.cfg)
        c.state["last_counts"] = {"A | Skin (Factory New)": 900}   # 无 last_counts_kind
        alerts = c.evaluate(self._items(), {"A | Skin (Factory New)": {
            "price": 100.0, "sell_count": 6, "count_kind": "listings"}})
        self.assertFalse(any(a.kind in ("在售量", "成交量") for a in alerts))
        # 本轮会把基准连同口径一起建立起来，下一轮即可正常比较
        self.assertEqual(c.state["last_counts_kind"].get("A | Skin (Factory New)"), "listings")

    def test_count_not_compared_across_metrics(self):
        """挂单量（批量接口）与当日成交数（官网兜底）口径不同，混比会造出假的「被扫货」。"""
        items = [{"name": "A | Skin (Factory New)", "qty": 1, "cost": 100.0, "source": "持仓"}]
        c = AlertChecker(self.cfg)
        c.state["last_counts"] = {"A | Skin (Factory New)": 900}
        c.state["last_counts_kind"] = {"A | Skin (Factory New)": "listings"}
        alerts = c.evaluate(items, {"A | Skin (Factory New)": {
            "price": 100.0, "sell_count": 6, "count_kind": "transactions"}})
        self.assertFalse(any(a.kind in ("在售量", "成交量") for a in alerts))
        # 同口径（都是挂单量）时照常判定
        c2 = AlertChecker(self.cfg)
        c2.state["last_counts"] = {"A | Skin (Factory New)": 900}
        c2.state["last_counts_kind"] = {"A | Skin (Factory New)": "listings"}
        alerts2 = c2.evaluate(items, {"A | Skin (Factory New)": {
            "price": 100.0, "sell_count": 100, "count_kind": "listings"}})
        self.assertTrue(any(a.kind == "在售量" for a in alerts2))

    def test_transaction_count_labelled_as_volume(self):
        """官网兜底只有成交量口径时，文案必须写「当日成交数/笔」，不能写成挂单。"""
        items = [{"name": "A | Skin (Factory New)", "qty": 1, "cost": 100.0, "source": "持仓"}]
        c = AlertChecker(self.cfg)
        c.state["last_counts"] = {"A | Skin (Factory New)": 100}
        c.state["last_counts_kind"] = {"A | Skin (Factory New)": "transactions"}
        alerts = c.evaluate(items, {"A | Skin (Factory New)": {
            "price": 100.0, "sell_count": 200, "count_kind": "transactions"}})
        a = [x for x in alerts if x.kind == "成交量"][0]
        self.assertIn("当日成交数", a.detail)
        self.assertIn("笔", a.detail)

    def test_footer_reports_partial_coverage(self):
        """取价不完整必须在推送里写明，否则用户会以为「没消息=全部正常」。"""
        from cs2analyst.alerts.checker import AlertRun
        self.assertIn("8/18", self.checker._footer(AlertRun(time="x", checked=8, expected=18)))
        self.assertIn("18 个标的", self.checker._footer(AlertRun(time="x", checked=18, expected=18)))

    def test_loss_and_profit_threshold(self):
        # A 亏损 45%（成本 100 → 现价 55）触发亏损提醒
        alerts = self.checker.evaluate(
            self._items(), {"A | Skin (Factory New)": {"price": 55.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "盈亏" and "亏损" in a.detail for a in alerts))
        # B 盈利 25%（成本 1000 → 现价 1250）触发止盈提醒
        c2 = AlertChecker(self.cfg)
        alerts2 = c2.evaluate(
            self._items(), {"B | Skin (Factory New)": {"price": 1250.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "盈亏" and "盈利" in a.detail for a in alerts2))

    def test_pnl_band_not_repeated(self):
        """同一档位的亏损只提醒一次；跨入更深档位才再次提醒。"""
        prices_45 = {"A | Skin (Factory New)": {"price": 55.0, "sell_count": 10}}
        first = self.checker.evaluate(self._items(), prices_45)
        self.assertTrue(any(a.kind == "盈亏" for a in first))
        # 同档位（-44%）不再提醒
        prices_44 = {"A | Skin (Factory New)": {"price": 56.0, "sell_count": 10}}
        second = self.checker.evaluate(self._items(), prices_44)
        self.assertFalse(any(a.kind == "盈亏" for a in second))
        # 跨入 -50% 档位 → 再次提醒
        prices_52 = {"A | Skin (Factory New)": {"price": 48.0, "sell_count": 10}}
        third = self.checker.evaluate(self._items(), prices_52)
        self.assertTrue(any(a.kind == "盈亏" for a in third))

    def test_break_support(self):
        items = [{"name": "A | Skin (Factory New)", "qty": 2, "cost": 100.0,
                  "support": 100.0, "source": "AI关注"}]
        alerts = self.checker.evaluate(
            items, {"A | Skin (Factory New)": {"price": 95.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "破位" for a in alerts))

    # ---------- 建仓信号（买点不必等 17:30 日报）----------
    def test_buy_signal_alert(self):
        items = [{"name": "A | Skin (Factory New)", "qty": 0, "cost": 0.0,
                  "action": "🟢 买入建仓", "score": 6.5, "source": "AI关注"}]
        alerts = self.checker.evaluate(
            items, {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 10}})
        sig = [a for a in alerts if a.kind == "建仓信号"]
        self.assertEqual(len(sig), 1)
        self.assertIn("买入建仓", sig[0].detail)
        self.assertIn("🟢", sig[0].to_line())

    def test_add_signal_alert(self):
        items = [{"name": "A | Skin (Factory New)", "qty": 2, "cost": 100.0,
                  "action": "🟢 加仓", "source": "持仓"}]
        alerts = self.checker.evaluate(
            items, {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 10}})
        self.assertTrue(any(a.kind == "建仓信号" for a in alerts))

    def test_no_signal_for_hold_or_reduce(self):
        for action in ("🟡 持有观望", "🟠 分批减仓", "🔴 清仓离场", None, ""):
            c = AlertChecker(self.cfg)
            items = [{"name": "A | Skin (Factory New)", "qty": 2, "cost": 100.0,
                      "action": action, "source": "持仓"}]
            alerts = c.evaluate(items, {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 10}})
            self.assertFalse(any(a.kind == "建仓信号" for a in alerts), action)

    def test_signal_respects_cooldown(self):
        """建仓信号同样受每日去抖限制，避免一天 5 次巡检反复轰炸。"""
        items = [{"name": "A | Skin (Factory New)", "qty": 0, "cost": 0.0,
                  "action": "🟢 买入建仓", "score": 6.0, "source": "AI关注"}]
        prices = {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 10}}
        first = self.checker.evaluate(items, prices)
        self.assertTrue(any(a.kind == "建仓信号" for a in first))
        second = self.checker.evaluate(items, prices)
        self.assertFalse(any(a.kind == "建仓信号" for a in second))

    def test_collect_items_attaches_advice(self):
        """持仓与观察池都带上当日系统建议，建仓信号才能判定。"""
        d = self.cfg.data_dir() / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-09-24.json").write_text(json.dumps({
            "date": "2026-09-24",
            "items": [{"饰品": "A | Skin (Factory New)", "系统建议": "🟢 买入建仓",
                       "评分": 7.0, "最近支撑": 90.0}],
            "watchlist": [{"饰品": "W1 | Skin (Factory New)", "系统建议": "🟢 加仓",
                           "评分": 5.0, "最近支撑": 10.0, "AI立场": "buy"}],
        }, ensure_ascii=False), encoding="utf-8")
        items = {i["name"]: i for i in AlertChecker(self.cfg).collect_items()}
        self.assertEqual(items["A | Skin (Factory New)"]["action"], "🟢 买入建仓")
        self.assertEqual(items["A | Skin (Factory New)"]["score"], 7.0)
        self.assertEqual(items["W1 | Skin (Factory New)"]["action"], "🟢 加仓")
        self.assertEqual(items["W1 | Skin (Factory New)"]["support"], 10.0)

    def test_push_highlights_buy_signal(self):
        """推送文本：标题变为建仓信号、信号置顶、并提示可升级为盘中挂单。"""
        from unittest import mock

        from cs2analyst.alerts.checker import Alert, AlertRun
        self.cfg.notify["wecom_webhook"] = "https://example.invalid/hook"
        run = AlertRun(time="2026-09-24 15:00", checked=18)
        urgent = [
            Alert(name="A | Skin", name_cn="甲饰品", kind="跌", detail="跌 4.5%", pct=-4.5),
            Alert(name="B | Skin", name_cn="乙饰品", kind="建仓信号",
                  detail="系统建议「🟢 买入建仓」"),
        ]
        text = self.checker._compose_alert_message(run, urgent)
        self.assertIn("出现建仓信号", text)
        self.assertLess(text.index("乙饰品"), text.index("甲饰品"))   # 信号置顶
        self.assertIn("盘中挂单", text)
        self.assertIn("9:00–24:00", text)
        # 真正发送时走 wecom.send
        captured: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, hook: captured.update(text=t) or True):
            self.assertTrue(self.checker._send(text))
        self.assertIn("乙饰品", captured["text"])

    def test_alert_title_by_kind(self):
        from cs2analyst.alerts.checker import Alert
        be = Alert(name="x", name_cn="", kind="破位", detail="")
        drop = Alert(name="x", name_cn="", kind="跌", detail="", pct=-5.0)
        sig = Alert(name="x", name_cn="", kind="建仓信号", detail="")
        self.assertIn("跌破支撑", self.checker._alert_title([be, drop]))
        self.assertIn("价格异动", self.checker._alert_title([drop]))
        self.assertIn("建仓信号", self.checker._alert_title([drop, sig]))

    # ---------- 3. 分级推送（巡检加密后防止告警疲劳）----------
    def test_urgency_classification(self):
        from cs2analyst.alerts.checker import Alert
        c = AlertChecker(self.cfg)
        mk = lambda kind, pct=None: Alert(name="x", name_cn="", kind=kind, detail="", pct=pct)
        for a in (mk("建仓信号"), mk("破位"), mk("盈亏"), mk("跌", -4.2), mk("涨", 6.5)):
            self.assertTrue(c._is_urgent(a), a.kind)
        for a in (mk("跌", -3.4), mk("涨", 5.0), mk("在售量"), mk("跌")):
            self.assertFalse(c._is_urgent(a), f"{a.kind}/{a.pct}")

    def test_flood_gate_folds_drop_and_rise_only(self):
        c = AlertChecker(self.cfg, rules={"urgent_max_per_kind_per_day": 2})
        self.assertFalse(c._flood_blocked("跌"))
        c._mark_urgent("跌")
        c._mark_urgent("跌")
        self.assertTrue(c._flood_blocked("跌"))          # 达到上限 → 折叠
        self.assertFalse(c._flood_blocked("建仓信号"))     # 买点信号永不折叠
        self.assertFalse(c._flood_blocked("破位"))

    def test_cooldown_rolls_across_midnight(self):
        """滚动 24 小时去重：昨晚推过的，今天凌晨不重复推。"""
        from datetime import datetime, timedelta
        c = AlertChecker(self.cfg)
        key = "A | Skin (Factory New)|跌"
        c.state["sent_ts"][key] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.assertTrue(c._already_sent("A | Skin (Factory New)", "跌"))
        c.state["sent_ts"][key] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        self.assertFalse(c._already_sent("A | Skin (Factory New)", "跌"))

    @staticmethod
    def _stub_checker(cfg, price, last, rules=None, **kw):
        """造一个不联网的 checker：取价与清单都由测试给定。

        summary_times 默认设成小时外（-1），避免测试恰好在 12/22 点跑时行为漂移。
        """
        c = AlertChecker(cfg, rules={"ai_commentary": False, "summary_times": [-1],
                                     **(rules or {})})
        name = "A | Skin (Factory New)"
        c.collect_items = lambda: [{"name": name, "qty": 1, "cost": 100.0, "source": "持仓"}]
        c.fetch_prices = lambda names: {name: {"price": price, "sell_count": 10}}
        c.state["last_prices"] = {name: last}
        return c, name

    def test_run_pushes_only_urgent(self):
        """跌 4% → 首次只记待确认，第二次巡检确认后才推送（防单次跳变）。"""
        from unittest import mock
        c, name = self._stub_checker(self.cfg, 96.0, 100.0)   # -4.0%
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            first = c.run(push=True)
        self.assertEqual(first.mode, "")                      # 首次不打扰
        self.assertEqual(first.urgent, [])
        self.assertTrue(any("待下次巡检确认" in a.detail for a in first.mild))
        self.assertEqual(sent, {})
        # 下一次巡检价格仍然低 → 二次确认成立 → 推送
        c.fetch_prices = lambda names: {name: {"price": 96.0, "sell_count": 10}}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            second = c.run(push=True)
        self.assertEqual(second.mode, "alert")
        self.assertEqual(len(second.urgent), 1)
        self.assertIn("价格异动提醒", sent["text"])

    def test_drop_flicker_never_pushed(self):
        """单次跳变（下一次就恢复）永不推送——这正是假信号的特征。"""
        from unittest import mock
        c, name = self._stub_checker(self.cfg, 96.0, 100.0)
        with mock.patch("cs2analyst.notify.wecom.send", side_effect=lambda t, h: True):
            c.run(push=True)                                  # 第一次：-4%
        c.fetch_prices = lambda names: {name: {"price": 100.0, "sell_count": 10}}  # 恢复
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            second = c.run(push=True)
        self.assertEqual(second.mode, "")
        self.assertEqual(sent, {})

    def test_buy_signal_needs_no_confirmation(self):
        """建仓信号来自每日分析结论（不依赖单次采样）→ 不需要两次确认，立即推。"""
        from unittest import mock
        c = AlertChecker(self.cfg, rules={"ai_commentary": False, "summary_times": [-1]})
        # 显式给一个推送地址：否则在"没有个人 config.yaml"的全新环境里会被
        # 「未配置推送地址」提前跳过，测试变成依赖开发者本机配置（实际踩到）
        self.cfg.notify["wecom_webhook"] = "https://example.invalid/hook"
        name = "A | Skin (Factory New)"
        c.collect_items = lambda: [{"name": name, "qty": 0, "cost": 0.0, "source": "买点信号",
                                    "action": "🟢 买入建仓", "score": 6.5}]
        c.fetch_prices = lambda names: {name: {"price": 100.0, "sell_count": 10}}
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            run = c.run(push=True)
        self.assertEqual(run.mode, "alert")
        self.assertIn("出现建仓信号", sent["text"])

    def test_run_silent_for_mild_move(self):
        """跌 3.2% → 只记录不推送，等 12/22 点概览汇总。"""
        from unittest import mock
        c, _ = self._stub_checker(self.cfg, 96.8, 100.0)      # -3.2%
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            run = c.run(push=True)
        self.assertEqual(run.mode, "")                         # 未推送
        self.assertEqual(run.urgent, [])
        self.assertEqual(len(run.mild), 1)
        self.assertEqual(sent, {})                             # 一条都没发
        self.assertEqual(len(c.state["mild"]), 1)              # 已进入缓冲

    def test_run_summary_push_includes_buffer(self):
        """汇总时刻：把缓冲里的温和波动一次性交代清楚，并发完清空。"""
        from datetime import datetime
        from unittest import mock
        hour = datetime.now().hour
        c, _ = self._stub_checker(self.cfg, 96.8, 100.0,
                                  rules={"summary_times": [hour]})
        c.state["mild"] = [{"time": "10:00", "name": "B | Skin", "name_cn": "乙饰品",
                            "kind": "在售量", "detail": "挂单骤增（抛压）：100 → 150 件",
                            "price": 10.0, "pct": None}]
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            run = c.run(push=True)
        self.assertEqual(run.mode, "summary")
        self.assertIn("盘中概览", sent["text"])
        self.assertIn("-3.20%", sent["text"])      # 缓冲里的温和波动
        self.assertIn("乙饰品", sent["text"])
        self.assertEqual(c.state["mild"], [])      # 汇总后清空
        self.assertTrue(c.state["last_summary"])

    def test_run_summary_sent_even_when_quiet(self):
        """汇总时刻即使毫无异动也发一条（等于每天两次「系统在线」确认）。"""
        from datetime import datetime
        from unittest import mock
        c, _ = self._stub_checker(self.cfg, 100.0, 100.0,
                                  rules={"summary_times": [datetime.now().hour]})
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            run = c.run(push=True)
        self.assertEqual(run.mode, "summary")
        self.assertIn("盘中概览", sent["text"])
        self.assertIn("价格平稳", sent["text"])

    def test_run_flood_gate_folds_excess(self):
        """跌类紧急消息超过每日上限后自动折叠（不再打扰）。"""
        from unittest import mock
        c, _ = self._stub_checker(self.cfg, 90.0, 100.0,
                                  rules={"urgent_max_per_kind_per_day": 1})
        c.state["urgent_by_day"] = {c._today_key(): {"跌": 1}}
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send",
                        side_effect=lambda t, h: sent.update(text=t) or True):
            run = c.run(push=True)
        self.assertEqual(run.mode, "")             # 被折叠 → 不推送
        self.assertEqual(run.urgent, [])
        self.assertEqual(len(run.mild), 1)
        self.assertEqual(sent, {})

    def test_intraday_curve_saved(self):
        """每次巡检的取价落盘，积累日内价格曲线。"""
        from datetime import date
        c = AlertChecker(self.cfg)
        c.save_intraday({"A | Skin": {"price": 12.5, "sell_count": 7}})
        f = self.cfg.data_dir() / "intraday" / f"{date.today():%Y-%m-%d}.jsonl"
        self.assertTrue(f.exists())
        rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["prices"]["A | Skin"], 12.5)
        self.assertEqual(rec["counts"]["A | Skin"], 7)

    def test_fallback_is_capped(self):
        """批量接口失败时，官网兜底限量（避免高频巡检把官网请求打爆）。"""
        from unittest import mock

        calls: list[str] = []

        class FakeScraper:
            def __init__(self, **kw):
                pass

            def item_detail(self, n):
                calls.append(n)
                return {"sellingPriceList": [{"platform": "youpin", "price": 10.0}],
                        "transactionCount": 5}

        c = AlertChecker(self.cfg, rules={"fallback_max_items": 2, "interval_seconds": 0,
                                          "batch_retry_wait": 0})
        with mock.patch("cs2analyst.alerts.checker.SteamDTClient") as client, \
                mock.patch("cs2analyst.datasource.steamdt_scraper.SteamDTScraper", FakeScraper):
            client.return_value.batch_price.return_value = {}
            prices = c.fetch_prices([f"W{i} | Skin" for i in range(6)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(prices), 2)

    def test_batch_retry_before_fallback(self):
        """批量接口限流失败 → 先等待重试一次；重试成功就不用麻烦官网。"""
        from unittest import mock

        c = AlertChecker(self.cfg, rules={"batch_retry_wait": 0, "interval_seconds": 0})
        calls = {"batch": 0}

        def flaky(names):
            calls["batch"] += 1
            if calls["batch"] == 1:
                raise RuntimeError("errorCode=4005 当前接口请求已达到上限")
            return {"A | Skin": [{"platform": "youpin", "sellPrice": 12.5, "sellCount": 7}]}

        with mock.patch("cs2analyst.alerts.checker.SteamDTClient") as client:
            client.return_value.batch_price.side_effect = flaky
            prices = c.fetch_prices(["A | Skin"])
        self.assertEqual(calls["batch"], 2)                       # 重试了一次
        self.assertEqual(prices["A | Skin"]["price"], 12.5)
        self.assertEqual(prices["A | Skin"]["count_kind"], "listings")

    def test_scraper_fallback_marked_as_transactions(self):
        """官网兜底取到的计数是「当日成交数」，必须打上不同口径标记。"""
        from unittest import mock

        class FakeScraper:
            def __init__(self, **kw):
                pass

            def item_detail(self, n):
                return {"sellingPriceList": [{"platform": "youpin", "price": 20.0}],
                        "transactionCount": 9}

        c = AlertChecker(self.cfg, rules={"batch_retry_wait": 0, "interval_seconds": 0})
        with mock.patch("cs2analyst.alerts.checker.SteamDTClient") as client, \
                mock.patch("cs2analyst.datasource.steamdt_scraper.SteamDTScraper", FakeScraper):
            client.return_value.batch_price.return_value = {}
            prices = c.fetch_prices(["A | Skin"])
        self.assertEqual(prices["A | Skin"]["count_kind"], "transactions")
        self.assertEqual(prices["A | Skin"]["sell_count"], 9)

    def test_buy_signal_item_not_dropped_by_top_n(self):
        """买点信号标的必须进监控，不能被观察池 Top N 挤掉。"""
        d = self.cfg.data_dir() / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        watch = [{"饰品": f"W{i} | Skin (Factory New)", "评分": 9.0 - i, "最近支撑": 10.0}
                 for i in range(8)]
        # 评分最低的那个才是买点信号（正常会被 Top5 排除）
        watch[-1]["系统建议"] = "🟢 买入建仓"
        (d / "2026-09-24.json").write_text(json.dumps(
            {"date": "2026-09-24", "items": [], "watchlist": watch}, ensure_ascii=False),
            encoding="utf-8")
        items = {i["name"]: i for i in AlertChecker(self.cfg).collect_items()}
        self.assertIn("W7 | Skin (Factory New)", items)
        self.assertTrue(items["W7 | Skin (Factory New)"]["source"].startswith("买点信号"))

    # ---------- 价格基准（2026-09-24：全平台最低价 → 悠悠有品）----------
    def test_pick_price_prefers_youpin(self):
        """基准价取悠悠有品；HALOSKINS 报的低价不再主导（那正是假破位的根源）。"""
        from cs2analyst.alerts.checker import pick_price
        rows = [{"platform": "haloskins", "sellPrice": 201.69},
                {"platform": "c5", "sellPrice": 218.37},
                {"platform": "youpin", "sellPrice": 219.20},
                {"platform": "buff", "sellPrice": 222.50},
                {"platform": "steam", "sellPrice": 280.26}]
        price, basis, by = pick_price(rows)
        self.assertEqual(price, 219.20)
        self.assertEqual(basis, "悠悠有品")
        self.assertEqual(by["haloskins"], 201.69)      # 仍然记录，供排查

    def test_pick_price_fallback_and_junk_filter(self):
        from cs2analyst.alerts.checker import pick_price
        # 没有悠悠 → 用国内平台中位数（不是最低价）
        price, basis, _ = pick_price([{"platform": "c5", "sellPrice": 100.0},
                                      {"platform": "buff", "sellPrice": 110.0}])
        self.assertEqual(basis, "国内平台中位数")
        self.assertEqual(price, 100.0)                 # 两个值时取较小那个（下中位）
        # 越界值（时间戳 17.9 亿）/ 嵌套对象 / 占位价 0.09 全部剔除
        price, basis, by = pick_price([
            {"platform": "youpin", "sellPrice": 104.65},
            {"platform": "buff", "sellPrice": 110.78},
            {"platform": "dmarket", "sellPrice": {"data": "<cycle 2>"}},
            {"platform": "waxpeer", "sellPrice": 1790222464},
            {"platform": "csmoney", "sellPrice": 0.09}])
        self.assertEqual(price, 104.65)
        self.assertNotIn("dmarket", by)
        self.assertNotIn("waxpeer", by)
        self.assertNotIn("csmoney", by)

    def test_basis_change_skips_vs_last(self):
        """价源换了（悠悠缺失→中位数）就不做「较上次检查」比较，否则是拿两个平台的价在比。"""
        items = [{"name": "A | Skin (Factory New)", "qty": 1, "cost": 100.0, "source": "持仓"}]
        c = AlertChecker(self.cfg)
        c.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        c.state["last_price_basis"] = {"A | Skin (Factory New)": "悠悠有品"}
        alerts = c.evaluate(items, {"A | Skin (Factory New)": {
            "price": 90.0, "sell_count": 10, "price_basis": "国内平台中位数"}})
        self.assertFalse(any(a.kind in ("跌", "涨") for a in alerts))
        # 价源一致时正常判定
        c2 = AlertChecker(self.cfg)
        c2.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        c2.state["last_price_basis"] = {"A | Skin (Factory New)": "悠悠有品"}
        alerts2 = c2.evaluate(items, {"A | Skin (Factory New)": {
            "price": 90.0, "sell_count": 10, "price_basis": "悠悠有品"}})
        self.assertTrue(any(a.kind == "跌" for a in alerts2))

    def test_price_basis_migration_clears_old_baseline(self):
        """升级瞬间：旧基准（全平台最低价）必须丢弃，否则每件持仓都被算出假跳涨。"""
        self.checker.state_path.write_text(json.dumps({
            "last_prices": {"A | Skin (Factory New)": 201.69},   # 无 last_price_basis
        }, ensure_ascii=False), encoding="utf-8")
        c = AlertChecker(self.cfg)
        self.assertEqual(c.state["last_prices"], {})

    def test_yesterday_price_uses_same_basis(self):
        """昨日快照也要按同一基准重算，否则「今日悠悠 vs 昨日全平台最低」本身就是假涨跌。"""
        snap = self.cfg.data_dir() / "snapshots"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "2026-09-22_prices.json").write_text(json.dumps({
            "_date": "2026-09-22",
            "A | Skin (Factory New)": [
                {"platform": "HALOSKINS", "sellPrice": 201.69, "sellCount": 100},
                {"platform": "YOUPIN", "sellPrice": 219.20, "sellCount": 300},
                {"platform": "BUFF", "sellPrice": 222.50, "sellCount": 200}],
        }, ensure_ascii=False), encoding="utf-8")
        y = AlertChecker(self.cfg)._yesterday_prices()
        self.assertEqual(y["A | Skin (Factory New)"], 219.20)

    def test_intraday_skips_suspicious_prices(self):
        """被判为脏数据的价格绝不能进日内曲线（历史上 17.9 亿混进来过）。"""
        from datetime import date
        c, name = self._stub_checker(self.cfg, 1790222464.0, 100.0)   # 离谱高价
        c.run(push=False)
        f = self.cfg.data_dir() / "intraday" / f"{date.today():%Y-%m-%d}.jsonl"
        self.assertFalse(f.exists() and name in f.read_text(encoding="utf-8"))
        # 正常价格照常落盘
        c2, name2 = self._stub_checker(self.cfg, 105.0, 100.0)
        c2.run(push=False)
        files = sorted((self.cfg.data_dir() / "intraday").glob("*.jsonl"))
        self.assertIn(name2, files[-1].read_text(encoding="utf-8"))

    def test_footer_mentions_price_basis(self):
        from cs2analyst.alerts.checker import AlertRun
        self.assertIn("悠悠有品", self.checker._footer(AlertRun(time="x", checked=18)))

    # ---------- 去抖与状态 ----------
    def test_cooldown_same_day(self):
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        prices = {"A | Skin (Factory New)": {"price": 90.0, "sell_count": 10}}
        first = self.checker.evaluate(self._items(), prices)
        self.assertTrue(any(a.kind == "跌" for a in first))
        # 同一天第二次巡检，同一标的同类信号不再重复推送
        second = self.checker.evaluate(self._items(), prices)
        self.assertFalse(any(a.kind == "跌" for a in second))

    def test_state_persisted(self):
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        self.checker.evaluate(self._items(),
                              {"A | Skin (Factory New)": {"price": 100.0, "sell_count": 42}})
        self.assertTrue(self.checker.state_path.exists())
        again = AlertChecker(self.cfg)
        self.assertEqual(again.state["last_prices"].get("A | Skin (Factory New)"), 100.0)
        self.assertEqual(again.state["last_counts"].get("A | Skin (Factory New)"), 42)

    def test_no_alert_when_price_missing(self):
        alerts = self.checker.evaluate(self._items(), {})
        self.assertEqual(alerts, [])

    # ---------- 监控范围 ----------
    def test_sticker_excluded(self):
        """贴纸等无行情品类不进监控清单。"""
        self.cfg.inventory_path().write_text(json.dumps([
            {"market_hash_name": "A | Skin (Factory New)", "quantity": 1, "buy_price": 100.0},
            {"market_hash_name": "Sticker | Some Team", "quantity": 2, "buy_price": 50.0},
        ], ensure_ascii=False), encoding="utf-8")
        names = {i["name"] for i in AlertChecker(self.cfg).collect_items()}
        self.assertIn("A | Skin (Factory New)", names)
        self.assertNotIn("Sticker | Some Team", names)

    def test_manual_price_excluded(self):
        self.cfg.inventory_path().write_text(json.dumps([
            {"market_hash_name": "Weird | Item", "quantity": 1, "buy_price": 10.0,
             "manual_price": 8.0},
        ], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(AlertChecker(self.cfg).collect_items(), [])

    def test_watchlist_top_n_included(self):
        """观察池按评分排序取前 N 个纳入监控。"""
        d = self.cfg.data_dir() / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        watch = [{"饰品": f"W{i} | Skin (Factory New)", "评分": float(i), "最近支撑": 10.0}
                 for i in range(8)]
        (d / "2026-09-23.json").write_text(json.dumps(
            {"date": "2026-09-23", "items": [], "watchlist": watch}, ensure_ascii=False),
            encoding="utf-8")
        items = AlertChecker(self.cfg).collect_items()
        watch_names = {i["name"] for i in items if i["source"].startswith("AI关注")}
        self.assertEqual(len(watch_names), 5)          # 默认取 5 个
        self.assertIn("W7 | Skin (Factory New)", watch_names)   # 评分最高的在内
        self.assertNotIn("W0 | Skin (Factory New)", watch_names)  # 评分最低的被排除

    # ---------- 脏数据防护 ----------
    def test_clean_sell_prices(self):
        from cs2analyst.alerts.checker import clean_sell_prices
        # 混入 0.09 占位价 → 被剔除，最低价取真实的 111
        self.assertEqual(clean_sell_prices([0.09, 111.0, 112.5, 118.0]), [111.0, 112.5, 118.0])
        self.assertEqual(clean_sell_prices([5.0]), [5.0])
        self.assertEqual(clean_sell_prices([]), [])

    def test_outlier_guard_skips_dirty_price(self):
        """价格与上次偏差过大（如脏数据 0.09）→ 跳过，不告警也不污染基准。"""
        items = [{"name": "A | Skin (Factory New)", "qty": 2, "cost": 100.0, "source": "持仓"}]
        self.checker.state["last_prices"] = {"A | Skin (Factory New)": 100.0}
        alerts = self.checker.evaluate(
            items, {"A | Skin (Factory New)": {"price": 0.09, "sell_count": 10}})
        self.assertEqual(alerts, [])
        self.assertTrue(self.checker.suspicious)
        self.assertEqual(self.checker.state["last_prices"]["A | Skin (Factory New)"], 100.0)

    def test_outlier_guard_uses_yesterday_price(self):
        """即使没有上次巡检基准，也会与昨日快照价对比，拦截脏数据。"""
        snap = self.cfg.data_dir() / "snapshots"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "2026-09-22_prices.json").write_text(json.dumps({
            "_date": "2026-09-22",
            "A | Skin (Factory New)": [{"platform": "YOUPIN", "sellPrice": 108.0, "sellCount": 10}],
        }, ensure_ascii=False), encoding="utf-8")
        fresh = AlertChecker(self.cfg)          # 全新状态（无上次巡检基准）
        items = [{"name": "A | Skin (Factory New)", "qty": 1, "cost": 100.0, "source": "持仓"}]
        alerts = fresh.evaluate(items, {"A | Skin (Factory New)": {"price": 0.09, "sell_count": 5}})
        self.assertEqual(alerts, [])
        self.assertTrue(fresh.suspicious)
        # 正常价格不会被误判为脏数据
        fresh2 = AlertChecker(self.cfg)
        fresh2.evaluate(items, {"A | Skin (Factory New)": {"price": 105.0, "sell_count": 5}})
        self.assertEqual(fresh2.suspicious, [])
        self.assertEqual(fresh2.state["last_prices"].get("A | Skin (Factory New)"), 105.0)

    # ---------- 8. AI 点评必须知道「这件是不是我的持仓」（2026-09-30 用户反馈）----------
    def test_position_state_holdings_vs_watchlist(self):
        """持仓 → 给出件数/成本/盈亏/锁定期；非持仓 → 明说是观察标的。"""
        from datetime import datetime

        from cs2analyst.alerts.checker import Alert, AlertChecker
        c = AlertChecker(self.cfg)
        held = Alert(name="x", name_cn="甲", kind="跌", detail="", qty=2, cost=100.0,
                     pnl_pct=-25.0, source="持仓")
        state = c._position_state(held)
        self.assertIn("我的持仓", state)
        self.assertIn("2 件", state)
        self.assertIn("-25.0%", state)
        # 买入日期没记录 → 必须说"未知"，不能瞎猜能不能卖（用户只在持仓文件里没有 buy_date）
        self.assertIn("锁定期未知", state)

        old = Alert(name="x", name_cn="甲", kind="跌", detail="", qty=1, cost=100.0,
                    pnl_pct=-10.0, source="持仓", buy_date="2026-01-01")
        self.assertIn("可以卖", c._position_state(old))

        today = datetime.now().strftime("%Y-%m-%d")
        fresh = Alert(name="x", name_cn="甲", kind="跌", detail="", qty=1, cost=100.0,
                      source="持仓", buy_date=today)
        self.assertIn("锁定期", c._position_state(fresh))
        self.assertIn("才可卖", c._position_state(fresh))

        watch = Alert(name="y", name_cn="乙", kind="建仓信号", detail="买入")
        self.assertIn("非持仓", c._position_state(watch))

    def test_position_state_unlocked_flag(self):
        """用户确认过「早就过了 7 天」时，要明确说可以卖，而不是"锁定期未知"。"""
        from cs2analyst.alerts.checker import Alert, AlertChecker
        c = AlertChecker(self.cfg)
        held = Alert(name="x", name_cn="甲", kind="跌", detail="", qty=1, cost=100.0,
                     pnl_pct=-20.0, source="持仓", unlocked=True)
        state = c._position_state(held)
        self.assertIn("可以卖", state)
        self.assertIn("已确认", state)
        self.assertNotIn("未知", state)

    def test_ai_comment_prompt_carries_position_state(self):
        """提示词必须把逐条的持仓状态喂给模型，否则它只能写"若你是持仓者…"这种模板话。"""
        import inspect

        from cs2analyst.alerts.checker import AlertChecker
        src = inspect.getsource(AlertChecker.ai_comment)
        self.assertIn("_position_state", src)
        self.assertIn("trading_rules_text", src)   # 原有要求：必须引用共享规则文本
        self.assertIn("T+N", src)

    def test_collect_items_carries_buy_date_for_holdings(self):
        items = AlertChecker(self.cfg).collect_items()
        holds = [i for i in items if i.get("source") == "持仓"]
        self.assertTrue(holds, "测试夹具里应该有持仓条目")
        for it in holds:
            self.assertIn("buy_date", it)

    def test_alert_line_shows_holding_tag(self):
        from cs2analyst.alerts.checker import Alert
        held = Alert(name="x", name_cn="甲饰品", kind="跌", detail="跌 4%", qty=3, cost=10.0)
        self.assertIn("持仓 3 件", held.to_line())
        watch = Alert(name="y", name_cn="乙饰品", kind="建仓信号", detail="买入")
        self.assertNotIn("持仓", watch.to_line())

    def test_buffered_mild_keeps_holding_identity(self):
        """温和波动进缓冲再重建，仍要认出它是持仓。

        2026-10-06 回归：12:00/22:00 概览的 AI 点评用的是「从 state['mild'] 重建的
        Alert」，而重建时只还原了 6 个字段、qty/cost 全丢 → 7 条真实持仓被点评说成
        「全是观察标的、无持仓，无需止损」。这里锁死「缓冲往返不丢持仓身份」。
        """
        from cs2analyst.alerts.checker import Alert, AlertChecker
        c = AlertChecker(self.cfg)
        held = Alert(name="A | Skin (Factory New)", name_cn="甲", kind="跌",
                     detail="现价 ¥95，较上次检查（¥100.00）**-5.00%**",
                     price=95.0, pct=-5.0, qty=2, cost=100.0, pnl_pct=-5.0,
                     source="持仓", unlocked=True)
        c._buffer_mild([held])
        rebuilt = c._buffered_to_alerts(c.state["mild"])
        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(rebuilt[0].qty, 2)          # 件数不能丢
        self.assertEqual(rebuilt[0].cost, 100.0)     # 成本不能丢
        self.assertEqual(rebuilt[0].source, "持仓")
        self.assertTrue(rebuilt[0].unlocked)
        self.assertIn("我的持仓", c._position_state(rebuilt[0]))

    def test_buffered_mild_tolerates_legacy_records(self):
        """旧缓冲记录（无 qty/cost 字段）重建不能报错，按非持仓处理（向后兼容）。"""
        from cs2analyst.alerts.checker import AlertChecker
        c = AlertChecker(self.cfg)
        rebuilt = c._buffered_to_alerts([{"time": "10:00", "name": "B | Skin",
                                          "name_cn": "乙饰品", "kind": "涨",
                                          "detail": "涨 4%", "price": 10.0, "pct": 4.0}])
        self.assertEqual(rebuilt[0].qty, 0)
        self.assertIn("非持仓", c._position_state(rebuilt[0]))

    # ---------- 9. 盘中概览的 Word（2026-09-30 用户要求：概览也要 Word）----------
    def test_summary_docx_generated_and_pushed(self):
        """概览 Word：体检表能落盘、能作为附件推送；且不含已卖出条目以外的臆造数据。"""
        from pathlib import Path as _Path
        from unittest import mock

        from docx import Document

        from cs2analyst.alerts.checker import Alert, AlertChecker, AlertRun
        self.cfg.notify["wecom_webhook"] = "https://example.invalid/hook"
        c = AlertChecker(self.cfg)
        c.item_table = [
            {"name": "A | Skin (Factory New)", "display": "甲饰品（A | Skin）",
             "price": 105.0, "chg_last": -4.0, "chg_yest": -6.0, "qty": 2, "cost": 100.0,
             "pnl_pct": 5.0, "support": 110.0, "action": "🟡 持有观望", "source": "持仓"},
            {"name": "B | Skin (Factory New)", "display": "乙饰品（B | Skin）",
             "price": 50.0, "chg_last": 1.2, "chg_yest": 0.4, "qty": 0, "cost": 0.0,
             "pnl_pct": None, "support": 45.0, "action": "🟢 买入建仓", "source": "买点信号"},
        ]
        run = AlertRun(time="2026-09-30 12:00", checked=2, expected=2)
        sent: dict = {}
        with mock.patch("cs2analyst.notify.wecom.send_file",
                        side_effect=lambda hook, path, **kw: sent.update(path=str(path)) or True):
            c._send_summary_docx(
                run,
                [Alert(name="A | Skin (Factory New)", name_cn="甲饰品", kind="跌", detail="跌 4%")],
                [Alert(name="B | Skin (Factory New)", name_cn="乙饰品", kind="涨", detail="涨 1.2%")])
        self.assertTrue(sent.get("path"), "应该把生成的 Word 交给 send_file 推送")
        path = _Path(sent["path"])
        self.assertTrue(path.exists())
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs)
        for t in doc.tables:
            for row in t.rows:
                text += "\n" + " ".join(cell.text for cell in row.cells)
        self.assertIn("盘中概览", text)
        self.assertIn("甲饰品", text)          # 持仓体检表
        self.assertIn("乙饰品", text)          # 关注标的
        self.assertIn("组合合计", text)
        self.assertIn("持有观望", text)

    def test_summary_docx_failure_does_not_raise(self):
        """附件生成失败绝不能把已经发出的文字推送搅成异常。"""
        from unittest import mock

        from cs2analyst.alerts.checker import AlertChecker, AlertRun
        c = AlertChecker(self.cfg)
        c.item_table = []
        run = AlertRun(time="2026-09-30 12:00")
        with mock.patch("cs2analyst.report.docx_summary.build_summary_docx",
                        side_effect=RuntimeError("boom")):
            c._send_summary_docx(run, [], [])      # 不应抛异常


if __name__ == "__main__":
    unittest.main()
