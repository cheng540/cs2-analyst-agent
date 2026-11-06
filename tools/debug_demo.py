"""调试工具：离线演示模式下逐项打印分析字段与决策结果。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config
from cs2analyst.logging_setup import _fix_console_encoding, setup_logging
from cs2analyst.datasource.inventory import load_inventory
from cs2analyst.datasource.steamdt import SteamDTClient
from cs2analyst.analysis.item import analyze_item
from cs2analyst.analysis.market import analyze_market
from cs2analyst.engine.decision import decide
from demo.generate_demo_data import generate_demo_data

cfg = load_config()
_fix_console_encoding()
demo_dir = cfg.data_dir() / "demo"
inv_path = cfg.inventory_path().parent / "inventory_demo.json"
generate_demo_data(demo_dir, inv_path)

client = SteamDTClient(cfg.steamdt, offline_dir=demo_dir)
broad_daily = client.broad_kline(2)
broad_weekly = client.broad_kline(3)
index = client.broad_index()
market = analyze_market(broad_daily, broad_weekly, index, cfg.analysis["ma_windows"], cfg.analysis)
print(f"市场: trend={market.trend} risk={market.risk} block_add={market.block_add()}")

holdings = load_inventory(inv_path)
price_map = client.batch_price([h.market_hash_name for h in holdings])
analyses = []
for h in holdings:
    try:
        kline = client.item_kline(h.market_hash_name, 2)
        avg7 = client.avg7_price(h.market_hash_name)
        a = analyze_item(h, kline, price_map.get(h.market_hash_name, []), avg7, None,
                         cfg.analysis, cfg.decision)
        analyses.append(a)
    except Exception as exc:
        a = analyze_item(h, None, [], None, None, cfg.analysis, cfg.decision)
        a.data_ok = False
        a.missing_reason = f"拉取异常: {exc}"
        analyses.append(a)

total = sum(a.market_value for a in analyses if a.data_ok)
for a in analyses:
    a.weight = a.market_value / total if (a.data_ok and total > 0) else 0.0

for a in analyses:
    print("=" * 70)
    print(f"{a.holding.market_hash_name}  data_ok={a.data_ok} {a.missing_reason}")
    if not a.data_ok:
        continue
    print(f"  cur={a.cur_price} avg7={a.avg7_price} pnl%={a.pnl_pct*100:.2f} weight%={a.weight*100:.1f}")
    print(f"  trend={a.trend} ma={a.ma_state} pos%={a.position_pct} {a.position_label}")
    print(f"  sup={a.supports} near_sup={a.near_support} strong={a.strong_support} "
          f"res={a.resistances} near_res={a.near_resistance}")
    print(f"  liq={a.liquidity_grade} sell={a.sell_count} bid={a.bid_count}")
    print(f"  vol={a.volume_state} amp_ratio={a.amp_ratio} ret5={a.ret_5d} ret3={a.ret_3d} "
          f"break={a.break_support} stall={a.vol_up_stall} down={a.vol_up_down} stopfall={a.shrink_stop_falling}")
    d = decide(market, a, cfg.decision, total)
    print(f"  ==> {d.label}")
    for r in d.reasons:
        print(f"      - {r}")
    print(f"      action: {d.action}")
