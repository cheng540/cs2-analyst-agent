"""决策引擎 v2：多因子评分 + 交易叙事 + 仓位管理。

设计理念（不再使用机械四级规则）：
- 综合趋势、位置、量价、流动性、大盘背景、估值/盈亏六个因子打分；
- 用「交易叙事」处理关键形态（破位放量、高位滞涨、左侧机会、超跌反弹）；
- 大盘风险是「敞口系数」而非硬性开关——高风险时降低仓位上限，
  但低位企稳的优质标的仍允许小仓左侧参与；
- 输出带信心度的动作 + 目标仓位建议，供真实持仓参考与模拟盘执行。

动作集：BUY 买入建仓 / ADD 加仓 / HOLD 持有观望 / REDUCE 分批减仓 / CLEAR 清仓离场
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..analysis.item import ItemAnalysis
from ..analysis.market import MarketEnvironment


class Action(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    CLEAR = "CLEAR"


LABELS: dict[Action, str] = {
    Action.BUY: "🟢 买入建仓",
    Action.ADD: "🟢 加仓",
    Action.HOLD: "🟡 持有观望",
    Action.REDUCE: "🟠 分批减仓",
    Action.CLEAR: "🔴 清仓离场",
}

# 信念度 → 基础目标仓位（占总权益%）
_CONVICTION_WEIGHT = {5: 25.0, 4: 18.0, 3: 12.0, 2: 6.0, 1: 3.0}


@dataclass
class Decision:
    """单品决策。"""
    action: Action
    conviction: int = 3                 # 信心度 1-5
    score: float = 0.0                  # 综合评分（诊断用）
    target_weight_pct: float | None = None  # 建议目标仓位（占总权益%）
    reasons: list[str] = field(default_factory=list)
    action_text: str = ""

    @property
    def label(self) -> str:
        return LABELS[self.action]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "label": self.label,
            "conviction": self.conviction,
            "score": round(self.score, 2),
            "target_weight_pct": self.target_weight_pct,
            "reasons": self.reasons,
            "action_text": self.action_text,
        }


def _score_item(market: MarketEnvironment, item: ItemAnalysis) -> tuple[float, list[str]]:
    """多因子评分。返回 (score, 理由)。"""
    score = 0.0
    reasons: list[str] = []

    # ---- 1. 趋势因子 ----
    if item.ma_state == "多头排列":
        score += 2.0
        reasons.append("均线多头排列，趋势健康")
    elif item.ma_state == "空头排列":
        score -= 2.0
        reasons.append("均线空头排列，趋势承压")
    elif item.trend == "上升趋势":
        score += 1.2
        reasons.append("短期趋势向上")
    elif item.trend == "下降趋势":
        score -= 1.2
        reasons.append("短期趋势向下")

    # ---- 2. 位置因子（近6个月百分位） ----
    if item.position_pct is not None:
        if item.position_pct <= 25:
            score += 0.5
            reasons.append(f"价格处于近6个月低位区间（{item.position_pct:.0f}%）")
        elif item.position_pct >= 75:
            score -= 1.0
            reasons.append(f"价格处于近6个月高位区间（{item.position_pct:.0f}%）")
        if item.position_pct >= 90 and item.stagnation:
            score -= 0.5
            reasons.append("高位滞涨，注意回落风险")
    elif item.ret_6m is not None and item.ret_6m <= -40:
        score += 0.3
        reasons.append(f"6个月累计跌幅 {item.ret_6m:.0f}%，处于深度回调区")

    # ---- 3. 量价因子 ----
    if item.vol_up_down:
        score -= 1.5
        reasons.append("放量下跌，抛压明显")
    elif item.shrink_stop_falling:
        score += 0.8
        reasons.append("缩量止跌，卖压衰竭")
    elif item.vol_up_stall:
        score -= 0.8
        reasons.append("放量滞涨，上方压力沉重")

    # ---- 4. 流动性因子 ----
    if item.liquidity_grade == "高":
        score += 0.5
    elif item.liquidity_grade == "低":
        score -= 1.5
        reasons.append("流动性低，进出困难、滑价风险大")
    # 「数据不足」不惩罚：缺数据 ≠ 流动性差

    # ---- 5. 大盘背景因子（敞口调节，非硬性屏蔽） ----
    if market.risk == "低风险":
        score += 0.5
    elif market.risk == "高风险":
        score -= 0.8
        reasons.append("大盘高风险，控制敞口")

    # ---- 5.5 相对强度：单品 vs 大盘（识别「普跌里逆势独涨」）----
    # 用户 2026-10-01 提的场景：「十分看好一个单品，它破趋势独涨」。
    # 原先评分只有单品自身趋势 + 一个全局大盘罚分 → **独涨和随大流下跌受同样的市场惩罚**，
    # 引擎看不见"跑赢大盘"这件事，于是这类机会永远拿不到更高信心度、也就永远买不起。
    # 相对强度是趋势策略里最经典的正向因子之一，这里补上（任一侧缺数据就跳过）。
    if item.ret_5d is not None and market.ret_5d is not None:
        try:
            rs = float(item.ret_5d) - float(market.ret_5d)
        except (TypeError, ValueError):
            rs = None
        if rs is not None:
            if rs >= 8:
                score += 1.5
                reasons.append(f"逆势独涨：近5日跑赢大盘 {rs:+.1f} 个百分点"
                               f"（单品 {float(item.ret_5d):+.1f}% vs 大盘 {float(market.ret_5d):+.1f}%）")
            elif rs >= 4:
                score += 0.8
                reasons.append(f"相对强度偏强：近5日跑赢大盘 {rs:+.1f} 个百分点")
            elif rs <= -8 and item.trend != "下降趋势":
                # 只在「自身趋势还没走坏」时扣分：已经空头排列/放量下跌的标的，
                # 跑输大盘是同一件事的重复表述，再扣一次就是双重计分
                # （2026-10-01 实测：M4A4 龙王 本来 -3.0 恰好落在"减仓"，被重复扣成"清仓"）。
                score -= 0.5
                reasons.append(f"明显跑输大盘 {abs(rs):.1f} 个百分点"
                               f"（近5日 {float(item.ret_5d):+.1f}%）")

    # ---- 6. 估值 / 盈亏因子 ----
    if item.avg7_price and item.cur_price and item.avg7_price > 0:
        dev = (item.cur_price - item.avg7_price) / item.avg7_price
        if dev <= -0.03:
            score += 0.4
            reasons.append(f"现价低于7日均价 {abs(dev)*100:.1f}%，短期有估值折价")
        elif dev >= 0.05:
            score -= 0.3
            reasons.append(f"现价高于7日均价 {dev*100:.1f}%，短期略贵")
    if item.pnl_pct <= -0.30 and item.shrink_stop_falling:
        score += 0.6
        reasons.append("深度套牢但出现止跌信号，存在超跌反弹机会")
    elif item.pnl_pct <= -0.30:
        score -= 0.5
        reasons.append(f"持仓亏损 {item.pnl_pct*100:.0f}% 且无止跌迹象")
    elif item.pnl_pct >= 0.50:
        score -= 0.5
        reasons.append(f"浮盈 {item.pnl_pct*100:.0f}%，注意锁定利润")

    return score, reasons


def _sizing(conviction: int, market: MarketEnvironment, max_position: float) -> float | None:
    """由信心度与大盘环境计算目标仓位。"""
    base = _CONVICTION_WEIGHT.get(conviction, 6.0)
    if market.risk == "高风险":
        base *= 0.6
    elif market.risk == "低风险" and market.trend == "上升趋势":
        base *= 1.2
    return round(min(base, max_position), 1)


def decide(market: MarketEnvironment, item: ItemAnalysis,
           cfg: dict, equity: float,
           has_position: bool | None = None) -> Decision:
    """综合判断：评分定基调，叙事定动作，输出信心度与目标仓位。

    equity: 组合总权益（用于仓位建议）；has_position 为 None 时
    按 item.holding.quantity 推断是否持有。
    """
    if has_position is None:
        has_position = item.holding.quantity > 0
    score, reasons = _score_item(market, item)
    max_position = float(cfg.get("max_position_pct", 25.0))
    action = Action.HOLD
    conviction = 3

    # ------------------------------------------------------------
    # 叙事零（最高优先级）：用户已决定卖出 → 按用户意图给清仓建议
    # 系统不替用户改主意，只把执行层面信息补齐：分件成本、逐件盈亏、费用与上架方式。
    # ------------------------------------------------------------
    if has_position and getattr(item.holding, "sell_intent", False):
        lots = list(getattr(item.holding, "lot_prices", []) or [])
        lot_txt = ""
        try:
            if len(lots) > 1 and len({round(v, 2) for v in lots}) > 1 and item.cur_price:
                ordered = sorted(lots, reverse=True)
                lot_txt = "；分件成本 " + " / ".join(f"¥{v:,.2f}" for v in ordered)
                lot_txt += "（逐件盈亏 " + " / ".join(
                    f"{(float(item.cur_price) / v - 1) * 100:+.1f}%" for v in ordered) + "）"
        except (TypeError, ValueError, ZeroDivisionError):
            lot_txt = ""
        reasons.insert(0, f"你已决定卖出该仓位（{item.holding.status or '待售'}）{lot_txt}——"
                          "系统按你的决定直接给清仓建议，不再建议持有")
        return Decision(action=Action.CLEAR, conviction=5, score=round(score, 2),
                        target_weight_pct=0.0, reasons=reasons,
                        action_text="按你的决定执行：以「最低在售价」上架（宁可略低也别挂着等），"
                                    "成交后扣 1% 卖出费即为钱包余额；若要提现成现金，再扣 1%。")

    # ------------------------------------------------------------
    # 叙事一（最高优先级）：有效跌破前支撑 + 放量下跌 → 离场
    # ------------------------------------------------------------
    if item.break_support and item.vol_up_down:
        if has_position:
            action = Action.CLEAR
            conviction = 5
            reasons.insert(0, f"已有效跌破前支撑 {item.broken_support} 且放量下跌，形态破坏")
        else:
            action = Action.HOLD
            conviction = 1
            reasons.insert(0, f"破位放量下跌途中，不接飞刀，空仓观望")
        return Decision(action=action, conviction=conviction, score=round(score, 2),
                        target_weight_pct=None if action == Action.HOLD else 0.0,
                        reasons=reasons,
                        action_text=_action_text(action, conviction, None, item))

    # ------------------------------------------------------------
    # 叙事二：高位放量滞涨 → 减仓落袋
    # ------------------------------------------------------------
    if item.near_resistance and item.vol_up_stall and has_position and item.pnl_pct > 0:
        action = Action.REDUCE
        conviction = 4
        reasons.insert(0, f"价格运行至压力位 {item.nearest_resistance} 附近且放量滞涨，减仓锁定利润")
        return Decision(action=action, conviction=conviction, score=round(score, 2),
                        target_weight_pct=_sizing(2, market, max_position),
                        reasons=reasons,
                        action_text="建议先减 1/3~1/2 仓位落袋，剩余仓位设好止损，若放量突破压力位可回补。")

    # ------------------------------------------------------------
    # 叙事三：左侧机会——低位 + 缩量止跌 + 大盘非高风险 → 小仓试探
    # （下降周期不绝对禁止参与，而是以更小仓位、更严格信号介入）
    # ------------------------------------------------------------
    if (item.position_pct is not None and item.position_pct <= 25
            and item.shrink_stop_falling and market.risk != "高风险"
            and item.ma_state != "空头排列" and not item.break_support):
        score += 1.0
        reasons.append("左侧机会：低位缩量止跌，卖压衰竭，可小仓试探")

    # ------------------------------------------------------------
    # 评分带 → 动作
    # ------------------------------------------------------------
    if score >= 2.5:
        action = Action.ADD if has_position else Action.BUY
        conviction = min(5, 3 + int(score - 2.5))
    elif score >= 1.0:
        action = Action.ADD if has_position else Action.BUY
        conviction = 2
        reasons.append("信号偏多但尚不充分，建议小仓试探")
    elif score < -3.0:
        action = Action.CLEAR if has_position else Action.HOLD
        conviction = 5 if has_position else 1
        if not has_position:
            reasons.append("综合评分极弱，空仓回避")
    elif score < -1.5:
        action = Action.REDUCE if has_position else Action.HOLD
        conviction = 4 if has_position else 1
        if not has_position:
            reasons.append("评分偏弱，暂不介入")
    else:
        action = Action.HOLD
        conviction = 3

    # ------------------------------------------------------------
    # 深套但「形态没被破坏」→ 不砍在最低（全清只留给真信号）
    # 全清（信心 5）的正当理由只有：叙事一（有效跌破前支撑 + 放量下跌），
    # 或本身就在放量跳水；除此之外，深套 + 未破位 + 平量/缩量 → 降级为分批减仓。
    #
    # 为什么改（2026-09-30）：原先只在「缩量止跌」时才降级，于是像海豹蛙人这种
    # 「平量 + 深套 51% + 未破位」会被评分带推到信心 5 清仓——
    # 低流动性 -1.5、大盘高风险 -0.8、深套无止跌 -0.5，三项就把分数压到 -3 以下，
    # 结果等于"因为它不好卖，所以赶紧卖"，与"流动性差应少动、慢出"的常识相反，
    # 也和本函数自己的注释「不砍在最低」矛盾。它同时还踩了水纹之印那个坑：
    # 清淡品种急着出手，成交价会明显低于挂牌价。
    # ------------------------------------------------------------
    deep_loss = item.pnl_pct <= -0.30
    if action == Action.CLEAR and deep_loss and (
            item.shrink_stop_falling
            or (not item.break_support and not item.vol_up_down)):
        action = Action.REDUCE
        conviction = 3
        reasons.append(
            "深套中出现止跌信号，先减仓控制风险而非全清" if item.shrink_stop_falling
            else "深套但未破位、也没有放量跳水：不砍在最低，先减 1/3~1/2，"
                 "若放量跌破支撑再清仓")

    # 盈利仓位在系统性下跌中分批减仓即可，除非形态已破坏（避免恐慌式全清）
    if (action == Action.CLEAR and item.pnl_pct >= 0
            and not (item.break_support and item.vol_up_down)):
        action = Action.REDUCE
        conviction = 4
        reasons.append("持仓仍有浮盈，先分批减仓落袋；若后续跌破关键支撑再考虑清仓")

    target = None
    if action in (Action.BUY, Action.ADD):
        target = _sizing(conviction, market, max_position)
    elif action == Action.CLEAR:
        target = 0.0
    elif action == Action.REDUCE:
        target = _sizing(2, market, max_position)

    # ------------------------------------------------------------
    # 补仓（ADD）服从大盘基调：高风险或下降趋势 → 不摊平已有持仓
    # `MarketEnvironment.block_add()` 早就定义了这个意图，报告文案也一直写着
    # 「当前属风险释放阶段，不宜新开仓位或补仓摊薄成本」——但它**从未接进决策**，
    # 于是出现"文字说不补仓、行为上给加仓"。这里把它接上。
    # 注意区分：**新开**（BUY）在弱市里仍可凭强信号小仓参与，被禁止的是"往亏损里加钱"。
    # ------------------------------------------------------------
    if action == Action.ADD and market.block_add():
        action = Action.HOLD
        target = None
        reasons.insert(0, f"大盘{market.trend}·{market.risk}：不补仓摊平成本"
                          "（弱市里加仓摊薄是亏损扩大的主要原因）；"
                          "已有持仓先管好风控，新机会可以小仓新开")

    # ------------------------------------------------------------
    # 高风险市场里，新开/补仓只接受「强信号」（信心 ≥3）
    # 依据：market 的基调一直写着「下降周期 / 高风险：禁止新开与补仓」，
    # 但评分带里 score ≥1.0（信心 2，文案自称"信号偏多但尚不充分"）也会给 BUY/ADD
    # ——同一份报告里基调与建议互相打脸（2026-10-01 用户直接指出"居然提示我加仓"）。
    # 本函数注释写的"以更小仓位、更严格信号介入"就是这个意思：高风险下把门槛提到信心 3。
    # ------------------------------------------------------------
    if (action in (Action.BUY, Action.ADD) and market.risk == "高风险"
            and conviction < 3):
        action = Action.HOLD
        target = None
        # 插到最前面：快照与 AI 事实都只保留 reasons[:3]，
        # 拦截原因如果不排在最前，用户就看不到"为什么从加仓变成观望"
        reasons.insert(0, "大盘高风险：该信号偏多但尚不充分（信心 <3），本轮不做新开/补仓，先观察")

    # ------------------------------------------------------------
    # 仓位预算买不起「一件」时，必须分清两种情况（2026-10-01 用户追问后修正）
    #
    # 背景：组合只有约 ¥1,800，而饰品单件常常 ¥60~¥700——按百分比算出来的标准仓位
    # （信心 2 → 3.6% ≈ ¥65）经常买不到一件。第一版守卫一律降级为观望，结果是
    # "建议永远不可执行"，用户直接反问「那以后我都只能买 36 块钱的东西了？」。
    #
    # 正确的区分：
    #   a) 一件就突破单标的上限 max_position% → 真的不能碰（例：弹雨 ¥515 ≈ 组合 27% > 25%）
    #   b) 一件在上限内，只是大于该信号的标准仓位 → 允许按「最小可执行单位 1 件」执行，
    #      但必须把权重说清楚（X% vs 标准 Y%），让用户知道这是超配还是放弃。
    # 与模拟盘执行层一致：paper.py 里只有 `target_amount >= price` 才会成交。
    # ------------------------------------------------------------
    if (action in (Action.BUY, Action.ADD) and target and item.cur_price
            and equity and float(equity) > 0):
        unit = float(item.cur_price)
        budget = float(equity) * float(target) / 100.0
        if budget < unit:
            unit_weight = unit / float(equity) * 100.0
            want = float(target)
            if unit_weight > max_position:
                action = Action.HOLD
                target = None
                reasons.insert(0, f"最小可执行单位（1 件 ¥{unit:,.2f}）就占组合 "
                                  f"{unit_weight:.1f}%，超过单标的上限 {max_position:g}%：本轮不介入"
                                  f"（该信号的标准仓位只有 {want:g}% ≈ ¥{budget:,.0f}）")
            else:
                target = round(unit_weight, 1)
                reasons.insert(0, f"标准仓位 {want:g}% ≈ ¥{budget:,.0f} 买不到一件；"
                                  f"按最小可执行单位 1 件 ¥{unit:,.2f} 执行 = 组合 {unit_weight:.1f}%"
                                  f"（在单标的上限 {max_position:g}% 之内）")

    return Decision(action=action, conviction=conviction, score=round(score, 2),
                    target_weight_pct=target, reasons=reasons,
                    action_text=_action_text(action, conviction, target, item, equity))


def _illiquid_note(item: ItemAnalysis) -> str:
    """流动性差时的**执行方式**提醒：挂单贴住最低在售价，别砸给求购单。

    来自一次真金白银的教训（2026-09-28 水纹之印）：用户自己挂 ¥25，而当天最低在售价
    ¥27.50，成交价低了 9.1%——该品当日成交只有 1 件。清淡品种只要挂「最低在售价 − 0.01」
    就已经排在最前面，不需要自己往下砍。
    """
    if str(getattr(item, "liquidity_grade", "")) != "低":
        return ""
    return ("　⚠ 该品流动性低：挂单价贴住「最低在售价 − 0.01」挂单等成交，"
            "别点「卖给求购」（点差可能吃掉 5~10%）。")


def _is_min_lot(target: float | None, item: ItemAnalysis, equity: float | None) -> bool:
    """目标仓位是否已经是「最小可执行单位」（约等于 1 件）→ 文案不能再说"分 2~3 笔"。"""
    try:
        if not (target and equity and item.cur_price):
            return False
        return float(equity) * float(target) / 100.0 <= float(item.cur_price) * 1.05
    except (TypeError, ValueError):
        return False


def _action_text(action: Action, conviction: int, target: float | None,
                 item: ItemAnalysis, equity: float | None = None) -> str:
    """生成操作文案。"""
    stars = "★" * conviction + "☆" * (5 - conviction)
    name = item.holding.market_hash_name
    min_lot = _is_min_lot(target, item, equity)
    if action == Action.BUY:
        if min_lot:
            return (f"信心度 {stars}：建议建仓 {name} 1 件（约 {target}% 组合权益）——"
                    "这已是该标的最小可执行单位、无法再拆小；跌破关键支撑即放弃。")
        return (f"信心度 {stars}：建议建仓 {name}，目标仓位约 {target}% 组合权益；"
                "分 2~3 笔挂单建仓，首笔不超过目标仓位的 1/2，跌破关键支撑即放弃。")
    if action == Action.ADD:
        if min_lot:
            return (f"信心度 {stars}：建议加仓 {name} 1 件（约 {target}% 组合权益）——"
                    "最小可执行单位，无法再拆小；加仓部分设好止损。")
        return (f"信心度 {stars}：建议对 {name} 加仓至约 {target}% 组合权益；"
                "加仓部分设好止损，勿一次性打满。")
    if action == Action.REDUCE:
        return (f"信心度 {stars}：建议分批减仓 {name}（先减 1/3~1/2），"
                "保留部分仓位观察，若继续走弱则清仓。" + _illiquid_note(item))
    if action == Action.CLEAR:
        return (f"信心度 {stars}：建议清仓 {name}，落袋/止损离场，"
                "等待形态修复或更优价格再考虑重新介入。" + _illiquid_note(item))
    return (f"信心度 {stars}：维持观望 {name}，不追涨不杀跌，"
            "等待趋势与量价信号进一步明确。")
