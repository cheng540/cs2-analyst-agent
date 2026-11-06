"""把「用户已确认过 7 天锁定期」写进持仓文件（buy_date 缺失时的兜底开关）。

背景（2026-09-30 用户原话）：
    「我手上的这些的买入日期你不用管，早就过了七天，后面再有新的我买了的饰品的话，
      我会再告诉你的」
    → 所以不给逐件补 buy_date，而是给现有持仓打上 `unlocked: true`；
      **以后新买的必须填真实 buy_date**（那样系统能算出准确的可售日）。

用法：
    python tools/mark_unlocked.py            # 预览
    python tools/mark_unlocked.py --apply    # 写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = ap.parse_args()

    cfg = load_config()
    path = cfg.inventory_path()
    items = json.loads(path.read_text(encoding="utf-8"))

    changed = 0
    for it in items:
        if it.get("unlocked") is True:
            continue
        if str(it.get("buy_date") or "").strip():
            # 有真实买入日期的不用打标记，系统本来就能算
            print(f"跳过（已有买入日期 {it['buy_date']}）：{it.get('market_hash_name')}")
            continue
        print(f"标记已过锁定期：{it.get('market_hash_name')}（{it.get('quantity')} 件）")
        changed += 1
        it["unlocked"] = True

    print(f"\n共 {changed} 条待标记；持仓合计 {len(items)} 条")
    if not changed:
        print("（无需改动）")
        return 0
    if not args.apply:
        print("\n[预览] 未写入。确认后加 --apply")
        return 0
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
