"""把用户自己截的「项目运作截图」重做成一份带标题与说明的 Word。

原文件问题：4 张图、0 个字——看的人不知道在看什么。
本脚本：保留原图（从原 docx 里提取），加标题、总述、每图的说明与「这张图证明了什么」。

用法： python tools/make_screenshot_doc.py
输出： 求职材料/12_项目运作截图（配说明）.docx
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt, RGBColor, Inches  # noqa: E402

FONT = "微软雅黑"
GRAY = RGBColor(0x59, 0x59, 0x59)
BLUE = RGBColor(0x1F, 0x4E, 0x79)
RED = RGBColor(0xC0, 0x00, 0x00)

ROOT = Path(__file__).resolve().parent.parent
MAT = ROOT / "求职材料"
SRC_DOCX = MAT / "项目运作截图.docx"
WORK = MAT / "_截图提取"
OUT = MAT / "12_项目运作截图（配说明）.docx"


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


def h1(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(5)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), "2F5496")
    p._p.get_or_add_pPr().append(shd)
    set_font(p.add_run(text), 13, True, RGBColor(0xFF, 0xFF, 0xFF))


def pic(doc, path: Path, width_pt: float, caption: str = ""):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(2)
    p.add_run().add_picture(str(path), width=Pt(width_pt))
    if caption:
        c = doc.add_paragraph()
        c.alignment = WD_ALIGN_PARAGRAPH.CENTER
        c.paragraph_format.space_after = Pt(8)
        set_font(c.add_run(caption), 9, False, GRAY)


def extract_images() -> dict[str, Path]:
    WORK.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    with zipfile.ZipFile(SRC_DOCX) as z:
        for name in [n for n in z.namelist() if n.startswith("word/media/") and not n.endswith("/")]:
            data = z.read(name)
            path = WORK / Path(name).name
            path.write_bytes(data)
            out[Path(name).name] = path
    return out


def split_tall(src: Path, parts: int, keep_ratio: float = 1.0, tag: str = "seg") -> list[Path]:
    """把竖版长截图切成若干块（Word 一页放不下手机长截图）。"""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    h = int(h * keep_ratio)
    im = im.crop((0, 0, w, h))
    step = h // parts
    outs = []
    for i in range(parts):
        top, bottom = i * step, (h if i == parts - 1 else (i + 1) * step)
        piece = im.crop((0, top, w, bottom))
        p = WORK / f"{tag}_{i + 1}.jpg"
        piece.save(p, quality=88)
        outs.append(p)
    return outs


def build() -> None:
    imgs = extract_images()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    for s in doc.sections:
        s.top_margin = Pt(46)
        s.bottom_margin = Pt(46)
        s.left_margin = Pt(54)
        s.right_margin = Pt(54)

    # 封面区
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t.paragraph_format.space_after = Pt(2)
    set_font(t.add_run("CS2 饰品持仓智能分析 Agent"), 18, True)
    t2 = doc.add_paragraph()
    t2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t2.paragraph_format.space_after = Pt(10)
    set_font(t2.add_run("系统实际运行截图（真实数据 · 非演示数据）"), 11, False, GRAY)

    para(doc, "本页收集的是系统在真实运行中产生的画面：每日自动推送到手机的日报、"
              "自动生成的 Word 报告，以及网页问答的实际对话。", 10.5, space=3)
    para(doc, "系统的运行节奏：每天 17:30 自动跑完整流程并推送；9:00–24:00 每小时做一次盘中巡检"
              "（有异动才推送）；每周日推送一次复盘。以下截图均来自这个真实运行过程。", 10.5, space=8)

    # ---------------- 图 1：企业微信推送 ----------------
    h1(doc, "图 1｜每日报告的手机推送（每天 17:30 自动送达）")
    para(doc, "这是系统每天自动推送到企业微信的日报摘要（原文为长图，为排版拆成上下两段）。", 10, space=2)
    wechat = imgs.get("image4.jpeg")
    if wechat:
        for i, part in enumerate(split_tall(wechat, parts=2, keep_ratio=0.62, tag="wechat")):
            pic(doc, part, 350, "" if i == 0 else "（接上图）")
    para(doc, "这张图证明了什么：", 10.5, bold=True, space=2)
    bullet(doc, "系统确实在无人值守地运行——这条消息是 17:31 自动送达的，不是手动发的")
    bullet(doc, "推送里同时给出「大盘判断 + 组合盈亏 + 分层持仓建议（清仓 1 / 减仓 3 / 持有 6）」")
    bullet(doc, "**模拟盘 A/B 双轨的净值与当日成交笔数**也在推送里（用于验证「AI 到底有没有加分」）")
    bullet(doc, "最后的「AI 研判」是模型基于当日事实写的解读，而不是复述规则")

    doc.add_page_break()

    # ---------------- 图 2：Word 报告 ----------------
    h1(doc, "图 2｜自动生成的 Word 报告（单品详细分析页）")
    para(doc, "报告固定七段结构：大盘环境 / 持仓整体评估 / 单品详细分析 / 建议汇总 / 风险提示 / "
              "AI 研判 / 模拟盘日报。这里是「单品详细分析」部分。", 10, space=2)
    report = imgs.get("image3.jpeg")
    if report:
        for i, part in enumerate(split_tall(report, parts=2, tag="report")):
            pic(doc, part, 330, "")
    para(doc, "这张图证明了什么：", 10.5, bold=True, space=2)
    bullet(doc, "每件饰品都有独立分析：趋势判定、6 个月位置百分位、支撑/压力位、流动性、量价")
    bullet(doc, "建议带**信心度与综合评分**（不是简单一句「持有」），并列出理由")
    bullet(doc, "表里能看到**「分件成本」**一栏——同一饰品多次买入价格不同时逐件记录"
                "（这是真实交易里才会遇到的问题）")
    bullet(doc, "还有「AI 研判」与规则结论并列，两者的分歧会被明确标出")

    doc.add_page_break()

    # ---------------- 图 3 / 4：网页问答 ----------------
    h1(doc, "图 3｜网页问答：一句话生成持仓诊断")
    para(doc, "在手机上问「分析一下我的持仓」，系统先调工具取真实数据，再按优先级分层给出结论。", 10, space=2)
    q1 = imgs.get("image1.png")
    if q1:
        pic(doc, q1, 460, "网页问答 · 持仓诊断（按「优先清仓 / 分批减仓 / 持有观望」分层）")
    para(doc, "这张图证明了什么：", 10.5, bold=True, space=2)
    bullet(doc, "回答里的行情数字来自工具实时取数，**不是模型凭记忆写的**")
    bullet(doc, "底部小字「回答基于真实行情与规则自动校验，每个数字都可展开核对」——"
                "点开能看到它调用了哪些工具、返回了什么原始数据")

    h1(doc, "图 4｜网页问答：单品技术分析（可以看到工具调用）")
    para(doc, "问「AK-47 安全网怎么样」，系统依次调用单品快照、技术分析、大盘环境三个工具后再回答。", 10, space=2)
    q2 = imgs.get("image2.png")
    if q2:
        pic(doc, q2, 460, "网页问答 · 单品分析（底部列出了本次调用的三个工具）")
    para(doc, "这张图证明了什么：", 10.5, bold=True, space=2)
    bullet(doc, "**底部能看到 `调用工具: get_item_snapshot、get_item_analysis、get_market_environment`**"
                "——这是 Function Calling 的实际过程")
    bullet(doc, "结论明确（「观望，不建议现在买」），并给出**若要参与的回踩位与止损位**")
    bullet(doc, "推测性内容被显式标注「（推测）」，不确定的地方不含糊")
    bullet(doc, "回答里所有数字都与工具返回一致（程序自动交叉校验）")

    # ---------------- 收尾 ----------------
    doc.add_page_break()
    h1(doc, "这些截图背后的技术要点")
    bullet(doc, "**防幻觉三层**：事实只能由 6 个工具取数 → 回答中的金额与百分比由程序与工具数据交叉校验 "
                "→ 每条结论附可核对的原始依据")
    bullet(doc, "**规则与 AI 分工**：数字、指标、仓位计算全部由代码完成；模型只负责解读与判断，"
                "并明确标注它与规则引擎的分歧")
    bullet(doc, "**分级推送**：每小时巡检 16 次，但只有紧急信号（买点/破位/急跌/深亏跨档）才立即推；"
                "温和波动静默记录，由 12:00 与 22:00 汇总——避免「消息太多用户不看，告警系统等于废掉」")
    bullet(doc, "**遵守平台真实规则**：T+7 锁定（买入 7 天内不可卖出）、卖出 1%、钱包提现另 1%")
    bullet(doc, "**可验证**：234 项单元测试全部离线可跑；A/B 双轨模拟盘用于回答「AI 是否真的加分」")

    para(doc, "")
    para(doc, "说明：截图中出现的持仓为本人真实持仓，盈亏为真实数据。"
              "系统的开发方式为「以 AI Agent 协作完成开发，本人负责需求定义、领域规则、"
              "产品决策与验收」。", 9.5, color=GRAY)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    print(f"✅ 已生成：{OUT}")
    print(f"   段落 {len(doc.paragraphs)} 个")


def main() -> int:
    if not SRC_DOCX.exists():
        print(f"❌ 找不到源文件：{SRC_DOCX}")
        return 1
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
