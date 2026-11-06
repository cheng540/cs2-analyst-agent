"""决策引擎 v2 单元测试：多因子评分、交易叙事、仓位管理与优先级。"""
import unittest

from cs2analyst.analysis.item import ItemAnalysis
from cs2analyst.analysis.market import MarketEnvironment
from cs2analyst.datasource.inventory import Holding
from cs2analyst.engine.decision import Action, decide

CFG = {"max_position_pct": 25.0}
EQUITY = 10000.0


def mk_market(trend="震荡趋势", risk="中风险") -> MarketEnvironment:
    return MarketEnvironment(trend=trend, risk=risk, base_tone="t")


def mk_item(**kw) -> ItemAnalysis:
    qty = kw.pop("quantity", 10)
    base = dict(
        holding=Holding(market_hash_name="Test Item", quantity=qty, buy_price=100.0),
        cur_price=100.0, pnl_pct=0.0, weight=0.10,
        trend="震荡趋势", ma_state="交叉缠绕",
        liquidity_grade="高",
        volume_state="平量", amp_ratio=1.0, ret_5d=0.0, ret_3d=0.0,
        position_pct=50.0, position_label="中位",
        stagnation=False, vol_up_down=False, vol_up_stall=False,
        shrink_stop_falling=False, break_support=False,
        nearest_support=90.0, nearest_resistance=110.0,
        strong_support=90.0, broken_support=None, avg7_price=100.0,
        near_support=False, near_resistance=False,
    )
    base.update(kw)
    return ItemAnalysis(**base)


class TestBreakdownNarrative(unittest.TestCase):
    def test_break_and_volume_down_clears_position(self):
        item = mk_item(break_support=True, vol_up_down=True, broken_support=105.0,
                       ret_5d=-8.0, quantity=5)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.CLEAR)
        self.assertEqual(d.conviction, 5)

    def test_break_while_flat_stays_flat(self):
        # 破位放量下跌途中、无持仓 → 不接飞刀
        item = mk_item(break_support=True, vol_up_down=True, broken_support=105.0,
                       ret_5d=-8.0, quantity=0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.HOLD)


class TestStallNarrative(unittest.TestCase):
    def test_resistance_stall_reduces_profit(self):
        item = mk_item(near_resistance=True, vol_up_stall=True, pnl_pct=0.3)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.REDUCE)

    def test_resistance_stall_no_position_no_reduce(self):
        item = mk_item(near_resistance=True, vol_up_stall=True, pnl_pct=0.0, quantity=0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertNotEqual(d.action, Action.REDUCE)


class TestLeftSideNarrative(unittest.TestCase):
    def test_left_side_probe_buy(self):
        # 低位 + 缩量止跌 + 大盘中风险 → 允许小仓试探（即使短期趋势弱）
        item = mk_item(position_pct=10.0, shrink_stop_falling=True,
                       trend="下降趋势", ma_state="交叉缠绕", quantity=0)
        d = decide(mk_market(trend="震荡趋势", risk="中风险"), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.BUY)
        self.assertEqual(d.conviction, 2)

    def test_left_side_blocked_when_high_risk(self):
        item = mk_item(position_pct=10.0, shrink_stop_falling=True,
                       trend="下降趋势", ma_state="交叉缠绕", quantity=0)
        d = decide(mk_market(trend="下降趋势", risk="高风险"), item, CFG, EQUITY)
        self.assertNotEqual(d.action, Action.BUY)

    def test_left_side_blocked_when_bearish_alignment(self):
        item = mk_item(position_pct=10.0, shrink_stop_falling=True,
                       trend="下降趋势", ma_state="空头排列", quantity=0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertNotEqual(d.action, Action.BUY)


class TestScoreBands(unittest.TestCase):
    def test_strong_bull_buys(self):
        item = mk_item(ma_state="多头排列", position_pct=30.0, quantity=0,
                       liquidity_grade="高", ret_5d=3.0)
        d = decide(mk_market(trend="上升趋势", risk="低风险"), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.BUY)
        self.assertGreaterEqual(d.conviction, 3)
        self.assertIsNotNone(d.target_weight_pct)

    def test_deep_bear_clears(self):
        item = mk_item(ma_state="空头排列", vol_up_down=True, liquidity_grade="低",
                       pnl_pct=-0.35, quantity=5)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.CLEAR)

    def test_mid_band_holds(self):
        item = mk_item()
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.HOLD)

    def test_deep_loss_with_reversal_not_full_clear(self):
        # 深套 + 止跌信号：减仓控制风险而非全清
        item = mk_item(ma_state="空头排列", shrink_stop_falling=True, liquidity_grade="低",
                       pnl_pct=-0.35, quantity=5, vol_up_down=False)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.REDUCE)

    def test_deep_loss_flat_volume_no_break_not_full_clear(self):
        """深套 + 平量 + 未破位 → 不该信心 5 清仓（不砍在最低）。

        2026-09-30 实测案例：海豹蛙人（-51%、位置 1%、平量、未破位）被评分带
        （低流动性 -1.5 + 大盘高风险 -0.8 + 深套无止跌 -0.5）推成「清仓 信心 5」，
        等于"因为它不好卖所以赶紧卖"，与流动性差应当慢出的常识相反。
        """
        item = mk_item(ma_state="空头排列", liquidity_grade="低", pnl_pct=-0.51,
                       quantity=1, shrink_stop_falling=False, break_support=False,
                       vol_up_down=False, volume_state="平量")
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.REDUCE)
        self.assertLess(d.conviction, 5)
        self.assertTrue(any("不砍在最低" in r for r in d.reasons))

    def test_deep_loss_volume_dump_still_clears(self):
        """但真在放量跳水（或已破位）时，仍然要果断清仓，不能被上面那条保护挡住。"""
        dumped = mk_item(ma_state="空头排列", liquidity_grade="低", pnl_pct=-0.51,
                         quantity=1, vol_up_down=True)
        self.assertEqual(decide(mk_market(), dumped, CFG, EQUITY).action, Action.CLEAR)
        broken = mk_item(ma_state="空头排列", liquidity_grade="低", pnl_pct=-0.51,
                         quantity=1, break_support=True, vol_up_down=False)
        self.assertEqual(decide(mk_market(), broken, CFG, EQUITY).action, Action.CLEAR)

    def test_illiquid_exit_advises_limit_order(self):
        """流动性低的减仓/清仓建议，必须给出「挂最低在售价 − 0.01」的执行方式。

        来自水纹之印那笔真实亏损（自挂 ¥25，当天最低在售价 ¥27.50，低 9.1%）。
        """
        item = mk_item(ma_state="空头排列", liquidity_grade="低", pnl_pct=-0.51,
                       quantity=1, volume_state="平量")
        text = decide(mk_market(), item, CFG, EQUITY).action_text
        self.assertIn("最低在售价", text)
        self.assertIn("卖给求购", text)

    def test_high_risk_blocks_weak_buy_signal(self):
        """高风险下「信心 2」的弱信号不许给新开/补仓（基调写着禁止，行为必须一致）。

        2026-10-01 用户指出「居然提示我加仓」——当时 score 1.x → 信心 2 → 加仓，
        而同一份报告的大盘基调写的是"禁止新开与补仓"。
        """
        weak = mk_item(ma_state="多头排列", position_pct=20.0, quantity=1,
                       liquidity_grade="高", ret_5d=1.0)
        d = decide(mk_market(trend="下降趋势", risk="高风险"), weak, CFG, EQUITY)
        self.assertEqual(d.action, Action.HOLD)
        self.assertTrue(any("大盘高风险" in r for r in d.reasons))
        # 同样的弱信号在低风险环境里不受这条限制
        d2 = decide(mk_market(trend="上升趋势", risk="低风险"), weak, CFG, EQUITY)
        self.assertIn(d2.action, (Action.BUY, Action.ADD))

    def test_unaffordable_target_becomes_hold(self):
        """一件就突破单标的上限 → 不给买入建议（与模拟盘 `target_amount >= price` 一致）。

        2026-10-01 实例：权益 ¥10,000 时，弹雨 ¥515 只占 5%，属于"最小单位可执行"；
        真正不能碰的是"一件就超过 25% 上限"的情况（用户组合约 ¥1,800 对上 ¥515 的弹雨）。
        """
        too_big = mk_item(ma_state="多头排列", position_pct=20.0, quantity=1,
                          liquidity_grade="高", ret_5d=2.0)
        too_big.cur_price = EQUITY * 0.30        # 一件 = 组合 30% > 25% 上限
        d = decide(mk_market(trend="上升趋势", risk="低风险"), too_big, CFG, EQUITY)
        self.assertEqual(d.action, Action.HOLD)
        self.assertTrue(any("超过单标的上限" in r for r in d.reasons))
        self.assertIsNone(d.target_weight_pct)

    def test_minimum_lot_kept_when_within_cap(self):
        """一件在上限内、只是大于标准仓位 → 保留建议，但改成「1 件」并说明是超配。

        这是对 2026-10-01 用户追问「那以后我都只能买 36 块钱的东西了？」的直接回应：
        第一版守卫一律降级为观望，会让小资金账户永远无法执行任何建议。
        """
        pricey = mk_item(ma_state="多头排列", position_pct=20.0, quantity=0,
                         liquidity_grade="高", ret_5d=2.0)
        # 权益 ¥10,000：该信号标准仓位 14.4% ≈ ¥1,440，一件 ¥2,000 买不到，
        # 但一件只占 20%（上限 25% 之内）→ 应按「1 件」执行
        pricey.cur_price = 2000.0
        d = decide(mk_market(trend="上升趋势", risk="低风险"), pricey, CFG, EQUITY)
        self.assertIn(d.action, (Action.BUY, Action.ADD))
        self.assertIsNotNone(d.target_weight_pct)
        # 目标仓位要被抬到「1 件」对应的权重（20%）
        self.assertAlmostEqual(d.target_weight_pct, 20.0, places=1)
        self.assertTrue(any("最小可执行单位" in r for r in d.reasons))
        # 只有一件时，文案不能再写"分 2~3 笔建仓"
        self.assertIn("1 件", d.action_text)
        self.assertNotIn("分 2~3 笔", d.action_text)


class TestRelativeStrength(unittest.TestCase):
    """相对强度：识别「普跌里逆势独涨」（2026-10-01 用户提的场景）。"""

    def test_strong_relative_strength_raises_score(self):
        # 同一个标的，只在"近5日涨跌"上不同：大盘近5日 -5%
        base = dict(ma_state="交叉缠绕", trend="震荡趋势", position_pct=40.0,
                    liquidity_grade="高", quantity=0)
        flat = mk_item(**base)                       # ret_5d 默认 0 → RS = +5 → 不加分
        strong = mk_item(**base, ret_5d=10.0)        # RS = +15 → +1.5 分
        mkt = mk_market(trend="下降趋势", risk="高风险")
        mkt.ret_5d = -5.0
        d_flat = decide(mkt, flat, CFG, EQUITY)
        d_strong = decide(mkt, strong, CFG, EQUITY)
        self.assertGreater(d_strong.score, d_flat.score)
        self.assertTrue(any("逆势独涨" in r for r in d_strong.reasons))

    def test_lagging_item_penalised(self):
        lag = mk_item(ma_state="交叉缠绕", trend="震荡趋势", position_pct=40.0,
                      liquidity_grade="高", quantity=0, ret_5d=-20.0)
        mkt = mk_market(trend="震荡趋势", risk="中风险")
        mkt.ret_5d = -5.0        # RS = -15 → 扣 0.5
        d = decide(mkt, lag, CFG, EQUITY)
        self.assertTrue(any("跑输大盘" in r for r in d.reasons))

    def test_lagging_not_double_counted_when_trend_already_down(self):
        """自身已是下降趋势时，"跑输大盘"不再重复扣分（避免双重计分）。

        2026-10-01 实测：demo 里 M4A4 龙王 本来 -3.0 恰好落在"减仓"，
        被重复扣成 -3.5 → "清仓"，把端到端测试里减仓这条路径覆盖弄没了。
        """
        lag = mk_item(ma_state="空头排列", trend="下降趋势", position_pct=10.0,
                      liquidity_grade="高", quantity=0, ret_5d=-20.0)
        mkt = mk_market(trend="震荡趋势", risk="中风险")
        mkt.ret_5d = -5.0
        d = decide(mkt, lag, CFG, EQUITY)
        self.assertFalse(any("跑输大盘" in r for r in d.reasons))

    def test_skipped_when_market_return_unknown(self):
        item = mk_item(ma_state="交叉缠绕", trend="震荡趋势", position_pct=40.0,
                       liquidity_grade="高", quantity=0, ret_5d=30.0)
        d = decide(mk_market(), item, CFG, EQUITY)      # 大盘 ret_5d 为 None
        self.assertFalse(any("独涨" in r for r in d.reasons))


class TestAddBlockedInWeakMarket(unittest.TestCase):
    """补仓必须服从 MarketEnvironment.block_add()（文字与行为一致）。"""

    def test_add_blocked_in_high_risk_or_downtrend(self):
        held = mk_item(ma_state="多头排列", position_pct=20.0, quantity=1,
                       liquidity_grade="高", ret_5d=2.0)
        weak = mk_market(trend="下降趋势", risk="高风险")
        self.assertTrue(weak.block_add())
        d = decide(weak, held, CFG, EQUITY)
        self.assertEqual(d.action, Action.HOLD)
        self.assertTrue(any("不补仓" in r for r in d.reasons))

    def test_add_allowed_in_normal_market(self):
        held = mk_item(ma_state="多头排列", position_pct=20.0, quantity=1,
                       liquidity_grade="高", ret_5d=2.0)
        calm = mk_market(trend="上升趋势", risk="低风险")
        self.assertFalse(calm.block_add())
        d = decide(calm, held, CFG, EQUITY)
        self.assertIn(d.action, (Action.ADD, Action.BUY))

    def test_new_buy_still_possible_in_weak_market(self):
        """弱市禁的是"补仓摊平"，不是所有买入——强信号（信心 ≥3）仍可小仓新开。"""
        fresh = mk_item(ma_state="多头排列", trend="上升趋势", position_pct=20.0,
                        quantity=0, liquidity_grade="高", ret_5d=10.0)
        weak = mk_market(trend="下降趋势", risk="高风险")
        weak.ret_5d = -5.0          # 逆势独涨 → 额外 +1.5 → 信心 4
        d = decide(weak, fresh, CFG, EQUITY)
        self.assertEqual(d.action, Action.BUY)
        self.assertIsNotNone(d.target_weight_pct)


class TestSizing(unittest.TestCase):
    def test_high_risk_halves_sizing(self):
        item = mk_item(ma_state="多头排列", position_pct=30.0, quantity=0)
        d_lo = decide(mk_market(trend="上升趋势", risk="低风险"), item, CFG, EQUITY)
        d_hi = decide(mk_market(trend="上升趋势", risk="高风险"), item, CFG, EQUITY)
        # 注意：高风险下弱信号会被降级为观望（见 test_high_risk_blocks_weak_buy_signal），
        # 那本身也是"更保守"。这里要保证的是：**高风险永远不会给出更大的仓位**。
        if d_hi.target_weight_pct is None:
            self.assertIsNone(d_hi.target_weight_pct)
        else:
            self.assertLess(d_hi.target_weight_pct, d_lo.target_weight_pct)

    def test_sizing_capped(self):
        item = mk_item(ma_state="多头排列", position_pct=30.0, quantity=0)
        d = decide(mk_market(trend="上升趋势", risk="低风险"), item, CFG, EQUITY)
        self.assertLessEqual(d.target_weight_pct, 25.0)


class TestMarketContextNotHardGate(unittest.TestCase):
    def test_high_risk_can_still_buy_strong_setup(self):
        # 大盘高风险不再绝对禁止买入：强信号仍可小仓（敞口系数已降低）
        item = mk_item(ma_state="多头排列", position_pct=20.0, quantity=0,
                       liquidity_grade="高", shrink_stop_falling=True)
        d = decide(mk_market(trend="下降趋势", risk="高风险"), item, CFG, EQUITY)
        self.assertIn(d.action, (Action.BUY, Action.HOLD))
        if d.action == Action.BUY:
            self.assertLessEqual(d.target_weight_pct, 15.0)


class TestUserSellIntent(unittest.TestCase):
    """用户已决定卖出时，系统按用户意图给清仓建议，不再建议持有。"""

    def _item_with_intent(self, **kw):
        h = Holding(market_hash_name="Test Item", quantity=2, buy_price=199.0,
                    lots=[164.0, 234.0], status="待售")
        return mk_item(holding=h, quantity=2, cur_price=162.8, pnl_pct=-0.182, **kw)

    def test_sell_intent_forces_clear(self):
        d = decide(mk_market(), self._item_with_intent(), CFG, EQUITY)
        self.assertEqual(d.action, Action.CLEAR)
        self.assertEqual(d.conviction, 5)
        self.assertIn("你已决定卖出", d.reasons[0])

    def test_sell_intent_lists_per_lot_cost_and_pnl(self):
        d = decide(mk_market(), self._item_with_intent(), CFG, EQUITY)
        joined = " ".join(d.reasons)
        self.assertIn("¥234.00", joined)          # 高的那件在前
        self.assertIn("¥164.00", joined)
        self.assertIn("-30.4%", joined)           # 234 → 162.8
        self.assertIn("-0.7%", joined)            # 164 → 162.8

    def test_sell_intent_beats_break_support(self):
        """即使同时满足"破位放量"，也走用户意图那条（不重复讲技术面）。"""
        item = self._item_with_intent(break_support=True, vol_up_down=True, broken_support=180.0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertEqual(d.action, Action.CLEAR)
        self.assertIn("你已决定卖出", d.reasons[0])

    def test_no_intent_keeps_normal_logic(self):
        h = Holding(market_hash_name="Test Item", quantity=2, buy_price=199.0, lots=[164.0, 234.0])
        item = mk_item(holding=h, quantity=2, cur_price=199.0, pnl_pct=0.0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertNotIn("你已决定卖出", " ".join(d.reasons))
        self.assertNotEqual(d.action, Action.CLEAR)

    def test_flat_holding_with_intent_not_affected(self):
        h = Holding(market_hash_name="Test Item", quantity=0, buy_price=0.0, status="待售")
        item = mk_item(holding=h, quantity=0)
        d = decide(mk_market(), item, CFG, EQUITY)
        self.assertNotIn("你已决定卖出", " ".join(d.reasons))


if __name__ == "__main__":
    unittest.main()
