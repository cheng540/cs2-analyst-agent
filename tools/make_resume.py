"""生成修订版简历（沿用原简历的视觉风格：深蓝色块标题 + 紧凑单页排版）。

用法： python tools/make_resume.py
输出： 求职材料/06_刘乐_简历_AI Agent实习_修订版.docx

设计原则：
1. 视觉风格与原简历一致（2F5496 色块标题、20pt 姓名、9.5pt 正文、灰字联系方式）；
2. 只写用户能讲清楚的内容——项目描述以「他做了什么」为主，不写"独立开发代码"这类会被追问的表述；
3. 针对 AI Agent 岗位重排顺序：CS2 项目（同时具备 AI 工程 + 该平台业务场景）放第一位；
4. 全部信息来自用户原简历与其在本项目中的真实参与，未编造任何经历、数据或证书。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

FONT = "微软雅黑"
HEADER_FILL = "2F5496"      # 与原简历一致的深蓝
RED = RGBColor(0xC0, 0x00, 0x00)
GRAY = RGBColor(0x3C, 0x3C, 0x3C)


def set_font(run, size=9.5, bold=False, color=None):
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def section(doc, text: str):
    """深蓝色块小标题（复刻原简历样式）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(4)
    pf.space_after = Pt(2)
    pf.left_indent = Pt(3)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), HEADER_FILL)
    p._p.get_or_add_pPr().append(shd)
    set_font(p.add_run(text), 11, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
    return p


def entry(doc, text: str):
    """项目/经历标题行（10pt 加粗）。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(0)
    set_font(p.add_run(text), 10, bold=True)
    return p


def bullet(doc, text: str, size=9):
    p = doc.add_paragraph(style="List Bullet")
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = 1.0
    set_font(p.add_run(text), size)
    return p


def head_line(doc, text: str, size=20, bold=True, color=None, align=WD_ALIGN_PARAGRAPH.CENTER,
              after=1):
    p = doc.add_paragraph()
    p.alignment = align
    p.paragraph_format.space_after = Pt(after)
    set_font(p.add_run(text), size, bold, color)
    return p


def build(doc: Document) -> None:
    # ---------------- 抬头 ----------------
    head_line(doc, "刘  乐", 19, True, None, after=1)
    head_line(doc, "求职意向：AI Agent / 大模型应用开发实习生", 10, True, RED, after=1)
    head_line(doc, "电话：18771718390  |  邮箱：2827110018@qq.com  |  湖北武汉  |  2027 届本科在读",
              9, False, GRAY, after=5)

    # ---------------- 教育背景 ----------------
    section(doc, " 教育背景")
    entry(doc, "2023.09 - 2027.06    武昌理工学院    计算机科学与技术    本科")
    bullet(doc, "核心课程：数据结构与算法、数据库、计算机网络、操作系统、Java Web、Python")

    # ---------------- 项目经历 ----------------
    section(doc, " 项目经历")
    entry(doc, "2026.09 - 至今    CS2 饰品持仓智能分析 Agent（每日自动化 + AI 研判）    个人项目    "
               "Python / 双数据源 API / DeepSeek / 腾讯云")
    bullet(doc, "需求与领域：按悠悠有品真实规则建模（T+7 锁定；买入免费、卖出 1%、提现另 1%），每日 17:30 自动"
                "跑完「行情拉取 → 持仓分析 → AI 研判 → Word 报告 → 手机推送」，已部署腾讯云稳定运行")
    bullet(doc, "AI 工程（防幻觉）：三层防护——模型只能经 6 个工具取数；回答中的金额与百分比由程序与工具数据"
                "交叉校验，不匹配即标注；结论附可核对依据。并按用途分离模型与推理开关")
    bullet(doc, "数据管线与质量：针对接口配额（批量价格 1 次/分钟）做多级缓存与官网兜底；识别出「挂单量 vs "
                "当日成交数」口径混用会触发虚假告警；复现并推动修复十余处缺陷"
                "（含纪念品与普通版混淆致估值偏差 53%）")
    bullet(doc, "工程：234 项单元测试全部离线可跑；公网报告页与网页问答；A/B 双轨模拟盘对照实验运行中")

    entry(doc, "2026.06 - 至今    智能对话助手 AI Agent（多模态版）    个人项目    Python / DeepSeek API")
    bullet(doc, "基于 DeepSeek Function Calling 设计工具调用循环（封装 14 个工具函数），跑通「需求识别 → "
                "工具调用 → 结果返回」链路；自研轻量 RAG（中文 bigram + BM25，无需向量库）")
    bullet(doc, "接入多模态视觉实现图片理解与多轮追问；修复 10 处缺陷（含密钥硬编码、eval 注入风险），"
                "以 AST 白名单求值替代 eval、密钥迁移 .env，并补自动化回归测试")

    entry(doc, "2026.09    QQ 智能机器人云部署实践    个人实践    Linux / Docker / 宝塔 / LangBot / NapCat")
    bullet(doc, "腾讯云全流程部署（宝塔 + Docker），实现 7×24 在线自动回复；封装 OpenAI 兼容接口供其调用，"
                "按用户维护多轮会话；排查解决容器 DNS、端口映射、双层防火墙、WebSocket 路径等故障")

    entry(doc, "2024.09 - 2024.12    校园二手交易平台    课程设计（优秀）    Spring Boot / Vue3 / MySQL")
    bullet(doc, "独立完成前后端分离开发：设计 8 张数据表并配置索引，编写 20+ RESTful API，完成接口测试与联调")

    # ---------------- 实践经历 ----------------
    # 注意：两段经历的时间与公司全称以用户提交版为准（2026.07-2026.09 物流 / 2025.07-2025.09 内容运营）。
    # 早期版本曾合并为「2025.07 - 2026.09 运营经历」，会被误读为连续在职一年，已修正。
    section(doc, " 实践经历")
    entry(doc, "2026.07 - 2026.09    安陆迅安物流有限公司    运营支持（中通 / 韵达快运）")
    bullet(doc, "工单跟进：日均处理异常问题件与工单 80+，问题解决率 95% 以上；维护发货、签收、异常件台账")
    bullet(doc, "数据复盘：每日核对数据输出运营日报，梳理高频问题与流程痛点，提出 3 项改进建议并被采纳")
    entry(doc, "2025.07 - 2025.09    悠保空间（湖北）网络科技有限公司    运营兼职")
    bullet(doc, "内容与用户运营：负责内容发布、直播答疑与意向用户跟进，单条视频最高播放量 15 万")

    # ---------------- 技能特长 ----------------
    section(doc, " 技能特长")
    bullet(doc, "AI Agent 工程：工具调用（Function Calling）循环设计；防幻觉实践（事实归工具 + 数字交叉校验 + "
                "依据透明）；Prompt 迭代与模型选型（推理开关、成本与延迟权衡）")
    bullet(doc, "大模型应用与部署：RAG 检索（中文 bigram + BM25）、多模态图片理解；Linux、宝塔、Docker、systemd、"
                "cron，能排查 DNS / 端口映射 / 防火墙 / WebSocket 类故障")
    bullet(doc, "开发与证书：Python（接口调用、函数封装、异常处理）；Spring Boot / Vue3 / MySQL 基础；Git、"
                "Postman；大学英语四级、全国计算机二级（WPS 优秀）、普通话二甲、C1 驾驶证；可全职实习")


def main() -> int:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(9)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st = style.paragraph_format
    st.space_before = Pt(0)
    st.space_after = Pt(0)
    st.line_spacing = 0.98
    for section_ in doc.sections:
        section_.top_margin = Pt(36)
        section_.bottom_margin = Pt(30)
        section_.left_margin = Pt(53.85)
        section_.right_margin = Pt(53.85)

    build(doc)

    out_dir = Path(__file__).resolve().parent.parent / "求职材料"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "06_刘乐_简历_AI Agent实习_修订版.docx"
    doc.save(str(out))
    # 只输出 ASCII，避免 Windows 控制台 GBK 编码报错
    print(f"[OK] {out.name}  paragraphs={len(doc.paragraphs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
