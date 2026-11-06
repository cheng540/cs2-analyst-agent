"""把求职材料里的 Markdown 转成 Word（.docx）。

为什么需要：用户不习惯看 md。所以以 .md 为「源文件」（方便我维护），
需要看的时候一键转成排版好的 Word。

支持转换（够用于本项目所有材料）：
    标题 # / ## / ###、无序列表 -、有序列表 1.、待办 - [ ]、
    表格 | a | b |、代码块 ```、引用 >、分隔线 ---、行内 **加粗** 与 `等宽`

用法：
    python tools/md_to_docx.py                    # 转换 求职材料/ 下所有 .md
    python tools/md_to_docx.py 求职材料/11_Boss直聘资料（可直接粘贴）.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

FONT = "微软雅黑"
MONO = "Consolas"
BLUE = RGBColor(0x1F, 0x4E, 0x79)
GRAY = RGBColor(0x59, 0x59, 0x59)
RED = RGBColor(0xC0, 0x00, 0x00)

BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
ORDER_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _set_font(run, size=10.5, bold=False, color=None, mono=False):
    run.font.name = MONO if mono else FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _shade(paragraph, fill: str):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    paragraph._p.get_or_add_pPr().append(shd)


def _add_inline(paragraph, text: str, size=10.5, base_bold=False, color=None):
    """处理行内的 **加粗** 与 `等宽`。"""
    for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            _set_font(paragraph.add_run(part[2:-2]), size, True, color)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            _set_font(paragraph.add_run(part[1:-1]), size - 0.5, base_bold, None, mono=True)
        else:
            _set_font(paragraph.add_run(part), size, base_bold, color)


def _heading(doc, text, level):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    if level == 1:
        pf.space_before = Pt(12)
        pf.space_after = Pt(5)
        _shade(p, "2F5496")
        _add_inline(p, text, 13.5, base_bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
    elif level == 2:
        pf.space_before = Pt(9)
        pf.space_after = Pt(3)
        _add_inline(p, text, 11.5, base_bold=True, color=BLUE)
    else:
        pf.space_before = Pt(6)
        pf.space_after = Pt(2)
        _add_inline(p, text, 10.5, base_bold=True)
    return p


def _table(doc, rows):
    header, body = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for j, htxt in enumerate(header):
        cell = t.rows[0].cells[j]
        cell.text = ""
        _add_inline(cell.paragraphs[0], htxt, 9.5, base_bold=True)
    for row in body:
        cells = t.add_row().cells
        for j in range(len(header)):
            cells[j].text = ""
            _add_inline(cells[j].paragraphs[0], row[j] if j < len(row) else "", 9.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ") and "-" in s


def convert(md_path: Path, out_path: Path) -> int:
    """读 .md 文件 → 生成 .docx，返回段落数。"""
    return convert_lines(md_path.read_text(encoding="utf-8").splitlines(), out_path)


def convert_text(md_text: str, out_path: Path) -> int:
    """直接把一段 Markdown 文本渲染成 .docx。

    给「先有文本、再想要一份 Word」的场景复用（周报、盘中概览）——
    这样它们的排版逻辑和求职材料那一套完全一致，不用重复实现一遍。
    """
    return convert_lines(md_text.splitlines(), out_path)


def convert_lines(lines: list[str], out_path: Path) -> int:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    style.paragraph_format.space_after = Pt(3)
    for s in doc.sections:
        s.top_margin = Pt(46)
        s.bottom_margin = Pt(46)
        s.left_margin = Pt(54)
        s.right_margin = Pt(54)

    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            if buf:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(6)
                p.paragraph_format.left_indent = Pt(10)
                _shade(p, "F4F5F7")
                for k, bl in enumerate(buf):
                    if k:
                        p.add_run().add_break()
                    _set_font(p.add_run(bl), 9.5, mono=True)
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                if not _is_table_sep(lines[i]):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                _table(doc, rows)
            continue

        # 标题
        m = HEAD_RE.match(stripped)
        if m:
            _heading(doc, m.group(2).strip(), len(m.group(1)))
            i += 1
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(6)
            _set_font(p.add_run("─" * 34), 8, color=GRAY)
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(16)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(6)
            _shade(p, "F7F9FC")
            for k, bl in enumerate([b for b in buf if b]):
                if k:
                    p.add_run().add_break()
                _add_inline(p, bl, 10, color=BLUE)
            continue

        # 待办 / 无序列表
        m = BULLET_RE.match(line)
        if m:
            indent, text = len(m.group(1)), m.group(2).strip()
            todo = text.startswith("[ ]") or text.startswith("[x]")
            if todo:
                text = ("☐ " if text.startswith("[ ]") else "☑ ") + text[3:].strip()
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(2)
            if indent >= 2:
                p.paragraph_format.left_indent = Pt(18 + indent * 4)
            _add_inline(p, text, 10)
            i += 1
            continue

        # 有序列表
        m = ORDER_RE.match(line)
        if m:
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.space_after = Pt(2)
            _add_inline(p, m.group(3).strip(), 10)
            i += 1
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(4)
        _add_inline(p, stripped, 10.5)
        i += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return len(doc.paragraphs)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    mat = root / "求职材料"
    if len(sys.argv) > 1:
        targets = [Path(a) for a in sys.argv[1:]]
    else:
        # md 源文件放在子文件夹里，转换结果输出到上一层（用户只看 Word）
        targets = sorted(mat.glob("*.md")) + sorted((mat / "md源文件").glob("*.md"))

    if not targets:
        print("没有找到要转换的 .md 文件")
        return 1

    failed: list[str] = []
    for md in targets:
        if not md.exists():
            print(f"跳过（不存在）：{md}")
            continue
        # md源文件/ 里的输出到上一层
        out = (md.parent.parent / f"{md.stem}.docx") if md.parent.name == "md源文件" \
            else md.with_suffix(".docx")
        try:
            count = convert(md, out)
        except PermissionError:
            # 用户正用 Word/WPS 打开着这个 docx（Windows 会锁文件）。
            # 必须单文件容错：早期版本这里直接抛异常，导致排在后面的文件**一个都没转**。
            failed.append(out.name)
            print(f"[跳过·文件被占用] {out.name} —— 请先关闭 Word/WPS 里打开的它，然后重跑本命令")
            continue
        print(f"[OK] {md.name}  ->  {out.name}（{count} 段）")

    if failed:
        print(f"\n有 {len(failed)} 个文件没生成（被占用）：{'、'.join(failed)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
