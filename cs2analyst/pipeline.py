"""全流程编排：行情拉取 → 持仓同步 → 多维度分析 → 决策 → 报告生成 → 归档缓存。

三种运行模式：
  full     已配置 API Key：开放平台K线/价格 + 官网统计补充（真实量能/情绪分）
  snapshot 未配置 API Key：官网公开数据（大盘统计 + 单品详情页快照，无需任何 Key）
  offline  演示模式：本地合成数据，端到端体验完整流程

单饰品失败不影响整体流程：异常自动跳过并记录日志。

────────────────────────────────────────────────────────────────
★ 读代码指南（想快速看懂这个文件，按下面的顺序看）
────────────────────────────────────────────────────────────────

整个文件只有一个入口：run_pipeline()。它按 8 步顺序执行，每步都在代码里用
「# ---------------- N. 步骤名 ----------------」标出，同时打印一行日志（[N/6] ...）：

    第 1 步  拉取大盘行情            _fetch_broad()      → 大盘指数、趋势、风险等级
    第 1b 步 官网统计补充（可选）                          → 真实成交额/情绪分
    第 2 步  加载持仓                load_inventory()    → 谁在持仓、成本、数量
    第 3 步  拉取单品行情                                 → 每件饰品的K线与现价
    第 4 步  单品技术分析            analyze_item()      → 均线/位置/支撑压力/量价
    第 5 步  组合汇总 + 决策         decide()            → 每件的动作与信心度
    第 5.5  观察列表分析（模拟盘用）
    第 5.6  情报采集 + AI 研判       request_judgement() → 大模型解读（只读事实）
    第 5.7  模拟盘双轨执行           PaperTrader.execute_day()
    第 6 步  缓存归档 + 生成报告     ReportGenerator     → Word 报告
    第 7 步  推送到手机              notify_daily()      → 企业微信（失败不影响主流程）

设计要点（为什么这么写）：
  · **每一步失败都不让整体挂掉**：外部接口、大模型、推送全部包在 try 里，
    失败只记日志并降级（例如 K 线拿不到就退官网统计、推送失败就跳过）；
  · **事实由代码算好再交给模型**：第 4、5 步算完指标和评分，第 5.6 步才把它们
    作为「事实」拼成文本喂给大模型——模型不参与计算，只负责解读；
  · **中间结果全部落盘**：分析快照（data/analysis/）、研判（data/llm/）、
    情报（data/intel/）、账本（data/sim/）——因为模型每次调用都没有记忆。

想理解整个项目，读这一个文件 + 它调用的 decide()（决策引擎）和
alerts/checker.py（告警分级）就够讲清架构了。
────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .analysis.item import analyze_item, analyze_item_snapshot
from .analysis.market import analyze_market, analyze_market_stats
from .config import AppConfig
from .datasource.cache import DataCache
from .datasource.inventory import Holding, load_inventory
from .datasource.steamdt import SteamDTClient, _safe_name
from .engine.decision import decide
from .logging_setup import get_logger
from .report.docx_report import PortfolioSummary, ReportGenerator

log = get_logger(__name__)

MODE_LABELS = {
    "full": "开放平台 API + 官网统计补充",
    "snapshot": "官网公开快照（未配置 API Key，无历史K线，支撑压力/均线不可用）",
    "offline": "离线演示数据",
}


class PipelineError(RuntimeError):
    pass


def _fetch_broad(cfg: AppConfig, client: SteamDTClient, cache: DataCache
                 ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """大盘K线获取：当日缓存优先（开放接口有每日限额），失败降级历史缓存。

    周K由日K降采样（每5根取1根）以节省 API 限额。
    返回 (daily, weekly, index)；daily 可能为空 DataFrame。
    """
    from datetime import date as _date

    from .datasource.steamdt import parse_kline_rows

    today = _date.today()
    daily: pd.DataFrame | None = None
    rows = cache.load("market/broad_kline_2.json") or []
    cached_df = parse_kline_rows(rows)
    if not cached_df.empty and cached_df["date"].iloc[-1] == today:
        daily = cached_df
        log.info("使用当日缓存的大盘日K（%d 根，不消耗 API 限额）", len(daily))
    else:
        try:
            daily = client.broad_kline(ktype=2)
            cache.merge_kline("market/broad_kline_2.json",
                              daily[["ts", "open", "close", "high", "low"]].values.tolist())
            log.info("大盘日K API 获取成功：%d 根", len(daily))
        except Exception as exc:
            log.warning("大盘日K API 获取失败: %s", exc)
            if not cached_df.empty:
                daily = cached_df
                log.warning("降级使用历史缓存日K（%d 根，最新 %s）",
                            len(daily), cached_df["date"].iloc[-1])
            else:
                daily = None

    weekly = pd.DataFrame(columns=["ts", "open", "close", "high", "low"])
    if daily is not None and not daily.empty:
        weekly = daily.iloc[::5].reset_index(drop=True)  # 周K由日K降采样

    index: dict = {}
    try:
        index = client.broad_index()
        cache.save("market/broad_index.json", index)
    except Exception as exc:
        log.warning("大盘指数 API 获取失败（%s），尝试使用缓存", exc)
        cached_idx = cache.load("market/broad_index.json") or {}
        if cached_idx:
            index = cached_idx
    return daily if daily is not None else pd.DataFrame(), weekly, index


def run_pipeline(cfg: AppConfig, offline_dir: Path | None = None,
                 inventory_override: Path | None = None) -> dict[str, Any]:
    """执行一次完整分析流水线。返回结果摘要 dict。"""
    started = datetime.now()
    log.info("=" * 60)
    log.info("流水线启动：%s", started.strftime("%Y-%m-%d %H:%M:%S"))

    if offline_dir is not None:
        mode = "offline"
    elif cfg.has_api_key():
        mode = "full"
    else:
        mode = "snapshot"
    log.info("运行模式：%s（%s）", mode, MODE_LABELS[mode])

    client = SteamDTClient(cfg.steamdt, offline_dir=offline_dir)
    cache = DataCache(cfg.data_dir())
    inventory_path = inventory_override or cfg.inventory_path()
    scraper = None
    s_summary, s_txn = {}, None

    # ---------------- 1. 大盘行情 ----------------
    log.info("[1/6] 拉取大盘行情...")
    if mode == "snapshot":
        try:
            from .datasource.steamdt_scraper import SteamDTScraper
            scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=3)
            s_summary = scraper.market_summary()
            s_txn = scraper.transaction_daily()
            market = analyze_market_stats(s_summary, s_txn,
                                          cfg.analysis["ma_windows"], cfg.analysis)
        except Exception as exc:
            log.error("大盘统计拉取失败，本次运行中止：%s", exc)
            raise PipelineError(f"大盘数据拉取失败: {exc}") from exc
    else:
        broad_daily, broad_weekly, index = _fetch_broad(cfg, client, cache)
        if broad_daily is not None and not broad_daily.empty:
            market = analyze_market(broad_daily, broad_weekly, index,
                                    cfg.analysis["ma_windows"], cfg.analysis)
        else:
            # K线不可用（限额/网络）→ 降级官网统计
            try:
                from .datasource.steamdt_scraper import SteamDTScraper
                scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=3)
                s_summary = scraper.market_summary()
                s_txn = scraper.transaction_daily()
                market = analyze_market_stats(s_summary, s_txn,
                                              cfg.analysis["ma_windows"], cfg.analysis)
                log.info("大盘K线不可用，已降级为官网统计判定")
            except Exception as exc:
                log.error("大盘数据（API 与官网）均拉取失败，本次运行中止：%s", exc)
                raise PipelineError(f"大盘数据拉取失败: {exc}") from exc
    log.info("大盘结论：%s / %s → %s", market.trend, market.risk, market.base_tone)

    # ---------------- 1b. 官网公开统计补充（full 模式：真实量能/情绪分；失败不影响主流程） ----------------
    if mode == "full":
        try:
            from .datasource.steamdt_scraper import SteamDTScraper
            scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=2)
            s_summary = scraper.market_summary()
            s_txn = scraper.transaction_daily()
            if market.trend == "数据不足" and s_summary.get("marketIndexTrend"):
                market = analyze_market_stats(s_summary, s_txn,
                                              cfg.analysis["ma_windows"], cfg.analysis)
                log.info("大盘K线不可用，改用官网公开统计判定：%s / %s", market.trend, market.risk)
            else:
                market.enrich_scraper(s_summary, s_txn)
                log.info("已补充官网统计：真实量能=%s，情绪分=%s",
                         market.volume_state, market.sentiment_score)
        except Exception as exc:
            log.warning("官网统计补充失败（不影响主流程）: %s", exc)

    # ---------------- 2. 持仓同步 ----------------
    log.info("[2/6] 加载持仓...")
    holdings = load_inventory(inventory_path)

    # ---- 中文名映射（报告展示 + LLM 输入；失败不影响主流程） ----
    name_map: dict[str, str] = {}
    try:
        from .datasource.name_map import NameResolver
        name_resolver = NameResolver(
            cache_path=cache.root / "name_map.json",
            overrides_path=cfg.config_path.parent / "name_overrides.json",
            client=client if mode == "full" else None,
            scraper=scraper,
        )
        name_map = name_resolver.resolve_many([h.market_hash_name for h in holdings])
        log.info("中文名映射：%d/%d 条已解析",
                 sum(1 for v in name_map.values() if v), len(name_map))
        # 每 7 天刷新一次全量中英对照（4 万+ 条），保证问答能识别任意中文名
        try:
            added = name_resolver.refresh_catalog(days=7)
            if added:
                log.info("全量名称目录已更新（新增 %d 条）", added)
        except Exception as exc:
            log.debug("全量名称目录刷新失败: %s", exc)
    except Exception as exc:
        name_resolver = None
        log.warning("中文名映射失败（报告将显示英文名）: %s", exc)

    # ---- 持仓快照留痕：用户未反馈时，默认「持仓不变」并逐日对比 ----
    try:
        hist_dir = cache.root / "holdings_history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        snap_file = hist_dir / f"{started:%Y-%m-%d}.json"
        items_now = [{
            "market_hash_name": h.market_hash_name,
            "quantity": h.quantity,
            "buy_price": h.buy_price,
            "buy_date": h.buy_date,
            "unlocked": bool(getattr(h, "unlocked", False)),
            "manual_price": h.manual_price,
        } for h in holdings]
        snap_file.write_text(json.dumps({
            "date": started.strftime("%Y-%m-%d"),
            "recorded_at": started.isoformat(timespec="seconds"),
            "source": "config/inventory.json（默认规则：用户未反馈即视为持仓不变）",
            "items": items_now,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        prev_files = sorted(p for p in hist_dir.glob("*.json") if p.name != snap_file.name)
        last_file = hist_dir / "_last.json"
        compare_from = last_file if last_file.exists() else (prev_files[-1] if prev_files else None)
        if compare_from is not None:
            old = json.loads(compare_from.read_text(encoding="utf-8"))
            old_map = {i.get("market_hash_name"): i for i in (old.get("items") or [])}
            new_map = {i["market_hash_name"]: i for i in items_now}
            changes: list[str] = []
            for name in sorted(set(old_map) | set(new_map)):
                o, n = old_map.get(name), new_map.get(name)
                if o is None:
                    changes.append(f"新增 {name} ×{n['quantity']}")
                elif n is None:
                    changes.append(f"移除 {name}（原 ×{o['quantity']}）")
                elif (o.get("quantity") != n.get("quantity")
                      or float(o.get("buy_price") or 0) != float(n.get("buy_price") or 0)):
                    changes.append(f"{name}：{o.get('quantity')} 件 → {n.get('quantity')} 件")
            if changes:
                log.info("持仓变动（对比 %s）：%s", compare_from.stem, "；".join(changes))
            else:
                log.info("持仓与上次记录一致（%d 条 / %d 件）——按「用户未反馈 = 持仓不变」处理",
                         len(items_now), sum(i["quantity"] for i in items_now))
        last_file.write_text(json.dumps({
            "date": started.strftime("%Y-%m-%d"),
            "recorded_at": started.isoformat(timespec="seconds"),
            "items": items_now,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("持仓快照留痕失败（不影响主流程）: %s", exc)

    # ---------------- 3. 单品行情 ----------------
    log.info("[3/6] 拉取单品行情（%d 个条目）...", len(holdings))
    price_map: dict[str, list[dict]] = {}
    if mode in ("full", "offline"):
        # 批量价格接口限 1 次/分钟：full 模式下当日已获取则复用缓存，仅补拉缺失名称；
        # offline 模式从演示数据文件读取（无配额问题）
        names = [h.market_hash_name for h in holdings]
        snap_today = cache.load(f"snapshots/{started:%Y-%m-%d}_prices.json") or {}
        cached_map = {k: v for k, v in snap_today.items() if not k.startswith("_")}
        missing_names = [n for n in names if n not in cached_map] if mode == "full" else names
        if missing_names:
            try:
                fresh = client.batch_price(missing_names)
                cached_map.update(fresh)
                log.info("批量价格获取成功：%d 个（复用缓存 %d 个）",
                         len(fresh), len(names) - len(missing_names))
            except Exception as exc:
                log.warning("批量价格获取失败（%s），使用缓存/降级", exc)
        else:
            log.info("批量价格复用当日缓存：%d/%d", len(cached_map), len(names))
        price_map = cached_map

    # 历史流动性快照（用于在售量趋势）
    snapshots = cache.recent_price_snapshots(14)
    liquidity_history = {s.get("_date", ""): s for s in snapshots}
    history_list = [v for v in liquidity_history.values()]

    # ---------------- 4. 单品分析 ----------------
    log.info("[4/6] 单品技术分析...")
    analyses: list[Any] = []
    missing: list[str] = []

    def scraper_price_fallback(name: str) -> dict | None:
        """挂单量接口不可用时的官网兜底：单品页真实在售价 + 当日成交数。"""
        nonlocal scraper
        if mode == "offline":
            return None
        if scraper is None:
            try:
                from .datasource.steamdt_scraper import SteamDTScraper
                scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=2)
            except Exception:
                return None
        try:
            detail = scraper.item_detail(name)
        except Exception as exc:
            log.debug("官网兜底取价失败 %s: %s", name, exc)
            return None
        rows = []
        for r in (detail.get("sellingPriceList") or []):
            if not isinstance(r, dict):
                continue
            try:
                p = float(r.get("price"))
            except (TypeError, ValueError):
                continue
            if p > 0:
                rows.append({"platform": r.get("platform"),
                             "platformName": r.get("platformName"),
                             "sellPrice": p, "sellCount": 0,
                             "biddingPrice": 0, "biddingCount": 0})
        if not rows:
            return None
        from .analysis.item import _to_int
        return {"price_rows": rows,
                "extra": {"transaction_count": _to_int(detail.get("transactionCount")),
                          "survive_num": _to_int(detail.get("surviveNum"))}}

    def analyze_one(h: Holding, price_map_in: dict, history: list | None,
                    with_avg7: bool = True) -> Any:
        """按模式分析单个标的（持仓或观察列表共用）。"""
        if mode == "snapshot":
            detail = scraper.item_detail(h.market_hash_name) if scraper else {}
            return analyze_item_snapshot(detail, h, cfg.analysis, cfg.decision)
        kline = client.item_kline(h.market_hash_name, ktype=2,
                                  special_style=h.special_style or None)
        rows = kline[["ts", "open", "close", "high", "low"]].values.tolist() if not kline.empty else []
        if rows and mode == "full":  # 演示模式不写缓存，避免污染真实数据
            cache.merge_kline(f"items/{_safe_name(h.market_hash_name)}/kline_2.json", rows)
        avg7 = None
        if with_avg7:
            try:
                avg7 = client.avg7_price(h.market_hash_name)
            except Exception as exc:
                log.debug("7日均价获取失败 %s: %s", h.market_hash_name, exc)
        price_rows = price_map_in.get(h.market_hash_name, [])
        extra = None
        if not price_rows or all(not r.get("sellPrice") for r in price_rows):
            fb = scraper_price_fallback(h.market_hash_name)
            if fb:
                price_rows = fb["price_rows"]
                extra = fb["extra"]
        return analyze_item(h, kline, price_rows, avg7, history,
                            cfg.analysis, cfg.decision, extra=extra)

    for h in holdings:
        try:
            item = analyze_one(h, price_map, history_list)
            if item.data_ok:
                log.info("  ✓ %s：现价 ¥%.2f，盈亏 %+.2f%%，%s，流动性%s",
                         h.market_hash_name, item.cur_price, item.pnl_pct * 100,
                         item.trend, item.liquidity_grade)
            else:
                log.warning("  ✗ %s：%s", h.market_hash_name, item.missing_reason)
                missing.append(h.market_hash_name)
            analyses.append(item)
        except Exception as exc:
            log.error("  ✗ %s：拉取异常，已跳过：%s", h.market_hash_name, exc)
            log.debug(traceback.format_exc())
            missing.append(h.market_hash_name)
            if mode == "snapshot":
                item = analyze_item_snapshot({}, h, cfg.analysis, cfg.decision)
            else:
                item = analyze_item(h, pd.DataFrame(), [], None, None,
                                    cfg.analysis, cfg.decision)
            item.missing_reason = f"数据拉取异常: {exc}"
            analyses.append(item)

    # ---------------- 5. 组合汇总 + 决策 ----------------
    log.info("[5/6] 组合核算与决策引擎...")
    total_cost = sum(a.total_cost for a in analyses)
    total_value = sum(a.market_value for a in analyses if a.data_ok)
    total_pnl = total_value - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
    for a in analyses:
        if a.data_ok and total_value > 0:
            a.weight = a.market_value / total_value

    decisions = []
    for a in analyses:
        if a.data_ok:
            d = decide(market, a, cfg.decision, total_value)
        else:
            from .engine.decision import Action, Decision
            d = Decision(action=Action.HOLD, conviction=1, score=0.0,
                         reasons=[f"数据缺失，无法分析：{a.missing_reason}"],
                         action_text="先核对饰品名称/磨损写法是否正确，补齐数据后重新运行。")
        decisions.append(d)
        log.info("  %s → %s（信心 %d）", a.holding.market_hash_name, d.label, d.conviction)

    details = list(zip(analyses, decisions))
    summary = PortfolioSummary(
        total_cost=round(total_cost, 2),
        total_value=round(total_value, 2),
        total_pnl=round(total_pnl, 2),
        pnl_pct=round(pnl_pct, 2),
        item_count=len(holdings),
        piece_count=sum(h.quantity for h in holdings),
        data_ok_count=sum(1 for a in analyses if a.data_ok),
        missing_names=missing,
    )
    log.info("组合：市值 ¥%.2f / 盈亏 ¥%+.2f (%+.2f%%)",
             total_value, total_pnl, pnl_pct)

    # ---------------- 5.5 情报采集 + LLM 智能研判 ----------------
    sim_data: dict | None = None
    sim_watch_names: list[str] = []
    intel: dict = {}
    judgement = None
    watch_facts: list[dict] = []
    watch_rule_decisions: dict[str, Any] = {}
    watch_prices: dict[str, float] = {}

    def _holding_facts() -> list[dict]:
        facts = []
        for a, d in details:
            h = a.holding
            lots = h.lot_prices
            facts.append({
                "name": h.market_hash_name,
                "name_cn": name_map.get(h.market_hash_name, ""),
                "qty": h.quantity,
                "cost": h.buy_price,
                "price": float(a.cur_price or 0),
                "pnl_pct": a.pnl_pct * 100,
                "trend": a.trend,
                "position": (f"{a.position_pct:.0f}%（{a.position_label}）"
                             if a.position_pct is not None else a.position_label),
                "volume": f"{a.volume_state}（比值 {a.amp_ratio}）" if a.amp_ratio else a.volume_state,
                "liquidity": a.liquidity_grade,
                "rule_action": d.label,
                "rule_score": d.score,
                "rule_reasons": d.reasons[:3],
                # 分件成本与用户意图：让模型知道"这两件成本不同""这件已经决定要卖"
                "lots": lots if h.lots_differ else [],
                "status": h.status,
            })
        return facts

    if mode != "offline" and (cfg.sim.get("enabled", True) or cfg.llm.get("enabled", True)):
        # ---- 观察列表分析（规则决策，供模拟盘与 LLM 使用） ----
        try:
            from .sim.paper import PaperTrader
            from .sim.watchlist import load_watchlist

            common = dict(
                initial_capital=float(cfg.sim.get("initial_capital", 3000.0)),
                fee_rate=float(cfg.sim.get("fee_rate", 0.01)),
                min_cash_ratio=float(cfg.sim.get("min_cash_ratio", 0.15)),
                high_risk_cash_ratio=float(cfg.sim.get("high_risk_cash_ratio", 0.30)),
                max_position_pct=float(cfg.sim.get("max_position_pct", 25.0)),
                hold_days_min=int(cfg.sim.get("hold_days_min", 7)),
            )
            today_str = started.strftime("%Y-%m-%d")
            trader_a = PaperTrader(cfg.sim_state_path(), label="A", **common) \
                if cfg.sim.get("enabled", True) else None
            trader_b = (PaperTrader(cfg.sim_state_path_b(), label="B", **common)
                        if cfg.sim.get("enabled", True) and cfg.sim.get("dual_track", True)
                        else None)

            watch_names = load_watchlist(cfg.sim_watchlist_path())
            if watch_names:
                sim_watch_names = list(watch_names)
                if name_resolver is not None:
                    try:
                        name_map.update(name_resolver.resolve_many(watch_names))
                    except Exception as exc:
                        log.debug("观察列表名称解析失败: %s", exc)
                log.info("[5.5/6] 分析观察列表（%d 个标的）...", len(watch_names))
                watch_items: dict[str, Any] = {}
                for name in watch_names:
                    try:
                        h = Holding(market_hash_name=name, quantity=0, buy_price=0.0)
                        item = analyze_one(h, price_map, history_list, with_avg7=False)
                        if not item.data_ok:
                            log.warning("  观察列表跳过 %s：%s", name, item.missing_reason)
                            continue
                        watch_items[name] = item
                        watch_prices[name] = float(item.cur_price)
                    except Exception as exc:
                        log.warning("  观察列表跳过 %s：%s", name, exc)
                reserve_eq = trader_a.equity(watch_prices) if trader_a else sum(watch_prices.values())
                for name, item in watch_items.items():
                    try:
                        has_pos = bool(trader_a and name in trader_a.state["positions"])
                        d = decide(market, item, cfg.decision, reserve_eq, has_position=has_pos)
                        watch_rule_decisions[name] = d
                        watch_facts.append({
                            "name": name,
                            "name_cn": name_map.get(name, ""),
                            "price": float(item.cur_price),
                            "trend": item.trend,
                            "position": (f"{item.position_pct:.0f}%" if item.position_pct is not None
                                         else item.position_label),
                            "volume": f"{item.volume_state}（{item.amp_ratio}）",
                            "liquidity": item.liquidity_grade,
                            "rule_action": d.label,
                            "rule_score": d.score,
                            "support": item.nearest_support,
                            "resistance": item.nearest_resistance,
                        })
                        if d.action.value != "HOLD":
                            log.info("  规则建议 %s → %s（信心 %d，目标仓位 %s%%）",
                                     name, d.label, d.conviction, d.target_weight_pct)
                    except Exception as exc:
                        log.warning("  观察列表决策失败 %s：%s", name, exc)
        except Exception as exc:
            log.warning("观察列表处理失败（不影响主流程）: %s", exc)
            log.debug(traceback.format_exc())

    # ---- 情报采集（官方更新 + 市场异动） ----
    if mode != "offline" and cfg.intel.get("enabled", True):
        try:
            from .intel.news import IntelCollector
            collector = IntelCollector(cfg.intel, scraper=scraper)
            intel = collector.collect(cache.root / "intel")
        except Exception as exc:
            log.warning("情报采集失败（不影响主流程）: %s", exc)

    # ---- LLM 智能研判 ----
    if mode != "offline" and cfg.llm.get("enabled", True):
        try:
            from .llm.analyst import build_facts, request_judgement
            from .llm.client import LLMClient
            llm = LLMClient(cfg.llm, profile="judgement")   # 判断类：开思考，质量优先
            if llm.enabled:
                sim_note = ""
                if watch_prices:
                    sim_note = (f"观察池 {len(watch_prices)} 个标的，价格区间 "
                                f"¥{min(watch_prices.values()):.2f}~¥{max(watch_prices.values()):.2f}；"
                                f"规则动作分布：" + "、".join(
                                    f"{d.label}×{sum(1 for x in watch_rule_decisions.values() if x.label == d.label)}"
                                    for d in {v.label: v for v in watch_rule_decisions.values()}.values()))
                facts = build_facts(market, _holding_facts(), watch_facts, intel, sim_note)
                log.info("[5.6/6] 调用 DeepSeek 智能研判（事实 %d 字 + 情报 %d 条）...",
                         len(facts), len(intel.get("news") or []))
                judgement = request_judgement(llm, facts)
                if judgement.ok:
                    try:
                        (cache.root / "llm").mkdir(parents=True, exist_ok=True)
                        (cache.root / "llm" / f"{started:%Y-%m-%d}.json").write_text(
                            json.dumps(judgement.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    except OSError as exc:
                        log.warning("LLM 研判存档失败: %s", exc)
        except Exception as exc:
            log.warning("LLM 研判失败（不影响主流程）: %s", exc)
            log.debug(traceback.format_exc())

    # ---------------- 5.7 模拟盘双轨执行 ----------------
    if cfg.sim.get("enabled", True) and mode != "offline" and watch_prices and trader_a:
        try:
            today = started.strftime("%Y-%m-%d")
            tracks: dict[str, dict] = {}
            entry_a = trader_a.execute_day(today, market, watch_rule_decisions, watch_prices)
            tracks["A"] = {
                "label": "A", "name": "纯规则轨",
                "snapshot": trader_a.snapshot(watch_prices, as_of=today),
                "entry": entry_a,
                "decisions": dict(watch_rule_decisions),
                "trades_today": [t for t in trader_a.state["trade_log"] if t.get("date") == today],
                "overlay_notes": [],
            }
            if trader_b is not None:
                from .llm.analyst import apply_overlay
                if judgement is not None and judgement.ok:
                    b_decisions, overlay_notes = apply_overlay(
                        watch_rule_decisions, judgement, cfg.decision)
                else:
                    b_decisions, overlay_notes = (dict(watch_rule_decisions),
                                                  ["AI 研判不可用，B 轨本日与 A 轨一致"])
                entry_b = trader_b.execute_day(today, market, b_decisions, watch_prices)
                tracks["B"] = {
                    "label": "B", "name": "规则+AI 轨",
                    "snapshot": trader_b.snapshot(watch_prices, as_of=today),
                    "entry": entry_b,
                    "decisions": b_decisions,
                    "trades_today": [t for t in trader_b.state["trade_log"] if t.get("date") == today],
                    "overlay_notes": overlay_notes,
                }
                for note in overlay_notes:
                    log.info("  [AI 干预] %s", note)
            sim_data = {
                "tracks": tracks,
                "primary": tracks.get("B", tracks["A"]),
                "judgement": judgement.to_dict() if (judgement and judgement.ok) else None,
                "intel_summary": {
                    "news_count": len(intel.get("news") or []),
                    "anomaly_count": len(intel.get("anomalies") or []),
                    "top_news": [n.get("title") for n in (intel.get("news") or [])[:5]],
                },
            }
        except Exception as exc:
            log.warning("模拟盘运行失败（不影响主流程）: %s", exc)
            log.debug(traceback.format_exc())
    elif mode != "offline" and cfg.sim.get("enabled", True):
        log.warning("模拟盘无可用标的价格，跳过（检查观察列表与行情）")

    # ---------------- 6. 缓存归档 + 报告 ----------------
    log.info("[6/6] 数据归档与报告生成...")
    try:
        # 三种模式都归档当日价格快照。大盘K线已在 _fetch_broad 中合并缓存，这里只存价格。
        # 离线演示写的是自己的沙箱（data/demo_run），不会污染真实 data/。
        snap = {"_date": started.strftime("%Y-%m-%d"), "_mode": mode}
        snap.update({k: v for k, v in price_map.items()})
        cache.save_daily_prices(snap)
    except Exception as exc:
        log.warning("缓存归档失败（不影响报告）: %s", exc)

    # ---------------- 6b. 报告生成 ----------------
    # 保存当日分析快照（供网页问答、控制台复用，避免重复调用接口）
    #
    # 演示模式以前不写快照（怕污染真实 data/），现在 demo 的 data_dir 已经隔离到
    # data/demo_run，所以只有当 data_dir 指向那个沙箱时才写——控制台才能看到演示数据。
    _sandbox = str(cfg.paths.get("data_dir", "")).replace("\\", "/").startswith("data/demo_run")
    if mode != "offline" or _sandbox:
        try:
            analysis_snap = {
                "date": started.strftime("%Y-%m-%d"),
                "market": {"趋势": market.trend, "风险等级": market.risk, "量能": market.volume_state,
                           "大盘指数": market.index_value, "较昨日%": market.diff_ratio,
                           "情绪分": market.sentiment_score, "操作基调": market.base_tone},
                "portfolio": {"总成本": summary.total_cost, "总市值": summary.total_value,
                              "总盈亏": summary.total_pnl, "盈亏%": summary.pnl_pct,
                              "条目数": summary.item_count, "件数": summary.piece_count},
                "items": [{
                    "饰品": a.holding.market_hash_name,
                    "中文名": name_map.get(a.holding.market_hash_name, ""),
                    "数量": a.holding.quantity,
                    "成本价": a.holding.buy_price,
                    "现价": a.cur_price,
                    "盈亏%": round(a.pnl_pct * 100, 2),
                    "趋势": a.trend,
                    "位置": (f"{a.position_label}（{a.position_pct}%）"
                             if a.position_pct is not None else a.position_label),
                    "流动性": a.liquidity_grade,
                    "量价": a.volume_state,
                    "系统建议": d.label,
                    "信心度": d.conviction,
                    "目标仓位%": d.target_weight_pct,
                    "最近支撑": a.nearest_support,
                    "最近压力": a.nearest_resistance,
                    "理由": d.reasons[:3],
                } for a, d in details],
            }
            # 观察池（模拟盘池）也存进快照：告警系统据此挑选「最接近买点」的关注标的
            if watch_facts:
                analysis_snap["watchlist"] = [{
                    "饰品": w.get("name"),
                    "中文名": w.get("name_cn", ""),
                    "现价": w.get("price"),
                    "趋势": w.get("trend"),
                    "位置": w.get("position"),
                    "量价": w.get("volume"),
                    "流动性": w.get("liquidity"),
                    "系统建议": w.get("rule_action"),
                    "评分": w.get("rule_score"),
                    "最近支撑": w.get("support"),
                    "最近压力": w.get("resistance"),
                    "AI立场": (judgement.items.get(w.get("name"), {}) or {}).get("stance")
                    if judgement else None,
                    "AI信心": (judgement.items.get(w.get("name"), {}) or {}).get("confidence")
                    if judgement else None,
                } for w in watch_facts]
            cache.save(f"analysis/{started:%Y-%m-%d}.json", analysis_snap)
        except Exception as exc:
            log.warning("分析快照保存失败（不影响主流程）: %s", exc)

    generator = ReportGenerator(
        heading_font=cfg.report.get("heading_font", "微软雅黑"),
        body_font=cfg.report.get("body_font", "微软雅黑"),
        author=cfg.report.get("author", "CS2 持仓智能分析 Agent"),
        name_map=name_map,
    )
    report_path = cfg.report_dir() / f"持仓分析报告_{started:%Y-%m-%d}.docx"
    try:
        generator.generate(report_path, market, summary, details, run_date=started,
                           mode_label=MODE_LABELS[mode], sim=sim_data)
    except PermissionError:
        # 报告正被 Word 打开 → 换用带时间戳的备用文件名，避免整个流程失败
        report_path = cfg.report_dir() / f"持仓分析报告_{started:%Y-%m-%d}_{started:%H%M}.docx"
        log.warning("原报告文件被占用（可能正在 Word 中打开），改用备用文件名：%s",
                    report_path.name)
        generator.generate(report_path, market, summary, details, run_date=started,
                           mode_label=MODE_LABELS[mode], sim=sim_data)
    log.info("报告已生成：%s", report_path)

    elapsed = (datetime.now() - started).total_seconds()
    log.info("流水线完成，耗时 %.1f 秒。买入 %d / 加仓 %d / 持有 %d / 减仓 %d / 清仓 %d",
             elapsed,
             sum(1 for _, d in details if d.action.value == "BUY"),
             sum(1 for _, d in details if d.action.value == "ADD"),
             sum(1 for _, d in details if d.action.value == "HOLD"),
             sum(1 for _, d in details if d.action.value == "REDUCE"),
             sum(1 for _, d in details if d.action.value == "CLEAR"))
    result = {
        "run_time": started.isoformat(),
        "mode": mode,
        "report_path": str(report_path),
        "market": {"trend": market.trend, "risk": market.risk, "tone": market.base_tone},
        "portfolio": {
            "total_cost": summary.total_cost, "total_value": summary.total_value,
            "total_pnl": summary.total_pnl, "pnl_pct": summary.pnl_pct,
        },
        "decisions": [{"name": a.holding.market_hash_name, "type": d.label,
                       "name_cn": name_map.get(a.holding.market_hash_name, ""),
                       "action": d.action.value, "reasons": d.reasons} for a, d in details],
        "sim": sim_data,
        "name_map": name_map,
        "missing": missing,
    }

    # ---------------- 7. 推送到手机（失败不影响主流程） ----------------
    if mode != "offline":
        try:
            from .notify.wecom import notify_daily
            if notify_daily(cfg.notify, result):
                log.info("已推送日报摘要到手机")
        except Exception as exc:
            log.warning("推送失败（不影响主流程）: %s", exc)
    return result
