"""每日 Word 分析报告生成（python-docx）。

固定结构：
  报告头（日期/大盘总基调/整体盈亏概览）
  一、大盘环境综述
  二、持仓整体评估
  三、持仓单品详细分析
  四、操作建议汇总表
  五、风险提示
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..analysis.market import MarketEnvironment
from ..engine.decision import Decision

COLOR_STOP = RGBColor(0xC0, 0x00, 0x00)
COLOR_PROFIT = RGBColor(0x1E, 0x7B, 0x1E)
COLOR_HOLD = RGBColor(0xB8, 0x86, 0x0B)
COLOR_GRAY = RGBColor(0x59, 0x59, 0x59)


@dataclass
class PortfolioSummary:
    """组合整体概览。"""
    total_cost: float = 0.0
    total_value: float = 0.0
    total_pnl: float = 0.0
    pnl_pct: float = 0.0
    item_count: int = 0
    piece_count: int = 0
    data_ok_count: int = 0
    missing_names: list[str] = field(default_factory=list)


class ReportGenerator:
    """生成结构化 Word 报告。"""

    def __init__(self, heading_font: str = "微软雅黑", body_font: str = "微软雅黑",
                 author: str = "CS2 持仓智能分析 Agent",
                 name_map: dict[str, str] | None = None):
        self.heading_font = heading_font
        self.body_font = body_font
        self.author = author
        # 英文 marketHashName → 中文名（报告展示用；缺失时只显示英文名）
        self.name_map = {k: v for k, v in (name_map or {}).items() if k and v}

    def _dn(self, market_hash_name: str) -> str:
        """显示名：中文名（英文名）；无中文对照时只显示英文名。"""
        cn = self.name_map.get(market_hash_name)
        if cn and cn != market_hash_name:
            return f"{cn}（{market_hash_name}）"
        return market_hash_name

    def _fix_text(self, text: str) -> str:
        """把文本中出现的英文饰品名替换为「中文（英文）」形式。"""
        for en, cn in self.name_map.items():
            if en and en in text:
                text = text.replace(en, f"{cn}（{en}）" if cn != en else en)
        return text

    # ---------------- 基础工具 ----------------
    @staticmethod
    def _set_font(run, name: str, size: float | None = None, bold: bool | None = None,
                  color=None) -> None:
        run.font.name = name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
        if size is not None:
            run.font.size = Pt(size)
        if bold is not None:
            run.font.bold = bold
        if color is not None:
            run.font.color.rgb = color

    def _para(self, doc: Document, text: str = "", size: float = 10.5, bold: bool = False,
              color=None, align=None, space_after: float = 4) -> Any:
        p = doc.add_paragraph()
        if align is not None:
            p.alignment = align
        p.paragraph_format.space_after = Pt(space_after)
        if text:
            run = p.add_run(text)
            self._set_font(run, self.body_font, size, bold, color)
        return p

    def _label_line(self, doc: Document, label: str, value: str, size: float = 10.5) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        r1 = p.add_run(label)
        self._set_font(r1, self.body_font, size, bold=True)
        r2 = p.add_run(value)
        self._set_font(r2, self.body_font, size)

    def _heading(self, doc: Document, text: str, level: int = 1) -> None:
        h = doc.add_heading("", level=level)
        run = h.add_run(text)
        self._set_font(run, self.heading_font,
                       16 if level == 1 else (13 if level == 2 else 11.5), bold=True)

    @staticmethod
    def _cell_text(cell, text: str, size: float = 9.5, bold: bool = False, color=None) -> None:
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run(text)
        ReportGenerator._set_font(run, "微软雅黑", size, bold, color)

    # ---------------- 主流程 ----------------
    def generate(self, out_path: Path, market: MarketEnvironment,
                 summary: PortfolioSummary,
                 details: list[tuple[Any, Decision]],
                 run_date: datetime | None = None,
                 mode_label: str = "",
                 sim: dict | None = None) -> Path:
        """生成完整报告，返回文件路径。"""
        doc = Document()
        # 全局默认样式
        style = doc.styles["Normal"]
        style.font.name = self.body_font
        style.font.size = Pt(10.5)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), self.body_font)

        run_date = run_date or datetime.now()
        date_str = run_date.strftime("%Y-%m-%d %H:%M")
        self._judgement = (sim or {}).get("judgement") if sim else None

        # =============== 标题与报告头 ===============
        title = self._para(doc, f"CS2 饰品持仓智能分析日报", size=18, bold=True,
                           align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
        self._para(doc, f"报告日期：{date_str}", size=9.5, color=COLOR_GRAY,
                   align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
        if mode_label:
            self._para(doc, f"数据源：{mode_label}", size=9.5, color=COLOR_GRAY,
                       align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)

        head = doc.add_table(rows=4, cols=2)
        head.style = "Table Grid"
        head.alignment = WD_TABLE_ALIGNMENT.CENTER
        rows = [
            ("大盘总基调", market.base_tone),
            ("大盘风险等级", f"{market.trend} / {market.risk}"),
            ("整体持仓盈亏",
             f"总市值 ¥{summary.total_value:,.2f} | 总盈亏 ¥{summary.total_pnl:+,.2f} "
             f"({summary.pnl_pct:+.2f}%)"),
            ("数据完整性", f"{summary.data_ok_count}/{summary.item_count} 个条目数据完整"),
        ]
        for i, (k, v) in enumerate(rows):
            self._cell_text(head.rows[i].cells[0], k, 9.5, bold=True)
            self._cell_text(head.rows[i].cells[1], v, 9.5)

        # =============== 一、大盘环境综述 ===============
        self._heading(doc, "一、大盘环境综述")
        idx_line = f"大盘指数：{market.index_value:,.2f}" if market.index_value is not None else "大盘指数：未获取"
        if market.diff_ratio is not None:
            idx_line += f"（较昨日 {market.diff_ratio:+.2f}%）"
        self._label_line(doc, "指数表现：", idx_line)
        trend_txt = market.trend
        if market.ma_state:
            trend_txt += f"（日K {market.ma_state}"
            trend_txt += f"，周K {market.weekly_trend}" if market.weekly_trend else ""
            trend_txt += "）"
        self._label_line(doc, "趋势判定：", trend_txt)
        vol_txt = market.volume_state
        if market.volume_source:
            vol_txt += f"（{market.volume_source}）"
        else:
            vol_txt += "（日内振幅代理，见备注）"
        self._label_line(doc, "量能情况：", vol_txt)
        if market.sentiment_score is not None:
            self._label_line(doc, "平台情绪分：", f"{market.sentiment_score:.0f}（仅参考，不参与决策）")
        self._label_line(doc, "风险等级：", market.risk)
        self._label_line(doc, "操作总基调：", market.base_tone)
        if market.evidence:
            for ev in market.evidence:
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.space_after = Pt(1)
                run = p.add_run(ev)
                self._set_font(run, self.body_font, 10)

        # =============== 二、持仓整体评估 ===============
        self._heading(doc, "二、持仓整体评估")
        self._label_line(doc, "持仓条目：", f"{summary.item_count} 个条目 / 共 {summary.piece_count} 件饰品")
        self._label_line(doc, "持仓总成本：", f"¥{summary.total_cost:,.2f}")
        self._label_line(doc, "持仓总市值：", f"¥{summary.total_value:,.2f}")
        pnl_color = COLOR_PROFIT if summary.total_pnl >= 0 else COLOR_STOP
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        r1 = p.add_run("总浮动盈亏：")
        self._set_font(r1, self.body_font, 10.5, bold=True)
        r2 = p.add_run(f"¥{summary.total_pnl:+,.2f}（{summary.pnl_pct:+.2f}%）")
        self._set_font(r2, self.body_font, 10.5, bold=True, color=pnl_color)

        # 持仓结构：按市值占比排序
        structure = sorted(details, key=lambda x: x[0].weight, reverse=True)
        self._label_line(doc, "持仓结构：", "按市值占比排序如下")
        struct_tbl = doc.add_table(rows=1, cols=4)
        struct_tbl.style = "Table Grid"
        for j, htxt in enumerate(["饰品", "市值", "占比", "盈亏比例"]):
            self._cell_text(struct_tbl.rows[0].cells[j], htxt, 9.5, bold=True)
        for item, _ in structure:
            row = struct_tbl.add_row().cells
            self._cell_text(row[0], self._dn(item.holding.market_hash_name), 9)
            self._cell_text(row[1], f"¥{item.market_value:,.2f}", 9)
            self._cell_text(row[2], f"{item.weight*100:.1f}%", 9)
            color = COLOR_PROFIT if item.pnl_pct >= 0 else COLOR_STOP
            self._cell_text(row[3], f"{item.pnl_pct*100:+.2f}%", 9, color=color)

        if summary.missing_names:
            self._para(doc, f"⚠ 数据缺失条目（已跳过分析，请检查名称是否正确）："
                            f"{'、'.join(self._dn(n) for n in summary.missing_names)}",
                       9.5, color=COLOR_STOP)

        # 组合级策略判断（我的整体结论）
        commentary = self._portfolio_commentary(market, summary, details)
        if commentary:
            self._para(doc, commentary, 10, bold=False)

        # =============== 三、持仓单品详细分析 ===============
        self._heading(doc, "三、持仓单品详细分析")
        for idx, (item, decision) in enumerate(sorted(details, key=lambda x: x[0].holding.market_hash_name), 1):
            self._heading(doc, f"3.{idx} {self._dn(item.holding.market_hash_name)}", 2)
            if not item.data_ok:
                self._para(doc, f"⚠ 数据缺失：{item.missing_reason}", 10, color=COLOR_STOP)
                continue
            self._item_basic_table(doc, item)
            self._item_trend(doc, item)
            self._item_decision(doc, decision, item)

        # =============== 四、操作建议汇总表 ===============
        self._heading(doc, "四、操作建议汇总表")
        summary_tbl = doc.add_table(rows=1, cols=5)
        summary_tbl.style = "Table Grid"
        for j, htxt in enumerate(["建议", "饰品", "建议仓位", "盈亏比例", "核心理由"]):
            self._cell_text(summary_tbl.rows[0].cells[j], htxt, 9.5, bold=True)
        order = ["CLEAR", "REDUCE", "HOLD", "ADD", "BUY"]
        for item, decision in sorted(details, key=lambda x: (order.index(x[1].action.value)
                                                             if x[1].action.value in order else 9)):
            row = summary_tbl.add_row().cells
            dcolor = {"CLEAR": COLOR_STOP, "REDUCE": RGBColor(0xC8, 0x6A, 0x00),
                      "BUY": COLOR_PROFIT, "ADD": COLOR_PROFIT,
                      "HOLD": COLOR_HOLD}.get(decision.action.value)
            self._cell_text(row[0], decision.label, 9, bold=True, color=dcolor)
            self._cell_text(row[1], self._dn(item.holding.market_hash_name), 9)
            weight_txt = (f"{decision.target_weight_pct}%" if decision.target_weight_pct is not None
                          else "-")
            self._cell_text(row[2], weight_txt, 9)
            self._cell_text(row[3], f"{item.pnl_pct*100:+.2f}%" if item.data_ok else "-", 9)
            self._cell_text(row[4], "；".join(decision.reasons)[:80] if decision.reasons else "-", 9)
        # 统计行
        from collections import Counter
        counts = Counter(d.action.value for _, d in details)
        stat = " | ".join(f"{lab}：{counts.get(act, 0)} 项"
                          for act, lab in [("BUY", "买入"), ("ADD", "加仓"), ("HOLD", "持有"),
                                           ("REDUCE", "减仓"), ("CLEAR", "清仓")])
        self._para(doc, f"建议分布统计：{stat}", 9.5, color=COLOR_GRAY)

        # =============== 五、风险提示 ===============
        self._heading(doc, "五、风险提示")
        risks = [
            "本报告由技术分析模型自动生成，所有建议仅为技术分析参考，不构成任何投资/交易建议；",
            "饰品市场受游戏热度、版本更新、政策与情绪影响巨大，价格可能剧烈波动，市场有风险，交易需谨慎；",
            "SteamDT 单品K线接口不提供成交量数据，报告中「放量/缩量」为日内振幅代理指标，准确性有限；",
            "流动性等级基于各平台在售与求购挂单量估算，实际成交深度可能不同，低流动性饰品请警惕滑价；",
            "止损/止盈/补仓阈值可在 config/config.yaml 中自定义调整；建议结合自身风险承受能力与资金状况综合决策。",
        ]
        if any(item.snapshot_mode for item, _ in details):
            risks.append(
                "本报告为「官网公开快照模式」生成：单品无历史K线，均线/支撑压力/位置百分位不可用，"
                "补仓类建议不会触发；补齐 API Key 后自动升级为完整技术分析。")
        for r in risks:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(r)
            self._set_font(run, self.body_font, 9.5, color=COLOR_GRAY)

        # =============== 六、AI 智能研判 ===============
        judgement = (sim or {}).get("judgement") if sim else None
        if judgement:
            self._heading(doc, "六、AI 智能研判（DeepSeek）")
            self._label_line(doc, "大盘与情报解读：", judgement.get("market_view", ""), 10)
            self._label_line(doc, "组合建议：", judgement.get("portfolio_advice", ""), 10)
            warns = judgement.get("risk_warnings") or []
            if warns:
                self._para(doc, "AI 提示的风险点：", 10, bold=True)
                for w in warns:
                    p = doc.add_paragraph(style="List Bullet")
                    p.paragraph_format.space_after = Pt(1)
                    run = p.add_run(str(w))
                    self._set_font(run, self.body_font, 9.5)
            intel = (sim or {}).get("intel_summary") or {}
            if intel.get("top_news"):
                self._para(doc, f"当日情报（更新公告/新闻 {intel.get('news_count', 0)} 条，"
                                f"市场异动 {intel.get('anomaly_count', 0)} 条）：", 10, bold=True)
                for t in intel["top_news"]:
                    p = doc.add_paragraph(style="List Bullet")
                    p.paragraph_format.space_after = Pt(1)
                    run = p.add_run(str(t)[:90])
                    self._set_font(run, self.body_font, 9.5, color=COLOR_GRAY)
            self._para(doc, "注：AI 研判基于当日事实与情报生成，与规则引擎的分歧点已在各单品处标注；"
                            "AI 不修改任何行情事实，仅对 B 轨模拟盘拥有「否决买入 / 提议试仓」两种有限权限。",
                       9, color=COLOR_GRAY)

        # =============== 七、模拟盘日报 ===============
        if sim:
            self._heading(doc, "七、模拟盘日报" + ("（双轨对照）" if sim.get("tracks") else ""))
            self._sim_section(doc, sim, run_date)

        # 页脚
        footer = doc.sections[0].footer
        fp = footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fr = fp.add_run(f"生成于 {date_str} · {self.author} · 数据源：SteamDT 开放平台")
        self._set_font(fr, self.body_font, 8, color=COLOR_GRAY)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
        return out_path

    # ---------------- 模拟盘分块 ----------------
    def _sim_section(self, doc: Document, sim: dict, run_date: datetime) -> None:
        tracks = sim.get("tracks") or {}
        if tracks:
            self._sim_tracks(doc, tracks, sim)
            return
        # 兼容旧结构（单轨）
        self._sim_tracks(doc, {"A": {"label": "A", "name": "模拟盘",
                                     "snapshot": sim.get("snapshot") or {},
                                     "entry": sim.get("entry") or {},
                                     "trades_today": sim.get("trades_today") or [],
                                     "overlay_notes": []}}, sim)

    def _sim_tracks(self, doc: Document, tracks: dict, sim: dict) -> None:
        rule_desc = "T+7 交易规则：买入后 7 天内不可卖出（平台规则），锁定期内的卖出指令自动顺延"
        self._para(doc, rule_desc, 9.5, color=COLOR_GRAY)

        # 轨道对比
        tbl = doc.add_table(rows=1, cols=5)
        tbl.style = "Table Grid"
        for j, htxt in enumerate(["轨道", "账户净值", "累计收益", "现金", "持仓 / 累计成交"]):
            self._cell_text(tbl.rows[0].cells[j], htxt, 9.5, bold=True)
        for label in sorted(tracks.keys()):
            t = tracks[label]
            snap = t.get("snapshot") or {}
            row = tbl.add_row().cells
            self._cell_text(row[0], f"{label} 轨（{t.get('name', '')}）", 9, bold=True)
            self._cell_text(row[1], f"¥{snap.get('net_value', 0):,.2f}", 9)
            ret = snap.get("return_pct", 0.0)
            self._cell_text(row[2], f"{ret:+.2f}%", 9,
                            color=COLOR_PROFIT if ret >= 0 else COLOR_STOP)
            self._cell_text(row[3], f"¥{snap.get('cash', 0):,.2f}", 9)
            self._cell_text(row[4], f"{len(snap.get('positions') or [])} 项 / "
                                    f"{snap.get('trade_count', 0)} 笔", 9)
        if len(tracks) > 1:
            vals = {k: (v.get("snapshot") or {}).get("net_value", 0.0) for k, v in tracks.items()}
            a, b = vals.get("A", 0.0), vals.get("B", 0.0)
            self._para(doc, f"轨道差异：A 轨（纯规则）¥{a:,.2f} vs B 轨（规则+AI）¥{b:,.2f}，"
                            f"差值 {b - a:+,.2f} 元（初始资金 "
                            f"¥{(tracks.get('A', {}).get('snapshot') or {}).get('initial_capital', 0):,.2f}）",
                       9.5, color=COLOR_GRAY)

        for label in sorted(tracks.keys()):
            t = tracks[label]
            snap = t.get("snapshot") or {}
            entry = t.get("entry") or {}
            self._para(doc, f"—— {label} 轨（{t.get('name', '')}） ——", 10.5, bold=True)
            self._label_line(doc, "今日操作数：", f"{entry.get('trades', 0)} 笔", 9.5)
            notes = entry.get("locked_notes") or []
            if notes:
                self._para(doc, "T+7 锁定期顺延：", 9.5, bold=True, color=COLOR_HOLD)
                for n in notes:
                    run = self._para(doc, f"· {self._fix_text(n)}", 9, color=COLOR_HOLD)
            if t.get("overlay_notes"):
                self._para(doc, "AI 干预记录：", 9.5, bold=True)
                for n in t["overlay_notes"]:
                    self._para(doc, f"· {self._fix_text(n)}", 9, color=COLOR_GRAY)

            trades = t.get("trades_today") or []
            if trades:
                tt = doc.add_table(rows=1, cols=5)
                tt.style = "Table Grid"
                for j, htxt in enumerate(["方向", "标的", "数量", "价格", "金额"]):
                    self._cell_text(tt.rows[0].cells[j], htxt, 9, bold=True)
                for tr in trades:
                    cells = tt.add_row().cells
                    dir_cn = {"BUY": "买入", "ADD": "加仓", "REDUCE": "减仓",
                              "CLEAR": "清仓"}.get(tr.get("action"), tr.get("action"))
                    color = COLOR_PROFIT if tr.get("action") in ("BUY", "ADD") else COLOR_STOP
                    self._cell_text(cells[0], dir_cn, 9, color=color)
                    self._cell_text(cells[1], self._dn(tr.get("name", "")), 9)
                    self._cell_text(cells[2], str(tr.get("qty", "")), 9)
                    self._cell_text(cells[3], f"¥{tr.get('price', 0):,.2f}", 9)
                    self._cell_text(cells[4], f"¥{tr.get('amount', 0):,.2f}", 9)

            positions = snap.get("positions") or []
            if positions:
                pt = doc.add_table(rows=1, cols=6)
                pt.style = "Table Grid"
                for j, htxt in enumerate(["标的", "数量", "成本价", "现价", "浮盈", "可售状态"]):
                    self._cell_text(pt.rows[0].cells[j], htxt, 9, bold=True)
                for p in positions:
                    cells = pt.add_row().cells
                    self._cell_text(cells[0], self._dn(p["name"]), 9)
                    self._cell_text(cells[1], str(p["qty"]), 9)
                    self._cell_text(cells[2], f"¥{p['avg_cost']:,.2f}", 9)
                    self._cell_text(cells[3], f"¥{p['price']:,.2f}", 9)
                    color = COLOR_PROFIT if p["pnl_pct"] >= 0 else COLOR_STOP
                    self._cell_text(cells[4], f"{p['pnl_pct']:+.2f}%", 9, color=color)
                    if p.get("locked"):
                        self._cell_text(cells[5], f"锁定期内，可售日 {p.get('locked_until')}"
                                                  f"（剩 {p.get('days_left')} 天）", 9,
                                        color=COLOR_HOLD)
                    else:
                        self._cell_text(cells[5], "可售", 9, color=COLOR_PROFIT)
            else:
                self._para(doc, "当前持仓：空仓（现金等待机会）", 9, color=COLOR_GRAY)
            self._label_line(doc, "今日操盘手记：", entry.get("thoughts", ""), 9.5)

        # 净值历史（取 B 轨优先）
        primary = tracks.get("B") or tracks.get("A") or {}
        journal = (primary.get("snapshot") or {}).get("journal") or []
        if journal:
            self._para(doc, "净值历史（每日记录）：", 10, bold=True)
            tbl3 = doc.add_table(rows=1, cols=4)
            tbl3.style = "Table Grid"
            for j, htxt in enumerate(["日期", "净值", "累计收益", "当日操作"]):
                self._cell_text(tbl3.rows[0].cells[j], htxt, 9, bold=True)
            for jr in journal[-20:]:
                row = tbl3.add_row().cells
                self._cell_text(row[0], str(jr.get("date", "")), 9)
                self._cell_text(row[1], f"¥{jr.get('net_value', 0):,.2f}", 9)
                color = COLOR_PROFIT if (jr.get("return_pct") or 0) >= 0 else COLOR_STOP
                self._cell_text(row[2], f"{jr.get('return_pct', 0):+.2f}%", 9, color=color)
                self._cell_text(row[3], str(jr.get("trades", 0)), 9)

    @staticmethod
    def _portfolio_commentary(market: MarketEnvironment, summary: PortfolioSummary,
                              details: list) -> str:
        """组合级策略判断：把数字翻译成可执行的结论。"""
        if not details:
            return ""
        acts = [d.action.value for _, d in details]
        n_reduce = acts.count("REDUCE") + acts.count("CLEAR")
        deep = sum(1 for i, _ in details if i.data_ok and i.pnl_pct <= -0.30)
        low_liq = sum(1 for i, _ in details if i.data_ok and i.liquidity_grade == "低")
        no_data = sum(1 for i, _ in details if not i.data_ok)
        pnl = summary.pnl_pct

        if pnl <= -25:
            state = "整体深度套牢"
        elif pnl < 0:
            state = "整体小幅浮亏"
        elif pnl < 15:
            state = "整体小幅浮盈"
        else:
            state = "整体浮盈较厚"

        parts = [f"组合{state}（{pnl:+.1f}%），其中 {deep} 个条目亏损超 30%；"
                 f"大盘处于{market.trend}·{market.risk}。"]
        if market.block_add():
            parts.append("当前属风险释放阶段，不宜新开仓位或补仓摊薄成本，"
                         "首要任务是把仓位结构调向「高流动性 + 抗跌」的品种。")
        if n_reduce:
            parts.append(f"建议对评分转弱的 {n_reduce} 个条目分批减仓："
                         "优先处置趋势破位、流动性差的品种，单次挂单不超过日成交量的 5%，"
                         "回笼的现金先保留，等大盘企稳信号（连续缩量止跌 + 大盘指数重回 MA20）再考虑再配置。")
        if low_liq:
            parts.append(f"注意有 {low_liq} 个条目流动性偏差，减持需分批挂单，避免砸价。")
        if deep >= 3:
            parts.append("深套品种不必一次性割肉：可先减仓降低集中度，"
                         "保留小仓位等待超跌反弹（低价位区间反弹弹性通常更大）。")
        if no_data:
            parts.append(f"{no_data} 个条目缺行情数据（贴纸等品类），仅按手动市值计入组合。")
        return "".join(parts)

    # ---------------- 单品分块 ----------------
    def _item_basic_table(self, doc: Document, item: Any) -> None:
        h = item.holding
        rows = 4 + (1 if h.lots_differ else 0) + (1 if h.sell_intent else 0)
        t = doc.add_table(rows=rows, cols=4)
        t.style = "Table Grid"
        cells = [
            ("名称", self._dn(h.market_hash_name), "磨损", h.wear or "-"),
            ("持仓量", f"{h.quantity} 件", "购入价",
             f"¥{h.buy_price:,.2f}" + (f"（{h.buy_date}）" if h.buy_date else "")
             + ("（已过 T+7 锁定期）" if getattr(h, "unlocked", False) and not h.buy_date else "")),
            ("当前价", f"¥{item.cur_price:,.2f}" + (f"（7日均价 ¥{item.avg7_price:,.2f}）" if item.avg7_price else ""),
             "市值", f"¥{item.market_value:,.2f}"),
        ]
        pnl_color = COLOR_PROFIT if item.pnl_pct >= 0 else COLOR_STOP
        for i in range(3):
            for j in range(4):
                self._cell_text(t.rows[i].cells[j], cells[i][j], 9, bold=(j % 2 == 0))
        row3 = t.rows[3].cells
        self._cell_text(row3[0], "浮动盈亏", 9, bold=True)
        self._cell_text(row3[1], f"¥{item.pnl:+,.2f}（{item.pnl_pct*100:+.2f}%）", 9, color=pnl_color)
        self._cell_text(row3[2], "占组合比例", 9, bold=True)
        self._cell_text(row3[3], f"{item.weight*100:.1f}%", 9)
        extra = 4
        # 分件成本：同一饰品多次买入价不同 → 逐件列出（决定"先卖哪一件"时要用）
        if h.lots_differ:
            try:
                ordered = sorted(h.lot_prices, reverse=True)
                detail = " / ".join(
                    f"¥{v:,.2f}（{(float(item.cur_price) / v - 1) * 100:+.1f}%）" for v in ordered)
            except (TypeError, ValueError, ZeroDivisionError):
                detail = " / ".join(f"¥{float(v):,.2f}" for v in h.lot_prices)
            row = t.rows[extra].cells
            self._cell_text(row[0], "分件成本", 9, bold=True)
            self._cell_text(row[1], detail, 9)
            self._cell_text(row[2], "说明", 9, bold=True)
            self._cell_text(row[3], "各件成本不同；卖出时先卖哪件由你决定，成本高低本身不影响收益", 9)
            extra += 1
        if h.sell_intent:
            row = t.rows[extra].cells
            self._cell_text(row[0], "用户意图", 9, bold=True)
            self._cell_text(row[1], h.status or "待售", 9, color=COLOR_STOP)
            self._cell_text(row[2], "处理", 9, bold=True)
            self._cell_text(row[3], "已按你的决定给出清仓建议（按最低在售价上架；卖出费 1%，提现再 1%）", 9)

    def _item_trend(self, doc: Document, item: Any) -> None:
        if item.manual_price_based:
            self._label_line(doc, "走势分析：",
                             "该品类（贴纸等）暂无 API 行情数据，不提供均线/支撑压力等技术分析；"
                             "估值采用手动市价，建议定期更新 config/inventory.json 中的 manual_price。", 10)
            return
        if item.snapshot_mode:
            self._item_trend_snapshot(doc, item)
            return
        self._label_line(doc, "趋势判定：",
                         f"{item.trend}（均线{item.ma_state}；"
                         f"MA5={item.ma_values.get('MA5')}，MA10={item.ma_values.get('MA10')}，"
                         f"MA20={item.ma_values.get('MA20')}）", 10)
        self._label_line(doc, "价格位置：",
                         f"近6个月区间 {item.position_label}（百分位 {item.position_pct}%）；"
                         f"5日 {item.ret_5d:+.2f}%，3日 {item.ret_3d:+.2f}%", 10)
        sup = "、".join(f"¥{s:,.2f}" for s in item.supports) or "-"
        res = "、".join(f"¥{s:,.2f}" for s in item.resistances) or "-"
        self._label_line(doc, "支撑/压力：",
                         f"支撑位 {sup}；压力位 {res}", 10)
        liq = (f"{item.liquidity_grade}（在售 {item.sell_count} / 求购 {item.bid_count}"
               + (f"，{item.liquidity_trend}" if item.liquidity_trend else "") + "）")
        self._label_line(doc, "流动性：", liq, 10)
        vol_txt = f"{item.volume_state}（振幅比 {item.amp_ratio}）"
        if item.break_support and item.broken_support and item.vol_up_down:
            vol_txt += f"；⚠ 已跌破前支撑 {item.broken_support:,.2f} 且放量下跌"
        self._label_line(doc, "量价分析：", vol_txt, 10)

    def _item_trend_snapshot(self, doc: Document, item: Any) -> None:
        """快照模式走势段（无历史K线，展示官网真实成交数据）。"""
        d1 = item.ret_3d if item.ret_3d is not None else float("nan")
        d7 = item.ret_5d if item.ret_5d is not None else float("nan")
        d30 = item.ret_30d if item.ret_30d is not None else float("nan")
        d6m = item.ret_6m if item.ret_6m is not None else float("nan")
        self._label_line(doc, "趋势判定：",
                         f"{item.trend}（近1日 {d1:+.2f}% / 近7日 {d7:+.2f}% / "
                         f"近30日 {d30:+.2f}% / 近6个月 {d6m:+.2f}%）", 10)
        sup = "、".join(f"{r.get('platformName') or r.get('platform')} ¥{r.get('price')}"
                        for r in item.price_rows if r.get("price")) or "-"
        self._label_line(doc, "各平台在售价：", sup, 10)
        liq = (f"{item.liquidity_grade}（当日成交 {item.transaction_count} 件"
               + (f"，换手率 {item.turnover_rate}%" if item.turnover_rate is not None else "")
               + (f"，存世量 {item.survive_num:,}" if item.survive_num is not None else "")
               + (f"，持有人 {item.holders_num:,}" if item.holders_num is not None else "")
               + (f"，求购/在售比 {item.cash_ratio}" if item.cash_ratio is not None else "")
               + "）")
        self._label_line(doc, "流动性：", liq, 10)
        vol_txt = (f"{item.volume_state}（量比 {item.amp_ratio}，"
                   f"今日成交量/近5日均量）" if item.amp_ratio is not None
                   else f"{item.volume_state}（量比缺失）")
        self._label_line(doc, "量价分析：", vol_txt, 10)
        self._para(doc, "注：快照模式无历史K线，均线/支撑压力/位置百分位不可用；"
                        "补齐 API Key 后可升级为完整技术分析。", 9, color=COLOR_GRAY)

    def _item_decision(self, doc: Document, decision: Decision, item: Any = None) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        color = {"CLEAR": COLOR_STOP, "REDUCE": RGBColor(0xC8, 0x6A, 0x00),
                 "BUY": COLOR_PROFIT, "ADD": COLOR_PROFIT,
                 "HOLD": COLOR_HOLD}.get(decision.action.value)
        r1 = p.add_run("操作建议：")
        self._set_font(r1, self.body_font, 10.5, bold=True)
        r2 = p.add_run(decision.label)
        self._set_font(r2, self.body_font, 11, bold=True, color=color)
        stars = "★" * decision.conviction + "☆" * (5 - decision.conviction)
        r3 = p.add_run(f"  信心度 {stars}  综合评分 {decision.score:+.1f}"
                       + (f"  目标仓位 {decision.target_weight_pct}%" if decision.target_weight_pct is not None else ""))
        self._set_font(r3, self.body_font, 9, color=COLOR_GRAY)
        for reason in decision.reasons:
            p2 = doc.add_paragraph()
            p2.paragraph_format.space_after = Pt(1)
            run = p2.add_run(f"理由：{self._fix_text(reason)}")
            self._set_font(run, self.body_font, 10)
        p3 = doc.add_paragraph()
        p3.paragraph_format.space_after = Pt(6)
        run = p3.add_run(f"操作：{self._fix_text(decision.action_text)}")
        self._set_font(run, self.body_font, 10)
        # AI 研判点评（若有）
        judgement = getattr(self, "_judgement", None)
        if judgement and item is not None:
            name = item.holding.market_hash_name
            comment = str((judgement.get("items") or {}).get(name, {}).get("comment") or "")
            risk = str((judgement.get("items") or {}).get(name, {}).get("key_risk") or "")
            if comment:
                p4 = doc.add_paragraph()
                p4.paragraph_format.space_after = Pt(6)
                r_ai = p4.add_run(f"🤖 AI 研判：{comment}")
                self._set_font(r_ai, self.body_font, 10, color=RGBColor(0x1F, 0x4E, 0x79))
                if risk:
                    r_risk = p4.add_run(f"（关键风险：{risk}）")
                    self._set_font(r_risk, self.body_font, 9, color=COLOR_GRAY)
