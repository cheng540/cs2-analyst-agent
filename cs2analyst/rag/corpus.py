"""语料构建：把手写知识、平台规则、官方公告、历史研判与之后走势组装成文档。

⚠️ 设计红线（交接文档 4.1）：**持仓数字绝不进这个库**。
价格、持仓、盈亏属于结构化事实，只能走工具取数；把它们灌进检索库会让模型
"从片段里捞一个数字"当真，这正是要杜绝的幻觉来源。
所以本模块只吃四类东西：非结构化知识、规则文本、公告、以及「案例卡片」
（日期 + 当时的形态/判断 + 之后实际走势 —— 检索到的是**案例**，不是当前行情）。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))
_MAX_NEWS_CHARS = 1200
_MAX_SECTION_CHARS = 2000


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _safe_name(name: str) -> str:
    """与 datasource/steamdt.py 的缓存命名保持一致（不要另写一套，会找不到文件）。"""
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return keep[:120] or "unnamed"


def _doc(kind: str, title: str, text: str, source: str, day: str = "",
         tags: list[str] | None = None, keywords: str = "") -> dict:
    """`keywords` 只进索引、不进正文摘要（否则摘要全是别名行，看不到内容）。"""
    return {
        "id": f"{kind}:{source}:{title}"[:180],
        "kind": kind,
        "title": title.strip()[:120],
        "text": re.sub(r"\n{3,}", "\n\n", text).strip()[:_MAX_SECTION_CHARS],
        "source": source,
        "date": day,
        "tags": tags or [],
        "keywords": keywords.strip(),
    }


# --------------------------------------------------------------------------
# 1) 手写知识库 knowledge/*.md（按 ## 小节切分）
# --------------------------------------------------------------------------
def knowledge_docs(root: Path) -> list[dict]:
    out: list[dict] = []
    kdir = root / "knowledge"
    if not kdir.exists():
        return out
    for path in sorted(kdir.glob("*.md")):
        text = _read_text(path)
        if not text.strip():
            continue
        file_title = path.stem
        if not text.lstrip().startswith("#"):     # 无标题的文件整体成一篇
            out.append(_doc("knowledge", file_title, text, f"knowledge/{path.name}"))
            continue
        blocks = re.split(r"^## +", text, flags=re.M)
        head = blocks[0]
        m = re.search(r"^# +(.+)$", head, flags=re.M)
        if m:
            file_title = m.group(1).strip()
        preamble = re.sub(r"^# +.*$", "", head, flags=re.M).strip()
        if preamble:
            out.append(_doc("knowledge", f"{file_title}（总览）", preamble,
                            f"knowledge/{path.name}"))
        for block in blocks[1:]:
            title, _, body = block.partition("\n")
            out.append(_doc("knowledge", f"{file_title} · {title.strip()}",
                            body or title, f"knowledge/{path.name}"))
    return out


# --------------------------------------------------------------------------
# 2) 平台规则（rules.py 是唯一真相源，这里只是它的一个检索镜像）
# --------------------------------------------------------------------------
def rules_docs(cfg_sim: dict | None = None) -> list[dict]:
    from ..rules import cashout_cost_pct, fee_rates, trading_rules_brief, trading_rules_text

    sell, withdraw = fee_rates(cfg_sim)
    text = (
        f"{trading_rules_text(cfg_sim)}\n\n"
        f"一句话版：{trading_rules_brief(cfg_sim)}\n\n"
        f"换算：卖出费率 {sell * 100:g}%，提现费率 {withdraw * 100:g}%，"
        f"卖出并提现的合计成本约 {cashout_cost_pct(cfg_sim):g}%。\n"
        "常见误解：把费率记成 2.5%（早期未核实的假设，已更正）；"
        "买入**不收**手续费；不提现、继续在钱包里买饰品，只发生 1% 卖出费。"
    )
    return [_doc("rules", "悠悠有品交易规则：T+7 与费率口径", text, "cs2analyst/rules.py",
                 tags=["规则", "费率", "T+7", "提现"])]


# --------------------------------------------------------------------------
# 3) 官方公告 data/intel/*.json
# --------------------------------------------------------------------------
def intel_docs(root: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((root / "data" / "intel").glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        day = str(data.get("date") or path.stem)
        for news in (data.get("news") or []):
            title = str(news.get("title") or "").strip()
            body = str(news.get("contents") or "").strip()
            if not title:
                continue
            text = (
                f"发布日期：{news.get('date_str') or day}；来源栏目：{news.get('feed') or 'CS2 更新记录'}\n"
                f"官方市场影响评级：{news.get('market_impact') or '未标注'}；"
                f"玩法影响：{news.get('gameplay_impact') or '未标注'}；"
                f"类型：{news.get('update_type') or '未标注'}\n\n{body[:_MAX_NEWS_CHARS]}"
            )
            out.append(_doc("intel", title, text, f"data/intel/{path.name}", day,
                            tags=["公告", "更新"]))
    return out


# --------------------------------------------------------------------------
# 4) 经验库核心：历史研判 + 之后实际走势（案例卡片）
# --------------------------------------------------------------------------
def _kline_closes(root: Path, market_hash_name: str) -> list[tuple[date, float]]:
    """读单品 K 线缓存 → [(日期, 收盘)] 升序。没有缓存就返回空。"""
    path = root / "data" / "items" / _safe_name(market_hash_name) / "kline_2.json"
    rows = _read_json(path)
    if not isinstance(rows, list):
        return []
    out: list[tuple[date, float]] = []
    for row in rows:
        try:
            ts, _open, close = float(row[0]), float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        if close <= 0:
            continue
        out.append((datetime.fromtimestamp(ts, CST).date(), close))
    out.sort(key=lambda x: x[0])
    return out


def _close_on_or_after(series: list[tuple[date, float]], target: date) -> tuple[date, float] | None:
    for day, close in series:
        if day >= target:
            return day, close
    return None


def _verdict(advice: str, move_pct: float | None) -> str:
    """按当时建议方向与之后走势给一个可复核的短结论。"""
    if move_pct is None:
        return "无后续价格，无法复核"
    bearish = any(k in advice for k in ("减仓", "清仓", "卖出", "止损"))
    bullish = any(k in advice for k in ("买入", "加仓", "试仓", "建仓"))
    if bearish:
        if move_pct <= -2:
            return "判断偏对（后续确实走弱）"
        if move_pct >= 2:
            return "判断偏错（减仓后反弹）"
        return "中性（后续基本走平）"
    if bullish:
        if move_pct >= 2:
            return "判断偏对（后续确实走强）"
        if move_pct <= -2:
            return "判断偏错（买入后继续跌）"
        return "中性（后续基本走平）"
    if move_pct <= -5:
        return "持有偏被动（后续跌幅较大，当时减仓更优）"
    if move_pct >= 5:
        return "持有正确（后续上涨，没卖是对的）"
    return "持有可接受（后续波动不大）"


def case_docs(root: Path, lookahead_days: tuple[int, ...] = (3, 7)) -> list[dict]:
    """把「某天的系统建议」与「之后的真实走势」拼成案例卡片。"""
    out: list[dict] = []
    for path in sorted((root / "data" / "analysis").glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        day = str(data.get("date") or path.stem)
        try:
            base_day = date.fromisoformat(day)
        except ValueError:
            continue
        market = data.get("market") or {}
        for item in (data.get("items") or []):
            name = str(item.get("饰品") or "").strip()
            if not name:
                continue
            price = item.get("现价")
            series = _kline_closes(root, name)
            moves: list[str] = []
            move7: float | None = None
            for horizon in lookahead_days:
                hit = _close_on_or_after(series, base_day + timedelta(days=horizon))
                if not hit or not price:
                    continue
                day_hit, close = hit
                try:
                    pct = (close / float(price) - 1) * 100
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                moves.append(f"{horizon} 日后（{day_hit}）{pct:+.1f}%")
                if horizon == max(lookahead_days):
                    move7 = pct
            advice = str(item.get("系统建议") or "未给出")
            reasons = "；".join(str(r) for r in (item.get("理由") or [])[:4])
            text = (
                f"日期：{day}\n"
                f"饰品：{item.get('中文名') or name}（{name}）\n"
                f"当时形态：{item.get('趋势') or '—'}；位置 {item.get('位置') or '—'}；"
                f"量价 {item.get('量价') or '—'}；流动性 {item.get('流动性') or '—'}\n"
                f"当时价格：¥{price}；支撑 {item.get('最近支撑')}；压力 {item.get('最近压力')}\n"
                f"大盘背景：{market.get('趋势') or '—'} / {market.get('风险等级') or '—'}"
                f"（指数 {market.get('大盘指数') or '—'}）\n"
                f"当时系统建议：{advice}（信心度 {item.get('信心度') or '—'}）\n"
                f"当时理由：{reasons or '—'}\n"
                f"之后实际走势：{'；'.join(moves) if moves else '无后续价格数据'}\n"
                f"复核结论：{_verdict(advice, move7)}"
            )
            out.append(_doc("case", f"{day} {item.get('中文名') or name} 的建议与后续走势",
                            text, f"data/analysis/{path.name}", day,
                            tags=["案例", "复盘", "经验"],
                            keywords=("历史案例 上次 以前 类似形态 当时判断 后续走势 "
                                      "复盘 经验 怎么处理 减仓 清仓 持有")))
    return out


def judgement_docs(root: Path) -> list[dict]:
    """AI 每日研判原文（大盘观点 + 持仓建议）—— 回答"当时怎么想的"。"""
    out: list[dict] = []
    for path in sorted((root / "data" / "llm").glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        day = str(data.get("date") or path.stem)
        body = []
        if data.get("market_view"):
            body.append(f"大盘观点：{data['market_view']}")
        if data.get("portfolio_advice"):
            body.append(f"持仓建议：{data['portfolio_advice']}")
        warns = data.get("risk_warnings") or []
        if warns:
            body.append("风险提示：" + "；".join(str(w) for w in warns[:5]))
        if not body:
            continue
        out.append(_doc("judgement", f"{day} 当日 AI 研判", "\n\n".join(body),
                        f"data/llm/{path.name}", day, tags=["研判", "复盘"]))
    return out


def note_docs(root: Path) -> list[dict]:
    """用户手写笔记（data/journal），按 ## 小节切分。"""
    out: list[dict] = []
    path = root / "data" / "journal" / "user_notes.md"
    text = _read_text(path)
    if not text.strip():
        return out
    for block in re.split(r"^## +", text, flags=re.M)[1:]:
        title, _, body = block.partition("\n")
        out.append(_doc("note", title.strip(), body, "data/journal/user_notes.md",
                        tags=["笔记"]))
    if not out:
        out.append(_doc("note", "用户笔记", text, "data/journal/user_notes.md"))
    return out


def build_documents(root: Path, cfg_sim: dict | None = None) -> list[dict]:
    """组装全部语料。缺数据的来源自动跳过，不会因为某个目录不存在就整体失败。"""
    root = Path(root)
    docs: list[dict] = []
    docs += knowledge_docs(root)
    docs += rules_docs(cfg_sim)
    docs += intel_docs(root)
    docs += case_docs(root)
    docs += judgement_docs(root)
    docs += note_docs(root)

    seen: set[str] = set()
    unique: list[dict] = []
    for doc in docs:
        if not doc["text"].strip() or doc["id"] in seen:
            continue
        seen.add(doc["id"])
        unique.append(doc)
    return unique


def stats(docs: list[dict]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for doc in docs:
        by_kind[doc["kind"]] = by_kind.get(doc["kind"], 0) + 1
    return {
        "total": len(docs),
        "by_kind": by_kind,
        "chars": sum(len(d["text"]) for d in docs),
    }
