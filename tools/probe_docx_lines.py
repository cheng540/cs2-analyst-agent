"""诊断 docx 的「行距来源」：文档网格 / 段落行距 / 字体自身行高。

用法： python tools/probe_docx_lines.py "求职材料/06_刘乐_简历_AI Agent实习_修订版.docx"

为什么需要它：
    实测用户导出的 PDF 里，9pt 正文的**实际行距是 15.5pt**（自然行高应约 11.9pt），
    导致内容被撑高 30%、溢出到第 2 页。本脚本从三个可能来源定位原因：
      1) sectPr 的 <w:docGrid>（Word/WPS 的文档网格，会把每行吸附到固定行距）
      2) 段落自身的 <w:spacing w:line=... w:lineRule=...>
      3) 字体 msyh.ttc 的 hhea/OS2 度量（自然行高 = (ascender+|descender|+lineGap)/unitsPerEm）
"""
from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def font_metrics(path: Path) -> None:
    """解析 TTC/TTF 的 head + hhea + OS/2 度量，算出自然行高倍率。"""
    data = path.read_bytes()
    if data[:4] == b"ttcf":
        num = struct.unpack(">I", data[8:12])[0]
        off = struct.unpack(">I", data[12:16])[0]
        print(f"   字体 {path.name}：TTC 集合，含 {num} 个字体，取第 1 个 @{off}")
    else:
        off = 0
    num_tables = struct.unpack(">H", data[off + 4:off + 6])[0]
    tables: dict[str, tuple[int, int]] = {}
    for i in range(num_tables):
        rec = off + 12 + i * 16
        tag = data[rec:rec + 4].decode("latin-1")
        o, ln = struct.unpack(">II", data[rec + 8:rec + 16])
        tables[tag] = (o, ln)

    ho, _ = tables["head"]
    units = struct.unpack(">H", data[ho + 18:ho + 20])[0]
    ao, _ = tables["hhea"]
    asc, desc, gap = struct.unpack(">hhh", data[ao + 4:ao + 10])
    natural = (asc + abs(desc) + gap) / units
    print(f"   unitsPerEm={units}  hhea asc={asc} desc={desc} gap={gap}")
    print(f"   >>> 自然行高 = {natural:.3f} em  → 9pt 文本自然行距 ≈ {9 * natural:.2f}pt")
    if "OS/2" in tables:
        oo, _ = tables["OS/2"]
        typo_asc, typo_desc, typo_gap = struct.unpack(">hhh", data[oo + 68:oo + 74])
        win_asc, win_desc = struct.unpack(">HH", data[oo + 74:oo + 78])
        print(f"   OS/2 typo {typo_asc}/{typo_desc}/{typo_gap} → "
              f"{(typo_asc + abs(typo_desc) + typo_gap) / units:.3f} em；"
              f"win {win_asc}/{win_desc} → {(win_asc + win_desc) / units:.3f} em")


def probe_docx(path: Path) -> None:
    print(f"=== {path.name}")
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        styles = z.read("word/styles.xml").decode("utf-8")

    import re
    sect = re.search(r"<w:sectPr.*?</w:sectPr>", doc, re.S)
    if sect:
        grid = re.search(r"<w:docGrid[^/]*/>", sect.group(0))
        print(f"   [1] sectPr/docGrid: {grid.group(0) if grid else '（无 docGrid 元素）'}")
        pgsz = re.search(r"<w:pgSz[^/]*/>", sect.group(0))
        mar = re.search(r"<w:pgMar[^/]*/>", sect.group(0))
        print(f"       {pgsz.group(0) if pgsz else ''}")
        print(f"       {mar.group(0) if mar else ''}")

    dd = re.search(r"<w:docDefaults>.*?</w:docDefaults>", styles, re.S)
    if dd:
        sp = re.search(r"<w:spacing[^/]*/>", dd.group(0))
        print(f"   [2] docDefaults/spacing: {sp.group(0) if sp else '（无）'}")
    for name in ("Normal", "ListBullet", "List Bullet"):
        m = re.search(r'<w:style [^>]*w:styleId="' + name + r'".*?</w:style>', styles, re.S)
        if m:
            sp = re.search(r"<w:spacing[^/]*/>", m.group(0))
            sng = "snapToGrid" in m.group(0)
            print(f"   [2] style {name}: spacing={sp.group(0) if sp else '（无）'} snapToGrid={sng}")

    snaps = doc.count("snapToGrid")
    lines = re.findall(r'w:line="(\d+)" w:lineRule="(\w+)"', doc)
    print(f"   [3] 段落级 snapToGrid 出现次数={snaps}；段落级 w:line 设置={lines[:6]}")
    print(f"   字体自然行高：")
    font_metrics(Path(r"C:\Windows\Fonts\msyh.ttc"))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for a in sys.argv[1:]:
        p = Path(a)
        if p.suffix.lower() == ".docx":
            probe_docx(p)
        else:
            font_metrics(p)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
