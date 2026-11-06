"""大盘环境快报生成（无需 API Key，仅依赖官网公开统计）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from ..analysis.market import MarketEnvironment

COLOR_GRAY = RGBColor(0x59, 0x59, 0x59)


def generate_market_report(out_path: Path, market: MarketEnvironment,
                           txn: pd.DataFrame | None = None,
                           players: pd.DataFrame | None = None,
                           run_date: datetime | None = None,
                           body_font: str = "微软雅黑") -> Path:
    """生成大盘环境快报 docx。"""
    run_date = run_date or datetime.now()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = body_font
    style.font.size = Pt(10.5)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), body_font)

    def set_font(run, size: float = 10.5, bold: bool = False, color=None) -> None:
        run.font.name = body_font
        run._element.rPr.rFonts.set(qn("w:eastAsia"), body_font)
        run.font.size = Pt(size)
        run.font.bold = bold
        if color is not None:
            run.font.color.rgb = color

    # 标题
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("CS2 大盘环境快报")
    set_font(r, 18, bold=True)
    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = p2.add_run(f"生成时间：{run_date:%Y-%m-%d %H:%M}")
    set_font(r2, 9.5, color=COLOR_GRAY)

    def line(label: str, value: str, size: float = 10.5) -> None:
        lp = doc.add_paragraph()
        lp.paragraph_format.space_after = Pt(3)
        lr = lp.add_run(label)
        set_font(lr, size, bold=True)
        vr = lp.add_run(value)
        set_font(vr, size)

    def heading(text: str) -> None:
        h = doc.add_heading("", level=1)
        hr = h.add_run(text)
        set_font(hr, 15, bold=True)

    heading("一、大盘环境综述")
    idx = f"{market.index_value:,.2f}" if market.index_value is not None else "未获取"
    diff = market.diff_ratio
    idx_line = f"大盘指数 {idx}" + (f"（较昨日 {diff:+.2f}%）" if diff is not None else "")
    if market.rise_fall_type:
        idx_line += (f"，已连续{'上涨' if market.rise_fall_type == 'UP' else '下跌'} "
                     f"{market.rise_fall_days} 天")
    line("指数表现：", idx_line)
    line("趋势判定：", market.trend)
    line("风险等级：", market.risk)
    line("操作总基调：", market.base_tone)
    if market.volume_state:
        vol_txt = market.volume_state
        if market.volume_source:
            vol_txt += f"（{market.volume_source}）"
        line("量能情况：", vol_txt)
    if market.sentiment_score is not None:
        line("平台情绪分：", f"{market.sentiment_score:.0f}（仅参考，不参与决策）")

    heading("二、判定依据")
    for ev in market.evidence:
        bp = doc.add_paragraph(style="List Bullet")
        bp.paragraph_format.space_after = Pt(1)
        br = bp.add_run(ev)
        set_font(br, 10)

    if txn is not None and not txn.empty:
        heading("三、近7日市场成交")
        tbl = doc.add_table(rows=1, cols=3)
        tbl.style = "Table Grid"
        heads = ["日期", "成交额(¥)", "成交量(件)"]
        for j, htxt in enumerate(heads):
            cell = tbl.rows[0].cells[j]
            cell.text = ""
            rr = cell.paragraphs[0].add_run(htxt)
            set_font(rr, 9.5, bold=True)
        for _, row in txn.tail(7).iterrows():
            cells = tbl.add_row().cells
            for j, val in enumerate([f"{row['date']:%Y-%m-%d}",
                                     f"{row['turnover']:,.0f}",
                                     f"{row['volume']:,.0f}"]):
                cells[j].text = ""
                rr = cells[j].paragraphs[0].add_run(val)
                set_font(rr, 9.5)

    heading("四、风险提示")
    for txt in ("本快报基于 SteamDT 官网公开统计自动生成，仅为技术分析参考，不构成投资建议；",
                "平台情绪分仅作参考，不参与本系统的决策逻辑；市场有风险，交易需谨慎。"):
        rp = doc.add_paragraph(style="List Bullet")
        rp.paragraph_format.space_after = Pt(2)
        rr = rp.add_run(txt)
        set_font(rr, 9.5, color=COLOR_GRAY)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
