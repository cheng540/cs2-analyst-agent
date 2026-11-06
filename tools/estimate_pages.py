"""估算 docx 的排版高度（页数预估），用于在没有 Word/LibreOffice 时判断是否超过 1 页。

用法： python tools/estimate_pages.py 求职材料/06_刘乐_简历_AI Agent实习_修订版.docx

原理（粗略但足够判断「会不会溢出到第 2 页」）：
1. 可用宽度 = A4 宽 - 左右页边距；字符宽度：中文/全角按字号计，半角按 0.55 倍字号计；
2. 每个段落估算行数 = ceil(宽度占用 / 可用宽度)（最小 1 行）；
3. 行高 = 字号 * LINE_FACTOR * line_spacing，再加段落 space_before/after；
4. 累加得到总高度，除以可用高度即为页数。

⚠️ LINE_FACTOR 是实测校准值，不要随手改回 1.32：
   微软雅黑自身度量（hhea 2167/-536/0，unitsPerEm 2048）算出的自然行高只有 1.320 em，
   即 9pt 正文理论上每行 11.88pt。但用 Word / WPS 实际渲染本项目文档时，
   **实测行距稳定在 15.5pt（≈1.72 em），且不随字号变化**（10pt 条目标题同样是 15.5pt）。
   证据：references 见 data/pdf_probe.txt（两份不同时间的简历 PDF 都是 15.5pt）。
   历史上就是因为按 1.32 估算「应该能塞进一页」，实际导出却是 2 页。
   所以这里取 1.72（偏悲观），宁可在估算阶段就留出余量。

**最可靠的做法还是直接量**（本机已装有 Word + WPS，可 COM 调用）：
   powershell -ExecutionPolicy Bypass -File tools\measure_docx_pages.ps1 "文件.docx"
它返回 Word 与 WPS 各自统计的真实页数，并导出 PDF 供 probe_pdf_layout.py 分析行距。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from docx import Document
from docx.shared import Pt

A4_W, A4_H = 595.28, 841.89
LINE_FACTOR = 1.72          # 实测校准：9pt 正文在 Word/WPS 里每行 15.5pt（见模块文档字符串）
HALF_WIDTH = 0.55           # 半角字符宽度 / 字号


def char_width(ch: str, size: float) -> float:
    return size * (1.0 if ord(ch) > 0x2E80 else HALF_WIDTH)


def para_lines(text: str, size: float, avail: float) -> int:
    if not text.strip():
        return 1
    used, lines = 0.0, 1
    for ch in text:
        w = char_width(ch, size)
        if used + w > avail:
            lines += 1
            used = w
        else:
            used += w
    return lines


def estimate(path: Path) -> tuple[float, list[str]]:
    doc = Document(str(path))
    sec = doc.sections[0]
    avail_w = A4_W - sec.left_margin.pt - sec.right_margin.pt
    avail_h = A4_H - sec.top_margin.pt - sec.bottom_margin.pt

    total = 0.0
    rows: list[str] = []
    for p in doc.paragraphs:
        size = 9.0
        for r in p.runs:
            if r.font.size is not None:
                size = max(size, r.font.size.pt)
        text = p.text
        indent = 0.0
        if p.paragraph_format.left_indent is not None:
            indent = p.paragraph_format.left_indent.pt
        ls = 1.0
        if p.paragraph_format.line_spacing:
            ls = float(p.paragraph_format.line_spacing)
        n = para_lines(text, size, avail_w - indent)
        line_h = size * LINE_FACTOR * ls
        sb = p.paragraph_format.space_before.pt if p.paragraph_format.space_before else 0.0
        sa = p.paragraph_format.space_after.pt if p.paragraph_format.space_after else 0.0
        h = n * line_h + sb + sa
        total += h
        rows.append(f"{n:>2}行 {size:>4.1f}pt h={h:6.1f} | {text[:44]}")
    return total / avail_h, rows


def main() -> int:
    args = sys.argv[1:]
    if not args:
        base = Path(__file__).resolve().parent.parent / "求职材料"
        files = [base / "06_刘乐_简历_AI Agent实习_修订版.docx"]
    else:
        files = [Path(a) for a in args]
    for f in files:
        pages, rows = estimate(f)
        print(f"=== {f.name}")
        for r in rows:
            print("   " + r)
        print(f"--> 估算高度 {pages:.3f} 页   {'[OK] 单页' if pages <= 0.97 else '[WARN] 可能溢出'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
