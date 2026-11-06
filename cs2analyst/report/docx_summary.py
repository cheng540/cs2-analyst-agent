"""盘中概览的 Word 版（12:00 / 22:00 各一份）。

为什么要它：
    用户反馈「概览太短了，就一段文字」。文字推送受企业微信篇幅限制，只能写触发了异动的那几条；
    而真正有用的信息是**全部监控标的的体检表**——每件现价、较上次/昨日涨跌、我的成本与浮亏、
    距支撑还有多远。这些数据巡检本来就算过一遍，只是以前没落到纸面上。

设计原则：
1. **不依赖 alerts 包**（只接收普通 dict/list），避免与 checker 形成循环引用；
2. 复用 `ReportGenerator` 的字体与排版工具，保证和 17:30 日报看起来是一套东西；
3. 生成失败绝不抛异常——概览的文字推送不能被附件问题拖垮（调用方也会再包一层 try）。

用法（由 AlertChecker.run 在 summary_times 时调用）：
    build_summary_docx(path, run_time="2026-09-30 12:00", checked=14, expected=14,
                       market_line="...", portfolio=..., holdings=[...], watchlist=[...],
                       urgent_lines=[...], mild_lines=[...], ai_text="...", footer="...")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from .docx_report import ReportGenerator

RED = RGBColor(0xC0, 0x00, 0x00)
GREEN = RGBColor(0x1E, 0x7B, 0x34)
GRAY = RGBColor(0x59, 0x59, 0x59)

COLS_HOLD = ["饰品", "现价", "较上次", "较昨日", "件数", "成本", "浮亏", "距支撑", "系统建议"]
COLS_WATCH = ["饰品", "现价", "较上次", "较昨日", "距支撑", "系统建议"]


def _pct(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{x:+.2f}%" if x else "0.00%"


def _color_of(v: Any):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x <= -1:
        return GREEN      # A 股/CS2 习惯：跌用绿（报告里一直是这个口径）
    if x >= 1:
        return RED
    return None


def _price(v: Any) -> str:
    try:
        return f"¥{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _dist_to_support(price: Any, support: Any) -> str:
    """距最近支撑还有多少（负=已经跌破）。"""
    try:
        p, s = float(price), float(support or 0)
    except (TypeError, ValueError):
        return "—"
    if not s or not p:
        return "—"
    d = (p / s - 1) * 100
    return f"{d:+.1f}%" + ("（已破位）" if d < -1 else "")


def _table(doc: Document, gen: ReportGenerator, cols: list[str], rows: list[dict],
           row_builder) -> None:
    t = doc.add_table(rows=1, cols=len(cols))
    t.style = "Light Grid Accent 1"
    for i, c in enumerate(cols):
        gen._cell_text(t.rows[0].cells[i], c, 9, bold=True)
    for r in rows:
        cells = t.add_row().cells
        row_builder(r, cells)


def build_summary_docx(out_path: Path, *, run_time: str, checked: int = 0, expected: int = 0,
                       market_line: str = "", portfolio: dict | None = None,
                       holdings: list[dict] | None = None, watchlist: list[dict] | None = None,
                       urgent_lines: list[str] | None = None, mild_lines: list[str] | None = None,
                       ai_text: str = "", footer: str = "",
                       name_map: dict[str, str] | None = None) -> Path:
    """生成「盘中概览」Word。返回写入路径。"""
    gen = ReportGenerator(name_map=name_map or {})
    doc = Document()
    for s in doc.sections:
        s.top_margin, s.bottom_margin = Pt(42), Pt(42)
        s.left_margin, s.right_margin = Pt(48), Pt(48)

    # ---- 标题 ----
    h = doc.add_heading("", level=1)
    run = h.add_run("📊 盘中概览")
    gen._set_font(run, gen.heading_font, 18, bold=True)
    gen._para(doc, f"{run_time}　｜　本轮巡检 {checked}/{expected} 个标的取价成功"
                   + ("" if checked >= expected else "（有标的取价失败，见文末）"),
              size=9.5, color=GRAY, space_after=8)

    # ---- 大盘 ----
    if market_line:
        gen._heading(doc, "大盘环境", level=2)
        gen._para(doc, market_line, size=10.5)

    # ---- 我的持仓体检表 ----
    holdings = holdings or []
    gen._heading(doc, f"我的持仓体检（{len(holdings)} 条）", level=2)
    if holdings:
        def hold_row(r: dict, cells) -> None:
            gen._cell_text(cells[0], r.get("cn") or r.get("display") or r.get("name", ""), 8.5)
            gen._cell_text(cells[1], _price(r.get("price")), 8.5)
            gen._cell_text(cells[2], _pct(r.get("chg_last")), 8.5, color=_color_of(r.get("chg_last")))
            gen._cell_text(cells[3], _pct(r.get("chg_yest")), 8.5, color=_color_of(r.get("chg_yest")))
            gen._cell_text(cells[4], str(r.get("qty") or ""), 8.5)
            gen._cell_text(cells[5], _price(r.get("cost")), 8.5)
            gen._cell_text(cells[6], _pct(r.get("pnl_pct")), 8.5, color=_color_of(r.get("pnl_pct")))
            gen._cell_text(cells[7], _dist_to_support(r.get("price"), r.get("support")), 8.5)
            gen._cell_text(cells[8], str(r.get("action") or "—"), 8.5)
        _table(doc, gen, COLS_HOLD, holdings, hold_row)
        if portfolio:
            gen._para(doc, f"组合合计：成本 {_price(portfolio.get('总成本'))}　"
                           f"市值 {_price(portfolio.get('总市值'))}　"
                           f"浮动盈亏 {_pct(portfolio.get('盈亏%'))}",
                      size=10, bold=True, space_after=2)
    else:
        gen._para(doc, "（本轮没有取到持仓数据）", size=10, color=GRAY)

    # ---- 关注标的 ----
    watchlist = watchlist or []
    if watchlist:
        gen._heading(doc, f"关注标的（{len(watchlist)} 个，非持仓）", level=2)
        gen._para(doc, "这些不是你的货；买入即锁 7 天不能卖，所以只做观察。", size=9, color=GRAY,
                  space_after=4)

        def watch_row(r: dict, cells) -> None:
            gen._cell_text(cells[0], r.get("cn") or r.get("display") or r.get("name", ""), 8.5)
            gen._cell_text(cells[1], _price(r.get("price")), 8.5)
            gen._cell_text(cells[2], _pct(r.get("chg_last")), 8.5, color=_color_of(r.get("chg_last")))
            gen._cell_text(cells[3], _pct(r.get("chg_yest")), 8.5, color=_color_of(r.get("chg_yest")))
            gen._cell_text(cells[4], _dist_to_support(r.get("price"), r.get("support")), 8.5)
            gen._cell_text(cells[5], str(r.get("action") or "—"), 8.5)
        _table(doc, gen, COLS_WATCH, watchlist, watch_row)

    # ---- 本轮信号 ----
    urgent_lines = urgent_lines or []
    mild_lines = mild_lines or []
    gen._heading(doc, "本轮触发的信号", level=2)
    if urgent_lines:
        gen._para(doc, f"紧急（已即时推送，{len(urgent_lines)} 条）", size=10, bold=True, space_after=2)
        for line in urgent_lines:
            gen._para(doc, "· " + line, size=9.5, space_after=1)
    if mild_lines:
        gen._para(doc, f"温和波动（静默记录，{len(mild_lines)} 条）", size=10, bold=True, space_after=2)
        for line in mild_lines:
            gen._para(doc, "· " + line, size=9.5, color=GRAY, space_after=1)
    if not urgent_lines and not mild_lines:
        gen._para(doc, "本轮没有触发的异动（价格未达到任何阈值）。", size=10, color=GRAY)

    # ---- AI 点评 ----
    if ai_text:
        gen._heading(doc, "AI 点评", level=2)
        gen._para(doc, ai_text, size=10.5)

    # ---- 页脚 ----
    if footer:
        gen._para(doc, footer, size=8.5, color=GRAY, align=WD_ALIGN_PARAGRAPH.LEFT, space_after=0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
