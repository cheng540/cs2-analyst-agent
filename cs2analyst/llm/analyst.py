"""LLM 智能研判层：把「事实 + 情报」交给大模型做解读，输出结构化判断。

设计原则（混合架构）：
- 规则引擎负责「事实与风控」：真实价格、指标、评分、仓位数学、止损纪律；
- 大模型负责「解读与判断」：官方更新/市场叙事/异动含义、交叉验证、给出立场与风险点；
- 大模型**不能修改事实**，只能对规则动作做「否决 / 提议」两种有限干预（用于 B 轨模拟盘），
  全部干预都会记录留痕，便于月底复盘。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..engine.decision import Action, Decision
from ..logging_setup import get_logger
from .client import LLMClient

log = get_logger(__name__)

_STANCES = {"buy", "add", "hold", "reduce", "avoid"}

SYSTEM_PROMPT = """你是资深 CS2 饰品交易分析师，为一个个人持仓组合提供每日研判。

重要交易规则（必须遵守）：
- 平台为悠悠有品，执行 T+7 规则：买入后 7 天内不可卖出。因此禁止建议「今天买明天卖」的短线操作。
- 费用结构：**买入不收手续费；卖出收 1%**，卖出所得进入平台钱包；**钱包提现另外收 1%**。
  因此「继续在钱包里买饰品」的实际成本是 1%，「卖出并提现成现金」的合计成本约 2%。
  给卖出建议时要区分这两种口径，不要笼统说成「手续费很高」。

你的任务：
1. 解读大盘环境与今日情报（V社更新公告、市场异动），说明它们对哪些品类可能产生影响；
2. 对每个标的给出立场与理由，指出关键风险；
3. 给出组合层面的可执行建议（含仓位与现金比例），并明确说明你与「规则引擎建议」的分歧点。

纪律要求：
- 只能基于用户提供的数字推理，不得编造价格、成交量或公告内容；
- 不要因为单日涨跌就改变中期判断；
- 如果数据不足或信号矛盾，要明确说「不确定」，不要强行给结论；
- 严禁输出投资建议免责声明式的空话，要具体、可执行。

输出必须是严格的 JSON（不要任何额外文字）：
{
  "market_view": "150字以内的大盘与情报解读",
  "portfolio_advice": "150字以内的组合层面建议（仓位/现金/优先级）",
  "risk_warnings": ["风险点1", "风险点2"],
  "items": [
    {"name": "<英文 marketHashName，必须与输入一致>",
     "comment": "80字以内的点评（结合趋势/位置/量价/流动性/情报）",
     "stance": "buy|add|hold|reduce|avoid",
     "confidence": 1-5 的整数,
     "key_risk": "30字以内的关键风险"}
  ]
}"""


@dataclass
class LLMJudgement:
    """大模型研判结果。"""
    date: str = ""
    model: str = ""
    market_view: str = ""
    portfolio_advice: str = ""
    risk_warnings: list[str] = field(default_factory=list)
    items: dict[str, dict] = field(default_factory=dict)  # 英文名 -> 条目
    usage: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.items or self.market_view)

    def item_comment(self, name: str) -> str:
        return str((self.items.get(name) or {}).get("comment") or "")

    def item_risk(self, name: str) -> str:
        return str((self.items.get(name) or {}).get("key_risk") or "")

    def item_stance(self, name: str) -> tuple[str, int]:
        it = self.items.get(name) or {}
        stance = str(it.get("stance") or "").lower()
        if stance not in _STANCES:
            stance = ""
        try:
            conf = int(it.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        return stance, max(0, min(5, conf))

    def to_dict(self) -> dict:
        return {
            "date": self.date, "model": self.model, "market_view": self.market_view,
            "portfolio_advice": self.portfolio_advice, "risk_warnings": self.risk_warnings,
            "items": self.items, "usage": self.usage, "error": self.error,
        }


def _fmt_pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def build_facts(market: Any, holdings: list[dict], watchlist: list[dict],
                intel: dict | None, sim_note: str = "") -> str:
    """把当日事实与情报整理成紧凑的文本（喂给大模型）。"""
    lines: list[str] = [f"日期：{datetime.now():%Y-%m-%d}", "", "【大盘环境】"]
    lines.append(f"趋势：{market.trend}；风险等级：{market.risk}；量能：{market.volume_state}"
                 f"（{market.volume_source or '振幅代理'}）")
    if market.index_value is not None:
        lines.append(f"大盘指数：{market.index_value:.2f}"
                     + (f"（较昨日 {_fmt_pct(market.diff_ratio)}）" if market.diff_ratio is not None else ""))
    if market.sentiment_score is not None:
        lines.append(f"平台情绪分：{market.sentiment_score:.0f}（仅供参考，不是决策依据）")
    if market.trend == "下降趋势" or market.risk == "高风险":
        lines.append("规则引擎当前基调：" + market.base_tone)

    if intel:
        news = intel.get("news") or []
        if news:
            lines.append("")
            lines.append("【今日情报：V社/官方更新与社区动态】")
            for i, n in enumerate(news[:10], 1):
                title = str(n.get("title") or "").strip()
                date = str(n.get("date_str") or "")
                contents = str(n.get("contents") or "").strip().replace("\n", " ")[:300]
                lines.append(f"{i}. [{date}] {title} —— {contents}")
        anomalies = intel.get("anomalies") or []
        if anomalies:
            lines.append("")
            lines.append("【市场异动（官网涨跌/热榜）】")
            for a in anomalies[:12]:
                lines.append(f"- {a.get('name')}：{a.get('summary')}")

    lines.append("")
    lines.append("【我的持仓（规则引擎已给出评分与动作）】")
    for h in holdings:
        lines.append(
            f"- {h['name']}｜{h.get('name_cn', '')}｜数量 {h['qty']}｜成本 ¥{h['cost']:.2f}"
            f"｜现价 ¥{h['price']:.2f}｜盈亏 {_fmt_pct(h['pnl_pct'])}"
            f"｜趋势 {h['trend']}｜6个月位置 {h.get('position', '-')}"
            f"｜量价 {h.get('volume', '-')}｜流动性 {h.get('liquidity', '-')}"
            f"｜规则建议 {h['rule_action']}（评分 {h['rule_score']:+.1f}）")
        if h.get("lots"):
            try:
                detail = " / ".join(
                    f"¥{float(v):,.2f}（{(float(h['price']) / float(v) - 1) * 100:+.1f}%）"
                    for v in sorted(h["lots"], reverse=True))
                lines.append(f"    ⚠ 同一饰品分多次买入、成本不同，逐件：{detail}")
            except (TypeError, ValueError, ZeroDivisionError):
                lines.append(f"    ⚠ 分件成本：{' / '.join(str(v) for v in h['lots'])}")
        if h.get("status"):
            lines.append(f"    ★ 用户意图：{h['status']}——**这是用户已做的决定，不要再建议持有**，"
                         "只需在执行层面给建议（上架价、手续费、先后顺序）")
        if h.get("rule_reasons"):
            lines.append(f"    规则理由：{'；'.join(h['rule_reasons'][:3])}")

    if watchlist:
        lines.append("")
        lines.append("【模拟盘观察池（无持仓，等待买点）】")
        for w in watchlist:
            lines.append(
                f"- {w['name']}｜{w.get('name_cn', '')}｜现价 ¥{w['price']:.2f}"
                f"｜趋势 {w['trend']}｜位置 {w.get('position', '-')}"
                f"｜量价 {w.get('volume', '-')}｜流动性 {w.get('liquidity', '-')}"
                f"｜规则建议 {w['rule_action']}（评分 {w['rule_score']:+.1f}）")

    if sim_note:
        lines.append("")
        lines.append("【模拟盘现状】")
        lines.append(sim_note)
    return "\n".join(lines)


def request_judgement(llm: LLMClient, facts: str, cfg: dict | None = None) -> LLMJudgement:
    """调用大模型获取研判（失败返回带 error 的对象，不抛异常）。"""
    j = LLMJudgement(date=datetime.now().strftime("%Y-%m-%d"), model=getattr(llm, "model", ""))
    if not llm.enabled:
        j.error = "LLM 未启用或未配置 api_key"
        return j
    # 动态注入平台规则（与 config 保持一致，避免提示词写死后与实际参数脱节）
    system_prompt = SYSTEM_PROMPT
    try:
        from ..rules import trading_rules_text
        sim_cfg = (cfg or {}).get("sim") if isinstance(cfg, dict) else None
        system_prompt = SYSTEM_PROMPT + "\n\n当前平台规则（以配置为准）：\n- " + trading_rules_text(sim_cfg)
    except Exception:
        pass
    user_prompt = (facts + "\n\n请基于以上数据输出 JSON（items 必须覆盖上面出现的每一个英文名）。")
    try:
        data = llm.chat_json([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
    except Exception as exc:
        j.error = f"LLM 调用失败: {exc}"
        log.warning("%s", j.error)
        return j

    j.market_view = str(data.get("market_view") or "").strip()
    j.portfolio_advice = str(data.get("portfolio_advice") or "").strip()
    warns = data.get("risk_warnings") or []
    j.risk_warnings = [str(w).strip() for w in warns if str(w).strip()][:8]
    items = data.get("items") or []
    if isinstance(items, dict):  # 容错：模型返回对象形式
        items = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in items.items()]
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if name:
            j.items[name] = it
    j.usage = getattr(llm, "last_usage", {}) or {}
    log.info("LLM 研判完成：解读 %d 字，覆盖 %d 个标的，tokens=%s",
             len(j.market_view), len(j.items), j.usage.get("total_tokens", "?"))
    return j


def apply_overlay(decisions: dict[str, Decision], judgement: LLMJudgement,
                  cfg: dict) -> tuple[dict[str, Decision], list[str]]:
    """把 AI 研判叠加到规则决策上（B 轨专用）。

    只允许两种有限干预：
    1. 否决：规则要买/加仓，但 AI 立场为 avoid/reduce 或信心 ≤2 → 降级为持有；
    2. 提议：规则为持有，但 AI 立场为 buy 且信心 ≥4 → 小仓试建（试探性）。
    返回 (新决策, 变更说明)。
    """
    from ..engine.decision import _sizing  # 复用仓位计算

    out: dict[str, Decision] = {}
    notes: list[str] = []
    for name, d in decisions.items():
        stance, conf = judgement.item_stance(name)
        comment = judgement.item_comment(name)
        new_d = d
        # 无立场（AI 未点评该标的）则不干预，保持规则结论
        if (d.action in (Action.BUY, Action.ADD) and stance
                and (stance in ("avoid", "reduce") or conf <= 2)):
            new_d = Decision(action=Action.HOLD, conviction=2, score=d.score,
                             target_weight_pct=None,
                             reasons=list(d.reasons) + [f"AI 否决买入：{comment or '立场偏空'}"],
                             action_text="AI 研判不支持当前买点，暂缓建仓，等待信号确认。")
            notes.append(f"{name}：规则 {d.label} → AI 否决改为持有")
        elif d.action == Action.HOLD and stance == "buy" and conf >= 4:
            new_d = Decision(action=Action.BUY, conviction=2, score=d.score,
                             target_weight_pct=6.0,
                             reasons=list(d.reasons) + [f"AI 提议试仓（信心 {conf}）：{comment}"],
                             action_text="AI 研判认为存在机会，规则未触发买点，采用小仓试探（≤6% 权益）。")
            notes.append(f"{name}：规则持有 → AI 提议小仓试建")
        if new_d is not d:
            out[name] = new_d
        else:
            out[name] = d
    return out, notes
