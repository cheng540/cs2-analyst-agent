"""每周复盘推送单元测试（离线，纯文件读取）。"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from cs2analyst.config import load_config
from tools.weekly_report import build_weekly, collect_alerts, collect_sim


def _days(n=7):
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


class TestWeeklyReport(unittest.TestCase):
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
        self.cfg.sim["state_file"] = str(self.root / "state_a.json")
        self.cfg.sim["state_file_b"] = str(self.root / "state_b.json")
        for d in (self.cfg.data_dir(), self.cfg.report_dir()):
            d.mkdir(parents=True, exist_ok=True)
        self.days = _days(7)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, path: Path, label: str, net: float, trades: list | None = None,
                     created: str | None = None):
        path.write_text(json.dumps({
            "created": created or self.days[0], "label": label, "initial_capital": 3000.0,
            "cash": net, "positions": {}, "trade_log": trades or [],
            "journal": [{"date": self.days[-2], "net_value": 3000.0, "return_pct": 0.0,
                         "thoughts": "第一日观察。"},
                        {"date": self.days[-1], "net_value": net,
                         "return_pct": round((net / 3000 - 1) * 100, 2),
                         "thoughts": "大盘下降趋势、高风险。今日无操作，耐心等待信号。"}],
        }, ensure_ascii=False), encoding="utf-8")

    def _write_snap(self, day: str, value: float, index: float):
        d = self.cfg.data_dir() / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{day}.json").write_text(json.dumps({
            "date": day,
            "market": {"趋势": "下降趋势", "风险等级": "高风险", "量能": "平量",
                       "大盘指数": index, "情绪分": 41.0},
            "portfolio": {"总成本": 3173.59, "总市值": value, "盈亏%": -37.1},
            "items": [], "watchlist": [],
        }, ensure_ascii=False), encoding="utf-8")

    # ---------- 模拟盘 ----------
    def test_collect_sim_reads_both_tracks(self):
        self._write_state(self.cfg.sim_state_path(), "A", 3150.0)
        self._write_state(self.cfg.sim_state_path_b(), "B", 2870.5)
        sim = collect_sim(self.cfg, self.days)
        self.assertEqual(len(sim["tracks"]), 2)
        a = [t for t in sim["tracks"] if t["label"] == "A"][0]
        b = [t for t in sim["tracks"] if t["label"] == "B"][0]
        self.assertAlmostEqual(a["net_value"], 3150.0)
        self.assertAlmostEqual(b["net_value"], 2870.5)
        self.assertAlmostEqual(b["return_pct"], -4.32, places=2)
        self.assertEqual(a["journal_days"], 2)

    def test_collect_sim_counts_week_trades_only(self):
        old = self.days[0]
        trades = [{"date": self.days[-1], "action": "BUY"}, {"date": old, "action": "SELL"}]
        self._write_state(self.cfg.sim_state_path(), "A", 3000.0, trades)
        sim = collect_sim(self.cfg, self.days)
        self.assertEqual(sim["tracks"][0]["week_trades"], 2)
        self.assertEqual(sim["tracks"][0]["total_trades"], 2)
        # 只回看最近 3 天：窗口外的 SELL（6 天前）不计入
        sim2 = collect_sim(self.cfg, self.days[-3:])
        self.assertEqual(sim2["tracks"][0]["week_trades"], 1)

    # ---------- 巡检统计 ----------
    def test_collect_alerts_counts_runs_and_signals(self):
        d = self.cfg.data_dir() / "alerts"
        d.mkdir(parents=True, exist_ok=True)
        recs = [{"time": "x", "checked": 18, "alerts": []},
                {"time": "y", "checked": 18, "alerts": [{"kind": "跌"}, {"kind": "建仓信号"}]}]
        (d / f"{self.days[-1]}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
        st = collect_alerts(self.cfg, self.days)
        self.assertEqual(st["runs"], 2)
        self.assertEqual(st["hits"], 2)
        self.assertEqual(st["signals"], 1)
        self.assertEqual(st["days"], 1)

    def test_collect_alerts_counts_pushes(self):
        """统计「打扰了你几次」：只数真正推送出去的那些巡检。"""
        d = self.cfg.data_dir() / "alerts"
        d.mkdir(parents=True, exist_ok=True)
        recs = [
            {"time": "x", "checked": 18, "alerts": [], "mode": ""},                 # 静默
            {"time": "y", "checked": 18, "alerts": [{"kind": "跌"}], "mode": "alert"},
            {"time": "z", "checked": 18, "alerts": [{"kind": "跌"}], "mode": "summary"},
            {"time": "w", "checked": 18, "alerts": [], "mode": ""},
        ]
        (d / f"{self.days[-1]}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
        st = collect_alerts(self.cfg, self.days)
        self.assertEqual(st["runs"], 4)
        self.assertEqual(st["pushed"], 2)

    def test_collect_realized_trades(self):
        """已实现交易（真实卖出）：本周与累计分开统计。"""
        from tools.weekly_report import collect_realized
        (self.cfg.data_dir() / "real_trades.json").write_text(json.dumps([
            {"date": self.days[-1], "name": "A | Skin", "name_cn": "甲", "qty": 1,
             "price": 3.65, "net": 3.56, "cost": 6.15, "pnl": -2.59},
            {"date": self.days[0], "name": "B | Skin", "name_cn": "乙", "qty": 2,
             "price": 10.0, "net": 19.5, "cost": 15.0, "pnl": 4.5},
        ], ensure_ascii=False), encoding="utf-8")
        r = collect_realized(self.cfg, self.days[-3:])       # 只看最近 3 天
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["week"], 1)
        self.assertEqual(r["week_pnl"], -2.59)
        self.assertEqual(r["total_pnl"], 1.91)
        self.assertEqual(r["last"], "乙")

    def test_realized_slippage_reported(self):
        """成交价 vs 报告价的偏差：验证「市值是否虚高」的真实数据。"""
        from tools.weekly_report import build_weekly, collect_realized
        (self.cfg.data_dir() / "real_trades.json").write_text(json.dumps([
            {"date": self.days[-1], "name": "A | Skin", "name_cn": "甲", "qty": 2,
             "price": 1.30, "net": 2.53, "cost": 11.94, "pnl": -9.41,
             "market_price_at_sale": 2.77, "slippage_pct": -53.07},
        ], ensure_ascii=False), encoding="utf-8")
        r = collect_realized(self.cfg, self.days)
        self.assertAlmostEqual(r["avg_slippage"], -53.1, places=1)
        self._write_snap(self.days[-1], 1995.0, 795.0)
        # 卖出后跟踪会去取实时价——测试里必须挡掉，否则会真发请求并等 65 秒重试
        with mock.patch("tools.weekly_report.collect_post_sale", return_value=[]):
            text, _meta = build_weekly(self.cfg, 7)
        self.assertIn("成交价 vs 报告价", text)
        self.assertIn("纸面数字", text)

    def test_collect_realized_empty(self):
        from tools.weekly_report import collect_realized
        r = collect_realized(self.cfg, self.days)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["total_pnl"], 0.0)
        self.assertIsNone(r["avg_slippage"])

    def test_collect_post_sale_tracks_verdict(self):
        """卖出后跟踪：用最新价对比成交价，判断「建议卖出」对不对。"""
        from unittest import mock

        from tools.weekly_report import collect_post_sale
        today = date.today().strftime("%Y-%m-%d")
        (self.cfg.data_dir() / "real_trades.json").write_text(json.dumps([
            {"date": today, "name": "A | Skin", "name_cn": "甲", "qty": 1, "price": 100.0},
            {"date": today, "name": "B | Skin", "name_cn": "乙", "qty": 1, "price": 100.0},
        ], ensure_ascii=False), encoding="utf-8")

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            def batch_price(self, names):
                return {"A | Skin": [{"platform": "youpin", "sellPrice": 90.0}],
                        "B | Skin": [{"platform": "youpin", "sellPrice": 110.0}]}

        with mock.patch("cs2analyst.datasource.steamdt.SteamDTClient", FakeClient):
            out = collect_post_sale(self.cfg)
        self.assertEqual(len(out), 2)
        a = [x for x in out if x["name"] == "甲"][0]
        b = [x for x in out if x["name"] == "乙"][0]
        self.assertEqual(a["chg_pct"], -10.0)
        self.assertIn("卖对了", a["verdict"])          # 卖掉之后跌了 → 卖对了
        self.assertIn("卖早了", b["verdict"])          # 卖掉之后涨了 → 卖早了

    def test_collect_post_sale_skips_old_trades(self):
        from tools.weekly_report import collect_post_sale
        (self.cfg.data_dir() / "real_trades.json").write_text(json.dumps([
            {"date": "2020-01-01", "name": "A | Skin", "qty": 1, "price": 100.0},
        ], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(collect_post_sale(self.cfg, max_days=60), [])

    def test_collect_post_sale_survives_api_failure(self):
        """取价失败不能影响周报（网络/限流问题按跳过处理）。"""
        from unittest import mock

        from tools.weekly_report import collect_post_sale
        today = date.today().strftime("%Y-%m-%d")
        (self.cfg.data_dir() / "real_trades.json").write_text(json.dumps([
            {"date": today, "name": "A | Skin", "name_cn": "甲", "qty": 1, "price": 100.0},
        ], ensure_ascii=False), encoding="utf-8")

        class Boom:
            def __init__(self, *a, **kw):
                pass

            def batch_price(self, names):
                raise RuntimeError("errorCode=4005")

        with mock.patch("cs2analyst.datasource.steamdt.SteamDTClient", Boom), \
                mock.patch("tools.weekly_report.time.sleep"):     # 不要真的等 65 秒
            self.assertEqual(collect_post_sale(self.cfg), [])

    # ---------- 整体文本 ----------
    def test_build_weekly_content(self):
        self._write_state(self.cfg.sim_state_path(), "A", 3150.0)
        self._write_state(self.cfg.sim_state_path_b(), "B", 2870.5)
        self._write_snap(self.days[0], 2100.0, 820.0)
        self._write_snap(self.days[-1], 1995.0, 795.0)
        text, meta = build_weekly(self.cfg, 7)
        self.assertIn("模拟盘周报", text)
        self.assertIn("A 轨", text)
        self.assertIn("B 轨", text)
        self.assertIn("3,150.00", text)
        self.assertIn("-4.32%", text)              # B 轨收益
        self.assertIn("1,995", text)               # 持仓市值
        self.assertIn("-5.00%", text)              # 持仓市值 2100 → 1995
        self.assertIn("795", text)                 # 大盘指数
        self.assertEqual(meta["snapshots"], 2)
        self.assertEqual(meta["expected_daily"], 7)
        self.assertEqual(meta["expected_alert_runs"], 112)  # 每天 16 次巡检（9:00–24:00 每小时）
        self.assertIn("打扰你", text)

    def test_build_weekly_flags_missing_runs(self):
        """系统已跑了 7 天，但只有最后一天留下快照 → 必须报缺失。"""
        self._write_state(self.cfg.sim_state_path(), "A", 3000.0)
        self._write_snap(self.days[-1], 1995.0, 795.0)
        text, meta = build_weekly(self.cfg, 7)
        self.assertIn("1/7", text)
        self.assertIn("⚠️", text)                  # 缺日报天数的告警
        self.assertEqual(meta["snapshots"], 1)
        self.assertEqual(meta["expected_daily"], 7)
        self.assertEqual(len(meta["missing_daily"]), 6)

    def test_build_weekly_skips_partial_first_week(self):
        """首周（数据才 2 天）不应误报「有缺失」，否则真故障会被噪音淹没。"""
        self._write_state(self.cfg.sim_state_path(), "A", 3000.0,
                          created=self.days[-2])   # 系统才跑了 2 天
        self._write_snap(self.days[-2], 1990.0, 800.0)
        self._write_snap(self.days[-1], 1995.0, 795.0)
        text, meta = build_weekly(self.cfg, 7)
        self.assertEqual(meta["expected_daily"], 2)      # 只统计系统实际在跑的日子
        self.assertEqual(meta["missing_daily"], [])
        self.assertIn("日报快照 **2/2** 天✅", text)        # 不误报日报缺失

    def test_build_weekly_survives_empty_data(self):
        """没有任何数据也不应抛异常（首周/数据缺失场景）。"""
        text, meta = build_weekly(self.cfg, 7)
        self.assertIn("模拟盘周报", text)
        self.assertEqual(meta["snapshots"], 0)
        self.assertEqual(meta["sim"], [])


if __name__ == "__main__":
    unittest.main()
