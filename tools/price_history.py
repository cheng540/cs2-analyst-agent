"""看某个饰品的历史价格轨迹（扫本地所有快照），用于「这笔卖得对不对」的事后复盘。

用法：
    python tools/price_history.py "PP-Bizon | Water Sigil"          # 英文名片段即可
    python tools/price_history.py "水纹之印"                          # 中文名也可以（自动查 name_map）
    python tools/price_history.py "AWP | Black Nile" --platforms YOUPIN,BUFF

为什么要它：
    记录**补录的**历史卖出（用户隔几天才说）时，`tools/sell_item.py` 默认拿"最新价"当
    成交时基准价，会把滑点算错。这个脚本把各日快照的价格列出来，好挑出成交当日的真实基准。

扫描位置（本地同步目录，缺哪个就跳过哪个）：
    data/snapshots/*.json
    data/cloud_root/snapshots/*.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _date_of(path: Path, payload) -> str:
    if isinstance(payload, dict) and payload.get("_date"):
        return str(payload["_date"])
    return path.name[:10]


def load_days() -> list[tuple[str, dict]]:
    """返回 [(日期, 原始记录)]，按日期升序，同日期保留后出现的。"""
    days: dict[str, dict] = {}
    for folder in (ROOT / "data" / "snapshots", ROOT / "data" / "cloud_root" / "snapshots"):
        if not folder.exists():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            date = _date_of(f, payload)
            days.setdefault(date, {})[f.name] = payload
    return sorted(days.items())


def find_item(payload: dict, needle: str) -> dict | None:
    """在快照里找条目（键是英文名；值可能是 list[平台] 或 {items:[...]}）。"""
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if needle.lower() in str(key).lower():
            return {"name": key, "value": value}
    return None


def extract(entry: dict, platforms: list[str]) -> str:
    value = entry["value"]
    rows: list[str] = []
    if isinstance(value, list):                       # 价格快照：平台数组
        for row in value:
            plat = str(row.get("platform") or "")
            if platforms and plat not in platforms:
                continue
            price = row.get("sellPrice") or 0
            if not price:
                continue
            rows.append(f"{plat} ¥{float(price):,.2f}(在售{row.get('sellCount') or 0})")
    elif isinstance(value, dict) and value.get("现价"):  # 分析快照
        rows.append(f"现价 ¥{float(value['现价']):,.2f}")
        for k in ("位置", "趋势", "系统建议", "盈亏%"):
            if value.get(k) not in (None, ""):
                rows.append(f"{k}={value[k]}")
    return " | ".join(rows) or "（无价格）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="饰品名（英文片段或中文）")
    ap.add_argument("--platforms", default="YOUPIN,BUFF,C5,HALOSKINS",
                    help="只看这些平台（逗号分隔；留空=全部）")
    args = ap.parse_args()

    platforms = [p.strip().upper() for p in args.platforms.split(",") if p.strip()]
    needle = args.query.strip()

    # 中文名 → 英文名：借用系统的映射（config/name_overrides.json + 缓存的 name_map）
    if any(ord(c) > 0x2E80 for c in needle):
        try:
            from cs2analyst.chat.tools import ToolBox
            from cs2analyst.config import load_config
            box = ToolBox(load_config())
            en, _cands = box.resolve_name(needle)
            if en:
                print(f"[中文名解析] {needle} → {en}")
                needle = en
        except Exception as exc:  # noqa: BLE001
            print(f"[中文名解析失败，改用原文匹配] {type(exc).__name__}: {exc}")

    days = load_days()
    if not days:
        print("没找到任何快照文件")
        return 1

    print(f"\n查询：{needle}")
    print(f"平台：{', '.join(platforms) or '全部'}\n")
    hit = 0
    for date, payloads in days:
        for fname, payload in payloads.items():
            entry = find_item(payload, needle)
            if not entry:
                continue
            hit += 1
            print(f"{date}  [{fname}]  {entry['name']}")
            print(f"    {extract(entry, platforms)}")
    print(f"\n共命中 {hit} 条；快照日期：{', '.join(d for d, _ in days)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
