"""平台交易规则文本（单一来源）。

背景：AI 曾在一处提示词里漏掉 T+7 与手续费，导致点评可能给出不考虑平台约束的建议。
现在所有需要「知道交易规则」的地方（每日研判 / 网页问答 / 告警点评）都从这里取，
避免口径不一致或再次遗漏。

⚠️ 费率口径（2026-09-24 由用户更正，务必不要再写错）：
    出售收 1%（买入不收）→ 钱进入平台钱包；
    钱包提现再收 1%。所以：
      · 卖出后继续留在钱包里买饰品 → 实际成本只有 1%（这是本项目的默认口径）
      · 要提现成现金 → 合计约 2%（1% 卖出 + 1% 提现，复利约 1.99%）
    此前写成「2.5%」是我自己假设的数字、从未核实，后来一路传到了报告、提示词与求职材料里。
    教训与「名字写错」「口径混比」同类：**没核实过的数字不能当作事实写进系统**。
"""
from __future__ import annotations


def fee_rates(cfg_sim: dict | None = None) -> tuple[float, float]:
    """返回 (卖出费率, 提现费率)，单位为小数。"""
    sim = cfg_sim or {}
    try:
        sell = float(sim.get("fee_rate", 0.01))
    except (TypeError, ValueError):
        sell = 0.01
    try:
        withdraw = float(sim.get("withdraw_fee_rate", 0.01))
    except (TypeError, ValueError):
        withdraw = 0.01
    return sell, withdraw


def cashout_cost_pct(cfg_sim: dict | None = None) -> float:
    """卖出并提现成现金的合计成本（%，复利计算约 1.99%）。"""
    sell, withdraw = fee_rates(cfg_sim)
    return round((1 - (1 - sell) * (1 - withdraw)) * 100, 2)


def trading_rules_text(cfg_sim: dict | None = None) -> str:
    """生成平台交易规则说明文本（给模型看的完整版）。"""
    sim = cfg_sim or {}
    hold = int(sim.get("hold_days_min", 7) or 7)
    sell, withdraw = fee_rates(sim)
    sell_pct, wd_pct = sell * 100, withdraw * 100
    return (
        f"平台为悠悠有品，执行 T+{hold} 规则：买入后 {hold} 天内不可卖出"
        f"（新买入的仓位在锁定期内无法止损，短线套利不现实）。"
        f"费用结构：**买入不收手续费；卖出收 {sell_pct:g}%**，卖出所得进入平台钱包；"
        f"钱包提现另外收 {wd_pct:g}%。因此要看这笔钱接下来怎么用："
        f"① 继续留在钱包里买饰品（常见情况）→ 实际成本只有 {sell_pct:g}%；"
        f"② 要提现成现金 → 合计约 {cashout_cost_pct(sim):g}%（{sell_pct:g}% 卖出 + {wd_pct:g}% 提现）。"
        f"给卖出建议时要按这两种口径分别说清楚，不要笼统说成「手续费很高」。"
    )


def trading_rules_brief(cfg_sim: dict | None = None) -> str:
    """一行版规则（用于推送消息页脚）。"""
    sim = cfg_sim or {}
    hold = int(sim.get("hold_days_min", 7) or 7)
    sell, withdraw = fee_rates(sim)
    return (f"平台规则：T+{hold}（买入{hold}天内不可卖）· 卖出费 {sell * 100:g}%"
            f"（提现另收 {withdraw * 100:g}%）")
