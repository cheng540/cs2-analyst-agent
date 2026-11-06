"""生成求职材料：项目一页纸（发给 HR 的那份）。

为什么不用《说明书》投递：说明书 170+ 段落，HR 不会读。
一页纸要做的是：30 秒内让人看懂「他做了什么、怎么做的、效果如何、他负责什么」。

用法： python tools/make_job_kit.py
输出： 求职材料/01_项目一页纸_CS2饰品持仓智能分析Agent.docx
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

FONT = "微软雅黑"
GRAY = RGBColor(0x59, 0x59, 0x59)
BLUE = RGBColor(0x1F, 0x4E, 0x79)


def set_font(run, size=10.5, bold=False, color=None):
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def para(doc, text, size=10.5, bold=False, color=None, space=4, align=None):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space)
    if align:
        p.alignment = align
    set_font(p.add_run(text), size, bold, color)
    return p


def bullet(doc, text, size=10):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(2)
    set_font(p.add_run(text), size)
    return p


def heading(doc, text, size=12):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    set_font(p.add_run(text), size, bold=True, color=BLUE)
    return p


def build(doc: Document) -> None:
    para(doc, "CS2 饰品持仓智能分析 Agent", 18, bold=True,
         align=WD_ALIGN_PARAGRAPH.CENTER, space=2)
    para(doc, "每日自动化行情分析 · AI 研判 · 手机推送　|　个人项目　|　2026.09 - 至今",
         10, color=GRAY, align=WD_ALIGN_PARAGRAPH.CENTER, space=2)
    para(doc, "刘乐　武昌理工学院 计算机科学与技术　2027 届　18771718390　2827110018@qq.com",
         9.5, color=GRAY, align=WD_ALIGN_PARAGRAPH.CENTER, space=10)

    heading(doc, "一句话介绍")
    para(doc, "面向 CS2 饰品交易场景的**无人值守分析系统**：每天自动拉取真实行情、结合悠悠有品交易规则"
              "（T+7 锁定、卖出 1%、提现另 1%）分析本人真实持仓、由大模型给出研判，生成 Word 报告并推送到手机；"
              "已部署上线稳定运行，每日产出报告。")

    heading(doc, "它解决什么问题")
    bullet(doc, "手动盯盘成本高：12 个条目 / 18 件饰品，逐个查价、算盈亏、看走势，每天要花大量时间；")
    bullet(doc, "决策缺乏纪律：容易追涨杀跌，也很难把「T+7 锁定期」和平台的**两级费率**"
                "（卖出 1% 进钱包 / 提现再 1%）算进每一次买卖判断；")
    bullet(doc, "信息分散：行情、官方更新公告、市场异动分散在不同页面，人不会每天都看。")

    heading(doc, "系统做了什么（数据流）")
    para(doc, "行情拉取（开放平台 API + 官网兜底）→ 持仓同步 → 多维技术分析（均线/支撑压力/位置百分位/"
              "量价/流动性）→ 规则引擎评分 → AI 研判 → Word 报告 → 企业微信推送 → 网页可查",
         10, space=6)
    para(doc, "除此之外还包含：每小时盘中巡检（分级推送告警）、网页问答（可追问持仓与行情）、"
              "每周复盘推送、A/B 双轨模拟盘对照实验。", 10, space=6)

    heading(doc, "技术要点（也是我最想讲的部分）")
    para(doc, "1. 三层防幻觉设计 —— 让大模型在「真金白银」的场景里可用", 10, bold=True, space=2)
    bullet(doc, "事实只能由 6 个工具取数（单品快照/技术分析/大盘/持仓/模拟盘/情报），提示词明确禁止模型凭记忆报价；")
    bullet(doc, "回答生成后，程序自动提取其中的金额与百分比，逐一与工具返回数据交叉校验，对不上就标注；")
    bullet(doc, "每条回答附带所用工具与原始数据，用户可以自己核对，不必相信模型的语气。")

    para(doc, "2. 模型选型：实测三种配置后按场景拆分", 10, bold=True, space=2)
    bullet(doc, "同一份事实、同一个问题对比：旧配置（未开推理）1.5 秒但只复述规则；"
                "同一模型开启推理后 6.7 秒，能给出「为什么是这两个标的」的比较性推理；"
                "更大模型 33 秒，只略好一点、价格约 4 倍 → 最终判断类开推理、交互类关闭；")
    bullet(doc, "顺带发现并修复：思考模式下温度参数失效、思考内容挤占输出预算导致空正文、"
                "工具调用轮次正文为空被误判为故障。")

    para(doc, "3. 告警分级：把「监控」和「打扰」拆开", 10, bold=True, space=2)
    bullet(doc, "巡检频率提高到每小时 16 次后，若沿用「有触发就推」，普跌日会收到十几条雷同消息，"
                "用户一旦不看，告警系统就等于废掉；")
    bullet(doc, "因此把信号分级：紧急（买点/破位/急跌/深亏跨档）立即推；温和波动静默记录，"
                "由 12:00 与 22:00 两条概览汇总；并对同类消息设每日上限，超出自动折叠。")

    para(doc, "4. 数据质量：在真实数据里踩过的坑", 10, bold=True, space=2)
    bullet(doc, "两种口径混用（挂单量 vs 当日成交数）会造出「挂单骤减 -99%」的假警报 → 加口径标记；")
    bullet(doc, "取「全平台最低价」时，某平台那一行时有时无，同一饰品一天内跳 7% → 改用实际交易平台报价；")
    bullet(doc, "「纪念品版」被写成普通版名称，按另一个物品报价导致估值差 2 倍 → 加载持仓时做名称版本一致性检查。")

    heading(doc, "效果与产出")
    bullet(doc, "每日自动产出 Word 报告 + 手机推送；报告含大盘研判、组合评估、单品建议、风险提示、AI 研判、模拟盘日报；")
    bullet(doc, "本人真实持仓 10 条 / 15 件全部纳入监控；系统对本人实际卖出操作给出过明确的清仓建议；")
    bullet(doc, "A/B 双轨模拟盘（纯规则 vs 规则+AI）各 ¥3000 运行中，用于验证「AI 到底有没有加分」；")
    bullet(doc, "224 项单元测试全部离线可跑；完整说明书含部署步骤、踩坑记录与使用手册。")

    heading(doc, "我在项目中的角色（如实说明）")
    para(doc, "本项目的代码由 **AI Agent（DeepSeek Harness）作为主要开发工具**完成，我负责：", 10, space=3)
    bullet(doc, "需求定义与领域知识：交易规则（T+7，卖出 1% + 提现 1% 的两级费率）、报价口径、真实持仓与成交数据；")
    bullet(doc, "产品决策：推送渠道、网页问答、告警分级策略、模型分场景选型、是否做盘中挂单执行；")
    bullet(doc, "质量把关：在真实使用中发现并推动修复多处缺陷——例如 AI 点评漏算 T+7 锁定期、"
                "项目长期使用已退役的模型别名（等于一直跑在最弱配置）、"
                "纪念品与普通版物品名混淆导致估值与成交价偏差 53% 的误判；")
    bullet(doc, "运行与迭代：系统上线后持续使用并反馈问题，按月复盘决策质量。")
    para(doc, "我的体会是：**用 AI 做项目，难点不在写代码，而在于能不能定义清楚问题、判断结果对不对。**",
         10, bold=True, space=6)

    para(doc, f"（本页由项目内的工具生成 · {datetime.now():%Y-%m-%d}）", 8.5, color=GRAY)


def main() -> int:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    for section in doc.sections:
        section.top_margin = Pt(46)
        section.bottom_margin = Pt(46)
        section.left_margin = Pt(52)
        section.right_margin = Pt(52)
    build(doc)
    out_dir = Path(__file__).resolve().parent.parent / "求职材料"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "01_项目一页纸_CS2饰品持仓智能分析Agent.docx"
    doc.save(str(out))
    print(f"✅ 已生成：{out}")
    print(f"   段落 {len(doc.paragraphs)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
