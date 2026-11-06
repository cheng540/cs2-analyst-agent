"""单件饰品 / 持仓的**原始数据探针**：直接调工具函数打印 JSON，不经过大模型。

用法：
    python tools/probe_item.py --item "Lieutenant Rex Krikey | SEAL Frogman"
    python tools/probe_item.py --item "海豹蛙人" --market        # 附带大盘环境
    python tools/probe_item.py --portfolio                       # 只看持仓（含系统给出的建议）

为什么要它：
    网页问答 / `tools/ask.py` 的答案是大模型写的，且 evidence 只截前 400 字，
    判断「该不该卖」时我需要**完整、未经改写的原始数字**（现价、成本、均线、位置百分位、
    支撑压力、流动性、系统自己的建议），自己算一遍再下结论，避免"转述失真"。

注意：
    - 会真实调用 SteamDT 接口（批量取价有 1 次/分钟限制，同分钟重复调用返回 errorCode=4005）；
    - 不写任何文件，只 print。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.chat.tools import ToolBox  # noqa: E402
from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402


def dump(title: str, obj) -> None:
    print(f"\n{'=' * 70}\n## {title}\n{'=' * 70}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--item", default="", help="饰品名称（中文或英文全名）")
    ap.add_argument("--portfolio", action="store_true", help="打印全部持仓与系统建议")
    ap.add_argument("--market", action="store_true", help="打印大盘环境")
    ap.add_argument("--intel", action="store_true", help="打印最新情报")
    ap.add_argument("--kline", type=int, default=0, metavar="N",
                    help="附带最近 N 根日K的收盘价（用于核对「某天卖得贵还是便宜」）")
    ap.add_argument("--valuation", action="store_true",
                    help="按当前持仓文件实时估值（1 次批量取价；不读分析快照，所以不会拿到过期/已卖出的条目）")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir())
    box = ToolBox(cfg)

    if args.market or args.item or args.portfolio:
        try:
            dump("大盘环境 get_market_environment", box.get_market_environment())
        except Exception as exc:  # noqa: BLE001
            print(f"[大盘环境失败] {type(exc).__name__}: {exc}")

    if args.portfolio:
        try:
            dump("持仓 get_my_portfolio", box.get_my_portfolio())
        except Exception as exc:  # noqa: BLE001
            print(f"[持仓失败] {type(exc).__name__}: {exc}")

    if args.item:
        try:
            dump("实时快照 get_item_snapshot", box.get_item_snapshot(args.item))
        except Exception as exc:  # noqa: BLE001
            print(f"[快照失败] {type(exc).__name__}: {exc}")
        try:
            dump("技术分析 get_item_analysis", box.get_item_analysis(args.item))
        except Exception as exc:  # noqa: BLE001
            print(f"[分析失败] {type(exc).__name__}: {exc}")

    if args.intel:
        try:
            dump("最新情报 get_latest_intel", box.get_latest_intel())
        except Exception as exc:  # noqa: BLE001
            print(f"[情报失败] {type(exc).__name__}: {exc}")

    if args.kline and args.item:
        try:
            from cs2analyst.datasource.steamdt import SteamDTClient
            en, _c = box.resolve_name(args.item)
            en = en or args.item
            df = SteamDTClient(cfg.steamdt).item_kline(en, ktype=2)
            tail = df.tail(args.kline)
            print(f"\n{'=' * 70}\n## 最近 {len(tail)} 根日K（{en}）\n{'=' * 70}")
            for _, row in tail.iterrows():
                ts = row.get("ts")
                day = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
                print(f"  {day}  收盘 ¥{float(row.get('close') or 0):>8,.2f}"
                      f"  高 ¥{float(row.get('high') or 0):>8,.2f}"
                      f"  低 ¥{float(row.get('low') or 0):>8,.2f}")
        except Exception as exc:  # noqa: BLE001
            print(f"[日K失败] {type(exc).__name__}: {exc}")

    if args.valuation:
        # 为什么要单独做这个：get_my_portfolio 读的是**最新分析快照**，快照可能是几天前的，
        # 而且会包含"当时还持有、现在已经卖掉"的条目 → 组合总数会被高估。
        # 这里直接按当前持仓文件 + 一次批量取价现算，用于核对真实敞口。
        try:
            from cs2analyst.alerts.checker import pick_price
            from cs2analyst.datasource.inventory import load_inventory
            from cs2analyst.datasource.steamdt import SteamDTClient

            holdings = load_inventory(cfg.inventory_path())
            rows_map = SteamDTClient(cfg.steamdt).batch_price([h.market_hash_name for h in holdings])
            print(f"\n{'=' * 70}\n## 当前持仓实时估值（{len(holdings)} 条，基准=悠悠有品）\n{'=' * 70}")
            tot_cost = tot_val = 0.0
            warn: list[str] = []
            for h in holdings:
                price, basis, _by = pick_price(rows_map.get(h.market_hash_name) or [])
                if price is None:
                    warn.append(h.market_hash_name)
                cost = sum(h.lot_prices)
                val = float(price or 0) * max(int(h.quantity), 1)
                tot_cost += cost
                tot_val += val
                pnl = (val / cost - 1) * 100 if cost else 0.0
                print(f"  {h.market_hash_name[:40]:<42}{h.quantity:>3} 件  成本 ¥{cost:>9,.2f}"
                      f"  现价 ¥{float(price or 0):>8,.2f}  市值 ¥{val:>9,.2f}  {pnl:>7.1f}%"
                      f"  [{basis}]")
            print("  " + "-" * 96)
            print(f"  合计：{len(holdings)} 条 / {sum(int(h.quantity) for h in holdings)} 件"
                  f"   总成本 ¥{tot_cost:,.2f}   总市值 ¥{tot_val:,.2f}"
                  f"   浮动盈亏 ¥{tot_val - tot_cost:+,.2f}"
                  f"（{(tot_val / tot_cost - 1) * 100 if tot_cost else 0:+.2f}%）")
            if warn:
                print(f"  ⚠ 取不到价的：{'、'.join(warn)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[估值失败] {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
