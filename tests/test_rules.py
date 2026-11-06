"""平台交易规则文本的一致性测试。

背景：曾出现「告警 AI 点评的提示词里漏掉 T+7 与手续费」的问题。
这里锁定三个 AI 场景都必须带上平台规则，避免再次遗漏。
"""
import unittest

from cs2analyst.config import load_config
from cs2analyst.rules import trading_rules_brief, trading_rules_text


class TestRulesText(unittest.TestCase):
    def test_text_contains_key_constraints(self):
        txt = trading_rules_text({"hold_days_min": 7, "fee_rate": 0.01,
                                  "withdraw_fee_rate": 0.01})
        self.assertIn("T+7", txt)
        self.assertIn("7 天内不可卖出", txt)
        self.assertIn("卖出收 1%", txt)
        self.assertIn("锁定期内无法止损", txt)

    def test_fee_structure_is_two_tier(self):
        """费率结构必须是「卖出 1% + 提现另 1%」两档，且区分两种用途。

        2026-09-24 用户更正：此前写成 2.5% 是我自己假设的数字、从未核实，
        后来一路传到了报告、提示词和求职材料里——这个测试用来防止再次漂移。
        """
        txt = trading_rules_text({"hold_days_min": 7, "fee_rate": 0.01,
                                  "withdraw_fee_rate": 0.01})
        self.assertIn("买入不收手续费", txt)
        self.assertIn("提现", txt)
        self.assertIn("1.99%", txt)          # 卖出 + 提现的复利合计
        self.assertNotIn("2.5%", txt)        # 旧错误数字不得再出现

    def test_cashout_cost_pct(self):
        from cs2analyst.rules import cashout_cost_pct, fee_rates
        self.assertEqual(fee_rates({"fee_rate": 0.01, "withdraw_fee_rate": 0.01}),
                         (0.01, 0.01))
        self.assertEqual(cashout_cost_pct({"fee_rate": 0.01, "withdraw_fee_rate": 0.01}), 1.99)
        # 不提现（提现费率为 0）→ 只有卖出费
        self.assertEqual(cashout_cost_pct({"fee_rate": 0.01, "withdraw_fee_rate": 0}), 1.0)

    def test_text_follows_config(self):
        txt = trading_rules_text({"hold_days_min": 3, "fee_rate": 0.05,
                                  "withdraw_fee_rate": 0.02})
        self.assertIn("T+3", txt)
        self.assertIn("5%", txt)
        self.assertIn("2%", txt)

    def test_brief_is_one_line(self):
        brief = trading_rules_brief({"hold_days_min": 7, "fee_rate": 0.01,
                                     "withdraw_fee_rate": 0.01})
        self.assertNotIn("\n", brief)
        self.assertIn("T+7", brief)
        self.assertIn("卖出费 1%", brief)
        self.assertIn("提现另收 1%", brief)

    def test_defaults_without_config(self):
        txt = trading_rules_text(None)
        self.assertIn("T+7", txt)
        self.assertIn("1%", txt)


class TestPromptsIncludeRules(unittest.TestCase):
    """三个 AI 场景的提示词都必须包含平台规则。"""

    def setUp(self):
        self.cfg = load_config()

    def test_chat_assistant_prompt(self):
        from cs2analyst.chat.assistant import SYSTEM_PROMPT
        self.assertIn("T+7", SYSTEM_PROMPT)
        self.assertIn("手续费", SYSTEM_PROMPT)

    def test_daily_analyst_prompt(self):
        from cs2analyst.llm.analyst import SYSTEM_PROMPT
        self.assertIn("T+7", SYSTEM_PROMPT)
        self.assertIn("手续费", SYSTEM_PROMPT)

    def test_alert_comment_prompt_uses_shared_rules(self):
        """告警点评的提示词必须引用共享规则文本（含 T+7 与手续费）。"""
        import inspect

        from cs2analyst.alerts.checker import AlertChecker
        src = inspect.getsource(AlertChecker.ai_comment)
        self.assertIn("trading_rules_text", src)
        self.assertIn("T+N", src)          # 提示模型把锁定期算进去

    def test_alert_push_footer_has_rules(self):
        import inspect

        from cs2analyst.alerts.checker import AlertChecker
        src = inspect.getsource(AlertChecker._footer)
        self.assertIn("trading_rules_brief", src)

    def test_sell_action_text_uses_correct_fee(self):
        """「已决定卖出」的建议文案里的费率必须与平台一致（1% + 提现 1%）。"""
        from cs2analyst.analysis.item import ItemAnalysis
        from cs2analyst.analysis.market import MarketEnvironment
        from cs2analyst.datasource.inventory import Holding
        from cs2analyst.engine.decision import decide
        item = ItemAnalysis(
            holding=Holding(market_hash_name="X", quantity=1, buy_price=100.0, status="待售"),
            cur_price=100.0, pnl_pct=0.0, weight=0.1, trend="震荡趋势", ma_state="交叉缠绕",
            liquidity_grade="高", volume_state="平量", amp_ratio=1.0, ret_5d=0.0, ret_3d=0.0,
            position_pct=50.0, position_label="中位", stagnation=False, vol_up_down=False,
            vol_up_stall=False, shrink_stop_falling=False, break_support=False,
            nearest_support=90.0, nearest_resistance=110.0, strong_support=90.0,
            broken_support=None, avg7_price=100.0, near_support=False, near_resistance=False)
        d = decide(MarketEnvironment(trend="震荡趋势", risk="中风险", base_tone="t"),
                   item, {"max_position_pct": 25.0}, 10000.0)
        self.assertIn("1%", d.action_text)
        self.assertNotIn("2.5", d.action_text)


if __name__ == "__main__":
    unittest.main()
