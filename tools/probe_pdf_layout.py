"""测量 PDF 的排版几何：每页首/末基线、行距、字号分布、页边距。

用法： python tools/probe_pdf_layout.py "求职材料/刘乐简历_AI Agent实习.pdf"

用途：当无法在本机渲染 docx 时，用真实 PDF 反推「每行实际行距」与「页面可用高度」，
      再用它校准 estimate_pages.py 的估算（把经验系数换成实测值）。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from PyPDF2 import PdfReader

# Windows 控制台是 GBK，PDF 里可能含私有区字符（项目符号），统一按 UTF-8 + 替换输出，
# 同时把完整报告写入文件，便于用编辑器稳定查看。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def probe(path: Path, sink: list[str]) -> None:
    def say(msg: str = "") -> None:
        print(msg)
        sink.append(msg)

    reader = PdfReader(str(path))
    say(f"=== {path.name}  页数={len(reader.pages)}")
    pitches_all: list[float] = []
    sizes_all: Counter = Counter()
    for pi, page in enumerate(reader.pages, 1):
        box = page.mediabox
        h, w = float(box.height), float(box.width)
        lines: list[tuple[float, str, float]] = []   # (y, text, size)

        def visitor(text, cm, tm, font_dict, font_size, _lines=lines):
            t = (text or "").strip()
            if not t:
                return
            y = tm[5]
            size = round(float(font_size or 0), 1)
            _lines.append((y, t, size))

        page.extract_text(visitor_text=visitor)
        # 同一条基线可能被拆成多段，按 y 合并
        merged: dict[float, list[str]] = {}
        size_of: dict[float, float] = {}
        for y, t, s in lines:
            key = round(y, 1)
            merged.setdefault(key, []).append(t)
            size_of[key] = max(size_of.get(key, 0.0), s)
        ys = sorted(merged.keys(), reverse=True)
        say(f"\n-- 第 {pi} 页  尺寸 {w:.0f}x{h:.0f}pt  文本行数={len(ys)}")
        if not ys:
            continue
        say(f"   首行基线 y={ys[0]:.1f}（距页顶 {h - ys[0]:.1f}pt）  "
            f"末行基线 y={ys[-1]:.1f}（距页底 {ys[-1]:.1f}pt）")
        pitches = [round(ys[i] - ys[i + 1], 1) for i in range(len(ys) - 1)]
        normal = [p for p in pitches if 6 <= p <= 30]
        if normal:
            cnt = Counter(normal)
            top = cnt.most_common(3)
            say(f"   行距实测：最多见 {top}  中位数={sorted(normal)[len(normal)//2]:.1f}pt")
            pitches_all.extend(normal)
        s_cnt = Counter(size_of.values())
        say(f"   字号分布（原始 Tf 值）：{s_cnt.most_common()}")
        sizes_all.update(s_cnt)
        for y in ys[:2] + ys[-2:]:
            say(f"     y={y:7.1f} size={size_of[y]:>5.1f} | {''.join(merged[y])[:60]}")
    if pitches_all:
        cnt = Counter(pitches_all)
        med = sorted(pitches_all)[len(pitches_all) // 2]
        say(f"\n>>> 全局：行距中位数 {med:.1f}pt，最多见 {cnt.most_common(4)}")
        say(f">>> 全局字号（原始 Tf 值）：{sizes_all.most_common()}")
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    sink: list[str] = []
    for a in sys.argv[1:]:
        probe(Path(a), sink)
    out = Path(__file__).resolve().parent.parent / "data" / "pdf_probe.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(sink), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
