"""记录一笔真实卖出：更新持仓（含分件成本）+ 追加已实现交易记录。

为什么需要它：
- 持仓文件只反映「现在还有什么」，卖掉的东西一旦删掉就再也查不到成交价；
- 没有已实现记录，就没法复盘「当时该不该卖」（卖掉后价格是继续跌还是涨回去了）。
  → 所以卖出时同时做两件事：改持仓 + 写入 data/real_trades.json。

分件成本的处理（这是本工具最容易出错的地方）：
- 同一饰品多次买入、成本不同时，卖出要指定卖的是哪一件（`--lot`），默认按先进先出（记录顺序）；
- **卖完后剩下那件的成本价会重算**——否则均价还带着已卖出那件，盈亏就永久算错了。

用法：
    python tools/sell_item.py --name 银白石英 --qty 1 --price 3.65
    python tools/sell_item.py --name 白骨 --qty 2 --price 2.70 --date 2026-09-25
    python tools/sell_item.py --name 法玛斯 --qty 1 --lot 234        # 指定卖 ¥234 那件
    python tools/sell_item.py --name 银白石英 --qty 1 --dry-run      # 只看不动
    python tools/sell_item.py --list                                 # 看已实现交易
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import AppConfig, load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402

TRADES_REL = "real_trades.json"


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------
def normalize(text: str) -> str:
    """名称归一化：去掉空格、竖线、括号等，便于模糊匹配。"""
    return re.sub(r"[\s|｜()（）\-_·、,，.。★]", "", str(text or "")).lower()


# 中文名取到行尾/分号为止。注意**不能**在括号处截断：
# 「SG 553 (纪念品) | 白骨 (崭新出厂)」里真正有辨识度的「白骨」在括号后面。
CN_RE = re.compile(r"中文名：([^；;\n]+)")


def cn_of(note: str) -> str:
    m = CN_RE.search(str(note or ""))
    return m.group(1).strip() if m else ""


def match_holding(items: list[dict], query: str) -> dict | None:
    """按英文名 / 中文名（备注里的「中文名：xxx」）/ 关键词匹配持仓条目。"""
    q = normalize(query)
    if not q:
        return None
    exact, partial = [], []
    for it in items:
        name = str(it.get("market_hash_name") or "")
        cn = cn_of(it.get("note"))
        hay = [normalize(name), normalize(cn)]
        if q in hay:
            exact.append(it)
        elif any(q in h or h in q for h in hay if h):
            partial.append(it)
    if exact:
        return exact[0]
    if partial:
        # 多个候选时优先还没卖完的；仍并列则取第一条
        return partial[0]
    return None


def latest_price(cfg: AppConfig, name: str) -> float | None:
    """从最近的分析快照 / 日内取样里取现价（用于 --price 省略时的估算）。"""
    try:
        snaps = sorted((cfg.data_dir() / "analysis").glob("*.json"))
        for f in reversed(snaps):
            data = json.loads(f.read_text(encoding="utf-8"))
            for it in (data.get("items") or []):
                if it.get("饰品") == name and it.get("现价"):
                    return float(it["现价"])
    except Exception:
        pass
    try:
        files = sorted((cfg.data_dir() / "intraday").glob("*.jsonl"))
        if files:
            for line in reversed(files[-1].read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                rec = json.loads(line)
                val = (rec.get("prices") or {}).get(name)
                if val:
                    return float(val)
    except Exception:
        pass
    return None


def compute_sale(item: dict, qty: int, price: float, fee_rate: float,
                 lot: str | None) -> dict:
    """算出这笔卖出的成本、手续费与已实现盈亏（含分件成本处理）。"""
    lots = [float(x) for x in (item.get("lots") or []) if str(x).strip()]
    total_qty = int(item.get("quantity") or 1)
    avg = float(item.get("buy_price") or 0)
    if len(lots) != total_qty:          # 没有分件记录或数量对不上 → 按均价
        lots = [round(avg, 2)] * total_qty
    if qty > total_qty:
        raise SystemExit(f"❌ 卖出数量 {qty} 超过持仓 {total_qty} 件")

    # 选卖哪几件：--lot 指定成本价，否则先进先出
    idx = list(range(len(lots)))
    if lot:
        try:
            target = float(lot)
        except ValueError:
            raise SystemExit(f"❌ --lot 需要填成本价（如 --lot 234）：{lot}")
        if target not in lots:
            raise SystemExit(f"❌ 该持仓没有 ¥{target} 这一件，可选：{lots}")
        idx = [lots.index(target)]
        if qty > 1:
            idx += [i for i in range(len(lots)) if i != idx[0]][:qty - 1]
    sold_idx = idx[:qty]
    sold_lots = [lots[i] for i in sold_idx]
    remain_lots = [v for i, v in enumerate(lots) if i not in sold_idx]

    cost = round(sum(sold_lots), 2)
    gross = round(price * qty, 2)
    fee = round(gross * fee_rate, 2)
    net = round(gross - fee, 2)
    return {
        "qty": qty, "price": round(price, 2), "gross": gross, "fee": fee, "net": net,
        "cost": cost, "sold_lots": sold_lots, "remain_lots": remain_lots,
        "pnl": round(net - cost, 2),
        "pnl_pct": round((net / cost - 1) * 100, 2) if cost else 0.0,
        "new_avg": round(sum(remain_lots) / len(remain_lots), 2) if remain_lots else 0.0,
    }


def load_trades(cfg: AppConfig) -> list[dict]:
    path = cfg.data_dir() / TRADES_REL
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_trades(cfg: AppConfig, trades: list[dict]) -> Path:
    path = cfg.data_dir() / TRADES_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="记录一笔真实卖出（更新持仓 + 已实现账本）")
    ap.add_argument("--name", help="饰品名（英文名或中文名，支持模糊）")
    ap.add_argument("--qty", type=int, default=1, help="卖出件数（默认 1）")
    ap.add_argument("--price", type=float, help="成交单价（省略则用最近快照价估算）")
    ap.add_argument("--lot", help="指定卖出哪一件的成本价（分件成本不同时用）")
    ap.add_argument("--date", help="成交日期（默认今天）")
    ap.add_argument("--market-price", type=float,
                    help="成交当时的市价基准（补录历史成交时必填：本地快照只保留最近几天，"
                         "用最新价当基准会把滑点算错；可先用 tools/price_history.py 或日K查出当天价）")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--dry-run", action="store_true", help="只演示，不写入")
    ap.add_argument("--list", action="store_true", help="列出已实现交易记录")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir())

    if args.list:
        trades = load_trades(cfg)
        if not trades:
            print("暂无已实现交易记录。")
            return 0
        total = sum(t.get("pnl", 0) for t in trades)
        print(f"已实现交易 {len(trades)} 笔，累计已实现盈亏 ¥{total:+,.2f}\n")
        for t in trades:
            print(f"  {t['date']}  {t['name_cn'] or t['name']}  {t['qty']} 件  "
                  f"成交 ¥{t['price']:,.2f}（净到手 ¥{t['net']:,.2f}）"
                  f"  成本 ¥{t['cost']:,.2f}  已实现 {t['pnl']:+,.2f}（{t['pnl_pct']:+.1f}%）")
            if t.get("note"):
                print(f"        备注：{t['note']}")
        return 0

    if not args.name:
        ap.error("需要 --name（或使用 --list）")

    inv_path = cfg.inventory_path()
    items = json.loads(inv_path.read_text(encoding="utf-8"))
    item = match_holding(items, args.name)
    if not item:
        print(f"❌ 持仓里找不到「{args.name}」。当前持仓：")
        for it in items:
            print(f"   - {it.get('market_hash_name')}（{it.get('quantity')} 件）")
        return 2

    price = args.price if args.price is not None else latest_price(cfg, item["market_hash_name"])
    if not price:
        print("❌ 未提供 --price，且快照里也没有该饰品的现价，请手动指定")
        return 2

    # 成交价 vs 报告里的"现价"：这是校准「市值是否虚高」的真实数据
    # 注意：补录历史成交时 --market-price 必填，否则会拿"今天"的价当"成交当天"的基准，滑点失真。
    market_price = args.market_price if args.market_price is not None \
        else latest_price(cfg, item["market_hash_name"])
    slippage = None
    if market_price and market_price > 0:
        slippage = round((price / market_price - 1) * 100, 2)

    fee_rate = float(cfg.sim.get("fee_rate", 0.01))
    withdraw_rate = float(cfg.sim.get("withdraw_fee_rate", 0.01))
    r = compute_sale(item, args.qty, price, fee_rate, args.lot)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    cn = cn_of(item.get("note"))

    print(f"=== 记录卖出：{cn or item['market_hash_name']} ===")
    print(f"  成交：{r['qty']} 件 × ¥{r['price']:,.2f} = ¥{r['gross']:,.2f}"
          f"（卖出费 {fee_rate*100:g}% ≈ ¥{r['fee']:,.2f}，进钱包 ¥{r['net']:,.2f}）")
    cash_out = round(r["net"] * (1 - withdraw_rate), 2)
    print(f"  若继续在钱包里买饰品：成本 {fee_rate*100:g}%；"
          f"若提现成现金：再扣 {withdraw_rate*100:g}% ≈ 到手 ¥{cash_out:,.2f}"
          f"（合计成本 {(1 - (1 - fee_rate) * (1 - withdraw_rate)) * 100:.2f}%）")
    print(f"  成本：¥{r['cost']:,.2f}（卖出的是 {' / '.join(f'¥{v:,.2f}' for v in r['sold_lots'])}）")
    print(f"  已实现：¥{r['pnl']:+,.2f}（{r['pnl_pct']:+.2f}%）")
    if slippage is not None:
        note_txt = "（基本按报告价成交）" if abs(slippage) < 1 else \
                   "（报告价偏乐观，实际卖不到那个价）" if slippage < 0 else "（卖得比报告价高）"
        print(f"  报告价：¥{market_price:,.2f} → 实际成交 ¥{r['price']:,.2f}"
              f"（**{slippage:+.1f}%**）{note_txt}")
    if r["remain_lots"]:
        print(f"  剩余 {len(r['remain_lots'])} 件，成本价重算为 ¥{r['new_avg']:,.2f}"
              f"（{' / '.join(f'¥{v:,.2f}' for v in r['remain_lots'])}）")
    else:
        print("  已全部卖出 → 该条目将从持仓中移除")

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件")
        return 0

    # ---- 更新持仓 ----
    if r["remain_lots"]:
        item["quantity"] = len(r["remain_lots"])
        item["buy_price"] = r["new_avg"]
        item["lots"] = r["remain_lots"]
        if item.get("status") == "待售":
            item.pop("status", None)
            item["note"] = re.sub(r"；?用户 [\d-]+ 决定卖出[^；]*", "", str(item.get("note") or ""))
    else:
        items = [x for x in items if x is not item]
    inv_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 追加已实现账本 ----
    trades = load_trades(cfg)
    trades.append({
        "date": date_str,
        "name": item["market_hash_name"],
        "name_cn": cn,
        "qty": r["qty"],
        "price": r["price"],
        "gross": r["gross"],
        "fee": r["fee"],
        "fee_rate": fee_rate,
        "net": r["net"],
        "cost": r["cost"],
        "sold_lots": r["sold_lots"],
        "pnl": r["pnl"],
        "pnl_pct": r["pnl_pct"],
        "market_price_at_sale": round(market_price, 2) if market_price else None,
        "slippage_pct": slippage,
        "note": args.note,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    path = save_trades(cfg, trades)
    print(f"\n✅ 持仓已更新（现 {len(items)} 条）；已实现账本：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
