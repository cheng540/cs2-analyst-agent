"""模拟盘（纸上交易）单元测试。"""
import tempfile
import unittest
from pathlib import Path

from cs2analyst.analysis.market import MarketEnvironment
from cs2analyst.engine.decision import Action, Decision
from cs2analyst.sim.paper import PaperTrader, reset_state


def mk_market(risk="中风险"):
    return MarketEnvironment(trend="震荡趋势", risk=risk, base_tone="t")


def mk_dec(action, target=12.0, reasons=("测试信号",)):
    return Decision(action=action, conviction=3, score=1.5,
                    target_weight_pct=target, reasons=list(reasons), action_text="t")


class TestPaperTrader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _trader(self, capital=10000.0, hold_days_min=0):
        return PaperTrader(self.state_path, initial_capital=capital,
                           fee_rate=0.01, min_cash_ratio=0.15,
                           high_risk_cash_ratio=0.30, max_position_pct=25.0,
                           hold_days_min=hold_days_min)

    def test_init(self):
        t = self._trader()
        self.assertEqual(t.state["cash"], 10000.0)
        self.assertEqual(t.state["initial_capital"], 10000.0)
        self.assertEqual(t.equity({}), 10000.0)

    def test_buy_flow(self):
        t = self._trader()
        prices = {"A": 100.0}
        d = {"A": mk_dec(Action.BUY, target=12.0)}
        entry = t.execute_day("2026-09-23", mk_market(), d, prices)
        # 12% × 10000 = 1200 → 12 件
        self.assertEqual(t.state["positions"]["A"]["qty"], 12)
        self.assertEqual(t.state["cash"], 8800.0)
        self.assertEqual(entry["trades"], 1)
        self.assertEqual(len(t.state["journal"]), 1)

    def test_add_flow(self):
        t = self._trader()
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        t.execute_day("2026-09-24", mk_market(),
                      {"A": mk_dec(Action.ADD, target=20.0)}, prices)
        # 目标20% = 2000；已有1200 → 补800 → 8件
        self.assertEqual(t.state["positions"]["A"]["qty"], 20)
        self.assertAlmostEqual(t.state["positions"]["A"]["avg_cost"], 100.0)

    def test_add_insufficient_cash_skips(self):
        t = self._trader(capital=500.0)
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=25.0)}, prices)
        # 25%×500=125 → 1件（现金400，储备75）
        self.assertEqual(t.state["positions"]["A"]["qty"], 1)
        # 再加仓：现金不足 → 跳过
        t.execute_day("2026-09-24", mk_market(),
                      {"A": mk_dec(Action.ADD, target=25.0)}, prices)
        self.assertEqual(t.state["positions"]["A"]["qty"], 1)

    def test_reduce_and_clear(self):
        t = self._trader()
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        # 减仓 40%：12件 → ceil(4.8)=5件卖出
        t.execute_day("2026-09-24", mk_market(),
                      {"A": mk_dec(Action.REDUCE, target=6.0)}, prices)
        self.assertEqual(t.state["positions"]["A"]["qty"], 7)
        # 清仓
        t.execute_day("2026-09-25", mk_market(),
                      {"A": mk_dec(Action.CLEAR, target=0.0)}, prices)
        self.assertNotIn("A", t.state["positions"])
        # 卖出收取 1% 手续费：净现金 < 初始 + 0
        self.assertLess(t.state["cash"], 10000.0)

    def test_fee_only_on_sell(self):
        t = self._trader()
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        self.assertEqual(t.state["cash"], 8800.0)  # 买入无手续费
        t.execute_day("2026-09-24", mk_market(),
                      {"A": mk_dec(Action.CLEAR, target=0.0)}, prices)
        # 12件×100×(1-1%) = 1188 → 现金 8800+1188=9988
        self.assertAlmostEqual(t.state["cash"], 9988.0, places=2)

    def test_high_risk_reserve_higher(self):
        t = self._trader(capital=10000.0)
        prices = {"A": 100.0}
        # 高风险：储备 30% → 3000；25%目标=2500 → 现金 10000-2500=7500 ≥3000 可以买
        t.execute_day("2026-09-23", mk_market(risk="高风险"),
                      {"A": mk_dec(Action.BUY, target=25.0)}, prices)
        self.assertEqual(t.state["positions"]["A"]["qty"], 25)

    def test_journal_and_snapshot(self):
        t = self._trader()
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        # 次日价格涨到 110：净值 = 8800 + 12×110 = 10120
        snap = t.snapshot({"A": 110.0})
        self.assertEqual(snap["net_value"], 10120.0)
        self.assertAlmostEqual(snap["return_pct"], 1.2)
        self.assertEqual(len(snap["positions"]), 1)
        self.assertIn("thoughts", snap["journal"][-1])

    def test_reset(self):
        t = self._trader()
        prices = {"A": 100.0}
        t.execute_day("2026-09-23", mk_market(),
                      {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        reset_state(self.state_path)
        self.assertFalse(self.state_path.exists())
        t2 = self._trader()
        self.assertEqual(t2.state["cash"], 10000.0)
        self.assertEqual(t2.state["positions"], {})

    def test_same_day_idempotent(self):
        """同一日期重复执行不产生重复成交与重复记账。"""
        t = self._trader()
        prices = {"A": 100.0}
        e1 = t.execute_day("2026-09-23", mk_market(),
                           {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        e2 = t.execute_day("2026-09-23", mk_market(),
                           {"A": mk_dec(Action.BUY, target=12.0)}, prices)
        self.assertEqual(e1["date"], e2["date"])
        self.assertEqual(t.state["positions"]["A"]["qty"], 12)
        self.assertEqual(len(t.state["journal"]), 1)
        self.assertEqual(len(t.state["trade_log"]), 1)


class TestT7Lock(unittest.TestCase):
    """T+7 交易规则：买入后 7 天内不可卖出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.trader = PaperTrader(self.state_path, initial_capital=10000.0,
                                  fee_rate=0.01, min_cash_ratio=0.0,
                                  max_position_pct=25.0, hold_days_min=7)
        self.prices = {"A": 100.0}
        self.trader.execute_day("2026-09-01", mk_market(),
                                {"A": mk_dec(Action.BUY, target=12.0)}, self.prices)

    def tearDown(self):
        self.tmp.cleanup()

    def test_position_locked_after_buy(self):
        locked, until, days_left = self.trader.lock_status("A", "2026-09-01")
        self.assertTrue(locked)
        self.assertEqual(until, "2026-09-08")
        self.assertEqual(days_left, 7)

    def test_sell_blocked_within_lock(self):
        # 第 3 天尝试清仓 → 被 T+7 拒绝
        entry = self.trader.execute_day("2026-09-04", mk_market(),
                                        {"A": mk_dec(Action.CLEAR, target=0.0)}, self.prices)
        self.assertEqual(entry["trades"], 0)
        self.assertIn("A", self.trader.state["positions"])
        self.assertTrue(entry["locked_notes"])

    def test_sell_allowed_after_lock(self):
        # 第 8 天（2026-09-09）可以卖
        entry = self.trader.execute_day("2026-09-09", mk_market(),
                                        {"A": mk_dec(Action.CLEAR, target=0.0)}, self.prices)
        self.assertEqual(entry["trades"], 1)
        self.assertNotIn("A", self.trader.state["positions"])

    def test_add_extends_lock(self):
        self.trader.execute_day("2026-09-04", mk_market(),
                                {"A": mk_dec(Action.ADD, target=25.0)}, self.prices)
        locked, until, _ = self.trader.lock_status("A", "2026-09-09")
        self.assertTrue(locked)          # 加仓后锁定期顺延
        self.assertEqual(until, "2026-09-11")

    def test_snapshot_reports_lock(self):
        snap = self.trader.snapshot(self.prices, as_of="2026-09-03")
        pos = snap["positions"][0]
        self.assertTrue(pos["locked"])
        self.assertEqual(pos["locked_until"], "2026-09-08")


if __name__ == "__main__":
    unittest.main()
