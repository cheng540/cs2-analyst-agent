"""入口：python main.py {run|sim|market|demo|schedule|init}

  run       立即执行一次完整分析 + 模拟盘（有 Key=完整模式，无 Key=快照模式）
  sim       查看/重置模拟盘（--reset 重置，--refresh 刷新净值）
  market    大盘环境快报（无需 API Key，官网公开统计，真实成交额/情绪分）
  demo      生成演示数据并离线跑通全流程（无需 API Key）
  schedule  启动每日定时调度（默认 17:30，config.yaml 可改）
  init      初始化目录与示例配置文件
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from cs2analyst.config import PROJECT_ROOT, load_config
from cs2analyst.logging_setup import get_logger, setup_logging

log = get_logger("main")


def cmd_run(cfg) -> int:
    from cs2analyst.pipeline import PipelineError, run_pipeline
    try:
        result = run_pipeline(cfg)
        print("\n===== 本次运行摘要 =====")
        print(f"运行模式：{result['mode']}")
        print(f"大盘：{result['market']['trend']} / {result['market']['risk']}")
        p = result["portfolio"]
        print(f"组合：市值 ¥{p['total_value']:,.2f} / 盈亏 ¥{p['total_pnl']:+,.2f} ({p['pnl_pct']:+.2f}%)")
        for d in result["decisions"]:
            cn = d.get("name_cn") or ""
            label = f"{cn}（{d['name']}）" if cn else d["name"]
            print(f"  {d['type']}  {label}")
        if result.get("sim"):
            tracks = result["sim"].get("tracks") or {}
            for label in sorted(tracks):
                s = tracks[label]["snapshot"]
                print(f"模拟盘 {label} 轨（{tracks[label]['name']}）：净值 ¥{s['net_value']:,.2f}"
                      f"（{s['return_pct']:+.2f}%），持仓 {len(s['positions'])} 项，"
                      f"累计成交 {s['trade_count']} 笔")
            if result["sim"].get("judgement"):
                print(f"AI 研判：{result['sim']['judgement'].get('market_view', '')[:80]}…")
        if result["missing"]:
            print(f"数据缺失：{', '.join(result['missing'])}")
        print(f"报告：{result['report_path']}")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


def cmd_sim(cfg, args) -> int:
    """查看 / 重置模拟盘（双轨）。"""
    from cs2analyst.sim.paper import PaperTrader, reset_state

    if args.reset:
        for p in (cfg.sim_state_path(), cfg.sim_state_path_b()):
            reset_state(p)
        print(f"模拟盘已重置（A/B 双轨）。初始资金 "
              f"¥{cfg.sim.get('initial_capital', 3000):,.2f}，T+{cfg.sim.get('hold_days_min', 7)} 交易规则，"
              "下次运行 main.py run 时开始记录。")
        return 0

    # 读取本地中文名缓存用于展示
    name_map: dict[str, str] = {}
    try:
        import json as _json
        cache_file = cfg.data_dir() / "name_map.json"
        if cache_file.exists():
            name_map = (_json.loads(cache_file.read_text(encoding="utf-8")) or {}).get("map", {})
    except Exception:
        pass

    def dn(n: str) -> str:
        cn = name_map.get(n)
        return f"{cn}（{n}）" if cn and cn != n else n

    common = dict(
        initial_capital=float(cfg.sim.get("initial_capital", 3000.0)),
        fee_rate=float(cfg.sim.get("fee_rate", 0.01)),
        min_cash_ratio=float(cfg.sim.get("min_cash_ratio", 0.15)),
        high_risk_cash_ratio=float(cfg.sim.get("high_risk_cash_ratio", 0.30)),
        max_position_pct=float(cfg.sim.get("max_position_pct", 25.0)),
        hold_days_min=int(cfg.sim.get("hold_days_min", 7)),
    )
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    tracks = [("A", "纯规则轨", cfg.sim_state_path()),
              ("B", "规则+AI 轨", cfg.sim_state_path_b())]
    if not cfg.sim.get("dual_track", True):
        tracks = tracks[:1]

    print("\n===== 模拟盘状态（T+%d 交易规则）=====" % common["hold_days_min"])
    for label, name, path in tracks:
        trader = PaperTrader(path, label=label, **common)
        snap = trader.snapshot({}, as_of=today)
        print(f"\n-- {label} 轨（{name}）--")
        print(f"   初始资金 ¥{snap['initial_capital']:,.2f} | 当前净值 ¥{snap['net_value']:,.2f}"
              f"（{snap['return_pct']:+.2f}%）")
        print(f"   现金 ¥{snap['cash']:,.2f} | 累计成交 {snap['trade_count']} 笔")
        if snap["positions"]:
            for p in snap["positions"]:
                lock = (f"锁定期内，可售日 {p['locked_until']}（剩 {p['days_left']} 天）"
                        if p.get("locked") else "可售")
                print(f"     {dn(p['name'])} ×{p['qty']}  成本 ¥{p['avg_cost']:,.2f}  {lock}")
        else:
            print("     持仓：空仓")
        if snap["journal"]:
            jr = snap["journal"][-1]
            print(f"   最近记录：[{jr['date']}] 净值 ¥{jr['net_value']:,.2f}"
                  f"（{jr['return_pct']:+.2f}%），操作 {jr['trades']} 笔")
    return 0


def cmd_demo(cfg) -> int:
    """离线演示：合成数据跑通全流程，**不碰真实数据**。

    为什么要把数据目录换掉：demo 走的是同一条 pipeline，pipeline 会把行情写进
    `cfg.data_dir()`。直接跑会把合成指数（~1000）追加进真实的
    `data/market/broad_kline_2.json`，在真实图表上留一根假尖刺，还会覆盖
    `data/market/broad_index.json` 与 `data/holdings_history/`（2026-10-29 实际踩到）。
    所以演示跑的缓存与报告全部落在 `data/demo_run/`，可随时整个删掉。
    """
    import copy

    from demo.generate_demo_data import generate_demo_data
    from cs2analyst.pipeline import run_pipeline

    demo_dir = cfg.data_dir() / "demo"                 # 合成行情 fixture（离线数据源）
    sandbox = cfg.data_dir() / "demo_run"               # 演示产生的缓存与报告
    sandbox.mkdir(parents=True, exist_ok=True)
    inv_path = sandbox / "inventory_demo.json"
    generate_demo_data(demo_dir, inv_path)
    print(f"演示数据已生成：{demo_dir}")
    print(f"演示持仓文件：{inv_path}")

    run_cfg = copy.deepcopy(cfg)
    run_cfg.paths = {**cfg.paths,
                     "data_dir": "data/demo_run",
                     "report_dir": "data/demo_run/reports"}
    print("开始离线运行完整流水线（数据写入 data/demo_run，不影响真实数据）...\n")
    result = run_pipeline(run_cfg, offline_dir=demo_dir, inventory_override=inv_path)
    print("\n===== 演示运行摘要 =====")
    print(f"大盘：{result['market']['trend']} / {result['market']['risk']}")
    p = result["portfolio"]
    print(f"组合：市值 ¥{p['total_value']:,.2f} / 盈亏 ¥{p['total_pnl']:+,.2f} ({p['pnl_pct']:+.2f}%)")
    for d in result["decisions"]:
        print(f"  {d['type']}  {d['name']}")
    if result["missing"]:
        print(f"数据缺失（演示预期行为）：{', '.join(result['missing'])}")
    print(f"报告：{result['report_path']}")
    return 0


def cmd_market(cfg) -> int:
    """大盘环境快报：无需 API Key，官网公开统计。"""
    from datetime import datetime

    from cs2analyst.analysis.market import analyze_market_stats
    from cs2analyst.datasource.steamdt_scraper import SteamDTScraper
    from cs2analyst.report.market_report import generate_market_report

    print("拉取 SteamDT 官网公开统计...")
    scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=3)
    summary = scraper.market_summary()
    txn = scraper.transaction_daily()
    try:
        players = scraper.players_daily()
    except Exception:
        players = None
    env = analyze_market_stats(summary, txn, cfg.analysis["ma_windows"], cfg.analysis)

    print("\n===== 大盘环境快照 =====")
    print(f"大盘指数：{env.index_value:,.2f}" +
          (f"（较昨日 {env.diff_ratio:+.2f}%）" if env.diff_ratio is not None else ""))
    print(f"趋势判定：{env.trend}   风险等级：{env.risk}")
    print(f"量能：{env.volume_state}" + (f"（{env.volume_source}）" if env.volume_source else ""))
    if env.sentiment_score is not None:
        print(f"平台情绪分：{env.sentiment_score:.0f}（仅参考）")
    print(f"操作总基调：{env.base_tone}")
    for ev in env.evidence:
        print(f"  · {ev}")

    # 快照存档 + 快报
    import json
    snap_path = cfg.data_dir() / f"market_snapshot_{datetime.now():%Y-%m-%d}.json"
    snap_path.write_text(json.dumps({
        "time": datetime.now().isoformat(), "trend": env.trend, "risk": env.risk,
        "index": env.index_value, "diff_ratio": env.diff_ratio,
        "volume_state": env.volume_state, "sentiment": env.sentiment_score,
        "evidence": env.evidence,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = cfg.report_dir() / f"大盘环境快报_{datetime.now():%Y-%m-%d}.docx"
    generate_market_report(report_path, env, txn, players, run_date=datetime.now())
    print(f"\n快照已存档：{snap_path}")
    print(f"快报已生成：{report_path}")
    return 0


def cmd_brief(cfg) -> int:
    """上下文恢复简报：给未来的 AI / 用户自己快速回到状态。"""
    from datetime import datetime

    from cs2analyst.brief import build_brief, save_brief

    today = datetime.now().strftime("%Y-%m-%d")
    text = build_brief(cfg, today)
    print(text)
    path = save_brief(cfg, text, today)
    print(f"\n（简报已存档：{path}）")
    return 0


def cmd_schedule(cfg) -> int:
    if not cfg.has_api_key():
        log.warning("未配置 API Key：定时任务将以「官网公开快照模式」运行"
                    "（无单品历史K线，决策功能受限）。建议申请免费 Key 后重启用完整模式。")
    from cs2analyst.scheduler import run_scheduler
    run_scheduler(cfg)
    return 0


def cmd_init(cfg) -> int:
    """初始化目录与示例文件。"""
    created = []
    inv_path = cfg.inventory_path()
    example = inv_path.parent / "inventory.example.json"
    if not inv_path.exists() and example.exists():
        shutil.copyfile(example, inv_path)
        created.append(str(inv_path))
    for d in (cfg.data_dir(), cfg.report_dir(), cfg.log_dir()):
        d.mkdir(parents=True, exist_ok=True)
    print("初始化完成：")
    print(f"  配置文件：{cfg.config_path}")
    for c in created:
        print(f"  已创建：{c}")
    print("  下一步：1) 在 config/config.yaml 填入 SteamDT API Key；2) 编辑持仓文件；3) 运行 `python main.py run`")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CS2 饰品持仓智能分析 Agent")
    parser.add_argument("--config", type=str, default=None, help="自定义配置文件路径")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="完整持仓分析 + 模拟盘")
    sub.add_parser("brief", help="上下文恢复简报（读回项目全貌）")
    p_sim = sub.add_parser("sim", help="模拟盘管理")
    p_sim.add_argument("--reset", action="store_true", help="重置模拟盘")
    p_sim.add_argument("--refresh", action="store_true", help="拉取最新价格刷新净值")
    sub.add_parser("market", help="大盘环境快报（免Key）")
    sub.add_parser("demo", help="离线演示")
    sub.add_parser("schedule", help="每日定时调度")
    sub.add_parser("init", help="初始化")
    parser.set_defaults(command="run")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir())

    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "brief":
        return cmd_brief(cfg)
    if args.command == "sim":
        return cmd_sim(cfg, args)
    if args.command == "market":
        return cmd_market(cfg)
    if args.command == "demo":
        return cmd_demo(cfg)
    if args.command == "schedule":
        return cmd_schedule(cfg)
    return cmd_init(cfg)


if __name__ == "__main__":
    sys.exit(main())
