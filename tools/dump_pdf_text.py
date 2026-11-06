"""按阅读顺序导出 PDF 全文（用于和 docx 内容做差异比对，避免重排时误删用户新增内容）。

用法： python tools/dump_pdf_text.py "求职材料/刘乐简历_AI Agent实习.pdf"
输出： data/pdf_text.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyPDF2 import PdfReader

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def dump(path: Path, sink: list[str]) -> None:
    reader = PdfReader(str(path))
    sink.append(f"=== {path.name}  页数={len(reader.pages)}")
    for pi, page in enumerate(reader.pages, 1):
        h = float(page.mediabox.height)
        segs: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            t = (text or "")
            if not t.strip():
                return
            segs.append((round(tm[5], 1), round(tm[4], 1), t))

        page.extract_text(visitor_text=visitor)
        # 判断坐标系方向：若"姓名"出现在较小 y 值处，说明 y 轴向下
        ys = [s[0] for s in segs]
        downward = False
        if ys:
            first_big = None
            for y, x, t in segs:
                if len(t.strip()) <= 4 and t.strip() in ("刘乐", "刘  乐"):
                    first_big = y
                    break
            if first_big is not None and first_big <= min(ys) + (max(ys) - min(ys)) * 0.3:
                downward = True
        segs.sort(key=lambda s: (s[0] if downward else -s[0], s[1]))
        sink.append(f"\n--- 第 {pi} 页（y 轴{'向下' if downward else '向上'}，按阅读顺序）")
        cur_y, buf = None, []
        for y, x, t in segs:
            if cur_y is None or abs(y - cur_y) > 3:
                if buf:
                    sink.append("".join(buf))
                buf, cur_y = [t], y
            else:
                buf.append(t)
        if buf:
            sink.append("".join(buf))
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    sink: list[str] = []
    for a in sys.argv[1:]:
        dump(Path(a), sink)
    out = Path(__file__).resolve().parent.parent / "data" / "pdf_text.txt"
    out.write_text("\n".join(sink), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
