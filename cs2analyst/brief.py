"""上下文恢复简报：一条命令读回项目全貌（给未来的 AI / 用户自己看）。

读取（全部只读，缺文件自动跳过）：
- config/inventory.json        持仓概况
- data/sim/state_a|b.json      模拟盘双轨净值与持仓（含 T+7 锁定期）
- data/llm/*.json              最新一次 AI 研判
- data/intel/*.json            最新一次情报
- data/journal/user_notes.md   用户交互记录
- PROJECT_MEMO.md              项目备忘（含待办）
- reports/                     最新报告文件名
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .logging_setup import get_logger

log = get_logger(__name__)


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as exc:
        log.debug("读取失败 %s: %s", path, exc)
    return {}


def _latest(pattern_dir: Path, pattern: str) -> Path | None:
    if not pattern_dir.exists():
        return None
    files = sorted(pattern_dir.glob(pattern))
    return files[-1] if files else None


def _load_track(path: Path) -> dict:
    data = _read_json(path)
    if not data:
        return {}
    return data


def build_brief(cfg: AppConfig, today: str | None = None) -> str:
    """生成上下文恢复简报（Markdown 文本）。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    lines: list[str] = [f"# 上下文恢复简报（{today}）", ""]

    # ---- 项目备忘待办 ----
    memo = cfg.config_path.parent.parent / "PROJECT_MEMO.md"
    if memo.exists():
        try:
            text = memo.read_text(encoding="utf-8")
            tail = text.split("## 四、待办")[-1] if "## 四、待办" in text else ""
            todos = [ln.strip() for ln in tail.splitlines()
                     if ln.strip().startswith("- [")][:10]
            if todos:
                lines.append("## 待办事项（来自 PROJECT_MEMO.md）")
                lines.extend(todos)
                lines.append("")
        except OSError:
            pass

    # ---- 持仓概况 ----
    inv = _read_json(cfg.inventory_path())
    if isinstance(inv, list):
        pieces = sum(int(i.get("quantity") or 0) for i in inv)
        cost = sum(float(i.get("buy_price") or 0) * int(i.get("quantity") or 0) for i in inv)
        manual = [i.get("market_hash_name") for i in inv if i.get("manual_price")]
        lines.append("## 持仓概况")
        lines.append(f"- 条目 {len(inv)} 个 / 共 {pieces} 件，总成本 ¥{cost:,.2f}")
        if manual:
            lines.append(f"- 手动估值条目（无API行情）：{'、'.join(manual)}")
        lines.append("- 明细见 `config/inventory.json`；实时市值见最新报告")
        lines.append("")

    # ---- 模拟盘双轨 ----
    lines.append("## 模拟盘（双轨，T+7 规则）")
    for label, path in (("A", cfg.sim_state_path()), ("B", cfg.sim_state_path_b())):
        st = _load_track(path)
        if not st:
            lines.append(f"- {label} 轨：尚无状态文件")
            continue
        init = float(st.get("initial_capital") or 0)
        cash = float(st.get("cash") or 0)
        journal = st.get("journal") or []
        net = float(journal[-1].get("net_value")) if journal else cash
        ret = ((net / init - 1) * 100) if init else 0.0
        pos_names = list((st.get("positions") or {}).keys())
        lines.append(f"- **{label} 轨**（{'纯规则' if label == 'A' else '规则+AI'}）：初始 ¥{init:,.2f}"
                     f" → 净值 ¥{net:,.2f}（{ret:+.2f}%），现金 ¥{cash:,.2f}，"
                     f"持仓 {len(pos_names)} 项，运行 {len(journal)} 天")
        for name, pos in (st.get("positions") or {}).items():
            lines.append(f"    - {name} ×{pos.get('qty')}（成本 ¥{pos.get('avg_cost')}，"
                         f"锁定期至 {pos.get('locked_until') or '—'}）")
        if journal:
            last = journal[-1]
            lines.append(f"    最近手记（{last.get('date')}）：{str(last.get('thoughts'))[:120]}")
    lines.append("")

    # ---- 最新 AI 研判 ----
    llm_file = _latest(cfg.data_dir() / "llm", "*.json")
    if llm_file:
        j = _read_json(llm_file)
        lines.append(f"## 最新 AI 研判（{j.get('date', llm_file.stem)}）")
        if j.get("market_view"):
            lines.append(f"- 大盘解读：{str(j['market_view'])[:200]}")
        if j.get("portfolio_advice"):
            lines.append(f"- 组合建议：{str(j['portfolio_advice'])[:200]}")
        for w in (j.get("risk_warnings") or [])[:3]:
            lines.append(f"- 风险：{w}")
        lines.append("")

    # ---- 最新情报 ----
    intel_file = _latest(cfg.data_dir() / "intel", "*.json")
    if intel_file:
        it = _read_json(intel_file)
        news = it.get("news") or []
        anomalies = it.get("anomalies") or []
        lines.append(f"## 最新情报（{it.get('date', intel_file.stem)}）")
        lines.append(f"- 官方更新/公告 {len(news)} 条，市场异动 {len(anomalies)} 条")
        for n in news[:3]:
            impact = f"（市场影响：{n.get('market_impact')}）" if n.get("market_impact") else ""
            lines.append(f"    - [{n.get('date_str', '')}] {str(n.get('title'))[:60]}{impact}")
        lines.append("")

    # ---- 用户交互记录 ----
    notes = cfg.data_dir() / "journal" / "user_notes.md"
    if notes.exists():
        try:
            text = notes.read_text(encoding="utf-8")
            blocks = [b for b in text.split("\n## ")
                      if b.strip() and not b.lstrip().startswith("#")]
            recent = blocks[-3:]
            lines.append("## 最近用户交互记录（完整见 data/journal/user_notes.md）")
            for b in recent:
                head = b.strip().splitlines()[0] if b.strip() else ""
                body = [ln.strip() for ln in b.strip().splitlines()[1:] if ln.strip()][:4]
                lines.append(f"- **{head}**")
                for ln in body:
                    lines.append(f"    {ln}")
            lines.append("")
        except OSError:
            pass

    # ---- 最新报告 ----
    reports = sorted(cfg.report_dir().glob("*.docx"))
    if reports:
        lines.append("## 最新报告")
        lines.append(f"- {reports[-1]}")
        lines.append("")

    lines.append("---")
    lines.append("恢复上下文建议：读 `PROJECT_MEMO.md` → 本简报 → 最新报告；"
                 "用户若只说「今天不操作」，在 `data/journal/user_notes.md` 追加一条即可。")
    return "\n".join(lines)


def save_brief(cfg: AppConfig, text: str, today: str) -> Path:
    out = cfg.data_dir() / f"brief_{today}.md"
    out.write_text(text, encoding="utf-8")
    return out
