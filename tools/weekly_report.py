"""每周复盘推送（同时充当运行心跳检测）。

为什么要它：
- 用户要的是「一个月看成果」，但中途如果云端 cron 静默失效、或某天接口挂了导致日报没跑，
  没人会发现；本周报把「日报/巡检次数」写进推送，等于每周一次体检。
- 顺便把双轨模拟盘、持仓市值、大盘变化汇总成一张周报，不用翻日志。

用法：
    python tools/weekly_report.py              # 生成并推送
    python tools/weekly_report.py --dry-run    # 只生成并打印，不推送
    python tools/weekly_report.py --days 14    # 自定义回看天数
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import AppConfig, load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402

STATE_A = "sim/state_a.json"
STATE_B = "sim/state_b.json"


# ------------------------------------------------------------------
# 数据收集
# ------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _window_days(days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def collect_snapshots(cfg: AppConfig, day_list: list[str]) -> list[tuple[str, dict]]:
    root = cfg.data_dir() / "analysis"
    out: list[tuple[str, dict]] = []
    for d in day_list:
        snap = _read_json(root / f"{d}.json")
        if snap:
            out.append((d, snap))
    return out


def collect_sim(cfg: AppConfig, day_list: list[str]) -> dict:
    """双轨模拟盘：净值取最近一次结算，成交取窗口内记录。"""
    out: dict = {"tracks": [], "trades": 0, "thoughts": ""}
    for path, label in ((cfg.sim_state_path(), "A"), (cfg.sim_state_path_b(), "B")):
        state = _read_json(path)
        if not state:
            continue
        journal = state.get("journal") or []
        last = journal[-1] if journal else {}
        trades = [t for t in (state.get("trade_log") or [])
                  if str(t.get("date", "")) in day_list]
        out["tracks"].append({
            "label": label,
            "name": state.get("label") or f"{label} 轨",
            "initial": float(state.get("initial_capital") or 0),
            "cash": float(state.get("cash") or 0),
            "net_value": float(last.get("net_value") or state.get("cash") or 0),
            "return_pct": float(last.get("return_pct") or 0),
            "positions": len(state.get("positions") or {}),
            "week_trades": len(trades),
            "total_trades": len(state.get("trade_log") or []),
            "last_date": last.get("date") or "",
            "journal_days": len(journal),
        })
        if label == "B" and last.get("thoughts"):
            out["thoughts"] = last["thoughts"]
        out["trades"] += len(trades)
    if not out["thoughts"]:
        a = _read_json(cfg.sim_state_path())
        j = a.get("journal") or []
        out["thoughts"] = (j[-1].get("thoughts") if j else "") or ""
    return out


def collect_alerts(cfg: AppConfig, day_list: list[str]) -> dict:
    root = cfg.data_dir() / "alerts"
    runs = hits = signals = pushed = 0
    day_set: set[str] = set()
    for d in day_list:
        f = root / f"{d}.jsonl"
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                runs += 1
                day_set.add(d)
                alerts = rec.get("alerts") or []
                hits += len(alerts)
                signals += sum(1 for a in alerts if a.get("kind") == "建仓信号")
                # 推送了几条消息：新记录看 mode，老记录（全部推送）按 alerts 是否为空判断
                mode = rec.get("mode") or ("alert" if alerts else "")
                if mode in ("alert", "summary"):
                    pushed += 1
        except OSError:
            continue
    return {"runs": runs, "hits": hits, "signals": signals, "pushed": pushed,
            "days": len(day_set), "day_set": day_set}


def collect_post_sale(cfg: AppConfig, max_days: int = 60) -> list[dict]:
    """卖出后跟踪：把成交价与最新价对比，检验「建议卖出」到底对不对。

    为什么需要：没有这个，卖出就只有一个已实现数字，无法判断系统的判断力。
    有了它，「系统让我卖 → 卖完涨了还是跌了」就有了客观答案；这也是月末复盘
    「AI 建议准确度」的第一手数据。

    取价失败不影响周报（网络/配额问题按「暂缺」处理）。
    """
    path = cfg.data_dir() / "real_trades.json"
    if not path.exists():
        return []
    try:
        trades = json.loads(path.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError):
        return []
    today = date.today()
    recent: list[dict] = []
    for t in trades:
        try:
            d = datetime.strptime(str(t.get("date") or ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - d).days > max_days:
            continue
        recent.append(t)
    if not recent:
        return []
    names = [str(t.get("name")) for t in recent if t.get("name")]
    prices: dict[str, float] = {}
    try:
        from cs2analyst.alerts.checker import pick_price
        from cs2analyst.datasource.steamdt import SteamDTClient
        client = SteamDTClient(cfg.steamdt)
        raw = None
        for attempt in (1, 2):
            try:
                raw = client.batch_price(names)
                break
            except Exception as exc:
                if attempt == 1:
                    # 批量价格接口限「1 次/分钟」，失败多为撞上限流——等一分钟重试一次
                    print(f"[提示] 批量取价失败（{exc}），65 秒后重试一次")
                    time.sleep(65)
                else:
                    raise
        for name, rows in (raw or {}).items():
            price, _basis, _plat = pick_price(rows, "sellPrice")
            if price:
                prices[name] = price
    except Exception as exc:
        print(f"[提示] 卖出后跟踪取价失败（本次跳过）：{exc}")

    out: list[dict] = []
    for t in recent:
        name = str(t.get("name") or "")
        cur = prices.get(name)
        sell = float(t.get("price") or 0)
        if not cur or not sell:
            continue
        chg = (cur / sell - 1) * 100
        days = (today - datetime.strptime(str(t["date"]), "%Y-%m-%d").date()).days
        out.append({
            "name": t.get("name_cn") or name,
            "sell_price": sell,
            "now": round(cur, 2),
            "chg_pct": round(chg, 1),
            "days": days,
            # 卖掉之后价格下跌 → 这次卖出是对的
            "verdict": "✅ 卖对了（之后继续跌）" if chg <= -1
            else "⚠️ 卖早了（之后涨了）" if chg >= 1 else "➖ 基本持平",
        })
    return out


def collect_realized(cfg: AppConfig, day_list: list[str]) -> dict:
    """已实现交易（真实卖出）：卖过什么容易忘，账本必须单独统计。"""
    empty = {"total": 0, "week": 0, "week_pnl": 0.0, "total_pnl": 0.0,
             "avg_slippage": None, "last": ""}
    path = cfg.data_dir() / "real_trades.json"
    if not path.exists():
        return empty
    try:
        trades = json.loads(path.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(trades, list) or not trades:
        return empty
    week = [t for t in trades if str(t.get("date", "")) in day_list]
    slips = [float(t["slippage_pct"]) for t in trades if t.get("slippage_pct") is not None]
    return {
        "total": len(trades),
        "week": len(week),
        "week_pnl": round(sum(float(t.get("pnl") or 0) for t in week), 2),
        "total_pnl": round(sum(float(t.get("pnl") or 0) for t in trades), 2),
        "avg_slippage": round(sum(slips) / len(slips), 1) if slips else None,
        "last": str(trades[-1].get("name_cn") or trades[-1].get("name") or ""),
    }


def active_days(cfg: AppConfig, day_list: list[str]) -> list[str]:
    """系统实际在跑的日期（从最早一条数据算起）。

    否则首周（项目刚上线 / 数据才从某天开始）会误报「日报 2/7 天，有缺失」，
    把真正的故障淹没在噪音里。
    """
    dates: set[str] = set()
    for sub, suffix in (("analysis", ".json"), ("alerts", ".jsonl")):
        d = cfg.data_dir() / sub
        if d.exists():
            dates |= {f.stem for f in d.glob(f"*{suffix}")}
    for p in (cfg.sim_state_path(), cfg.sim_state_path_b()):
        created = str(_read_json(p).get("created") or "")[:10]
        if created:
            dates.add(created)
    dates = {d for d in dates if len(d) == 10 and d[4] == "-"}
    if not dates:
        return list(day_list)
    start = min(dates)
    active = [d for d in day_list if d >= start]
    return active or [day_list[-1]]


def expected_runs(cfg: AppConfig, active: list[str]) -> tuple[int, int]:
    """(应跑的日报天数, 应跑的巡检次数)。"""
    days = len(active)
    return days, days * max(1, len(cfg.alerts.get("times") or list(range(9, 24)) + [0]))


# ------------------------------------------------------------------
# 生成文本
# ------------------------------------------------------------------
def _pct(v: float) -> str:
    return f"{v:+.2f}%"


def build_weekly(cfg: AppConfig, days: int = 7) -> tuple[str, dict]:
    day_list = _window_days(days)
    snaps = collect_snapshots(cfg, day_list)
    sim = collect_sim(cfg, day_list)
    alerts = collect_alerts(cfg, day_list)
    realized = collect_realized(cfg, day_list)
    post_sale = collect_post_sale(cfg) if realized["total"] else []
    active = active_days(cfg, day_list)
    exp_daily, exp_alerts = expected_runs(cfg, active)
    snap_days = {d for d, _ in snaps}
    missing_daily = [d for d in active if d not in snap_days]
    missing_alerts = [d for d in active if d not in alerts["day_set"]]

    first = snaps[0][1] if snaps else {}
    last = snaps[-1][1] if snaps else {}
    p0 = (first.get("portfolio") or {})
    p1 = (last.get("portfolio") or {})
    m0 = (first.get("market") or {})
    m1 = (last.get("market") or {})

    start_d, end_d = day_list[0], day_list[-1]
    week_no = date.today().isocalendar()[1]
    lines = [f"**📅 模拟盘周报 · 第 {week_no} 周（{start_d[5:]} ~ {end_d[5:]}）**", ""]

    # 模拟盘
    if sim["tracks"]:
        track_desc = {"A": "纯规则执行", "B": "规则 + AI 辅助"}
        lines.append(f"**💼 模拟盘对照（起始各 ¥{sim['tracks'][0]['initial']:,.0f}）**")
        for t in sim["tracks"]:
            desc = track_desc.get(str(t["label"]).strip().upper(), t["name"])
            lines.append(f"· **{t['label']} 轨**（{desc}）：净值 **¥{t['net_value']:,.2f}**"
                         f"（{_pct(t['return_pct'])}）｜持仓 {t['positions']} 项｜"
                         f"现金 ¥{t['cash']:,.0f}｜本周成交 {t['week_trades']} 笔"
                         f"（累计 {t['total_trades']} 笔）")
        lines.append(f"· 运行 {sim['tracks'][0]['journal_days']} 个结算日"
                     f"（净值截至 {sim['tracks'][0]['last_date'] or '—'}）")
        # 三方对照：模拟盘 vs 大盘 vs 我的真实持仓
        # 为什么要有这一行：单独看「净值 +0.00%」没有意义；放在一起才能说明
        # 「下降趋势里空仓等待」是一种仓位选择，而不是没在做事。
        parts = []
        for t in sim["tracks"]:
            parts.append(f"{t['label']} 轨 {t['return_pct']:+.2f}%")
        if m0.get("大盘指数") and m1.get("大盘指数"):
            i0, i1 = float(m0["大盘指数"]), float(m1["大盘指数"])
            if i0:
                parts.append(f"大盘 {(i1 / i0 - 1) * 100:+.2f}%")
        if p0.get("总市值") and p1.get("总市值"):
            v0_, v1_ = float(p0["总市值"]), float(p1["总市值"])
            if v0_:
                parts.append(f"我的真实持仓 {(v1_ / v0_ - 1) * 100:+.2f}%")
        if parts:
            lines.append("· **同期对照**：" + " ｜ ".join(parts))
        lines.append("")

    # 持仓
    if p1:
        v0, v1 = float(p0.get("总市值") or 0), float(p1.get("总市值") or 0)
        chg = (v1 / v0 - 1) * 100 if v0 else 0.0
        lines.append("**📊 我的持仓**")
        lines.append(f"市值 ¥{v0:,.0f} → **¥{v1:,.0f}**（本周 {_pct(chg)}）｜"
                     f"成本 ¥{float(p1.get('总成本') or 0):,.0f}｜"
                     f"总盈亏 **{float(p1.get('盈亏%') or 0):+.1f}%**")
        lines.append("")

    # 已实现（真实卖出）
    if realized["total"]:
        txt = f"累计 {realized['total']} 笔，已实现 **¥{realized['total_pnl']:+,.2f}**"
        if realized["week"]:
            txt += f"；本周 {realized['week']} 笔 ¥{realized['week_pnl']:+,.2f}"
        if realized["last"]:
            txt += f"（最近：{realized['last']}）"
        lines.append("**💵 已实现交易（真实卖出）**")
        lines.append(txt)
        if realized.get("avg_slippage") is not None:
            s = realized["avg_slippage"]
            hint = "实际成交≈报告价，估值可靠" if abs(s) < 2 else \
                   "**实际成交明显低于报告价**——低流动性品种的「市值」是纸面数字" if s < 0 else \
                   "实际成交高于报告价"
            lines.append(f"· 成交价 vs 报告价：平均 **{s:+.1f}%**（{hint}）")
        # 卖出后跟踪：检验「建议卖出」对不对（没有这个，卖出就只有一个数字）
        tracking = post_sale or []
        if tracking:
            lines.append("")
            lines.append("**🎯 卖出后跟踪（系统建议卖出 → 之后涨跌）**")
            for t in tracking[:6]:
                lines.append(f"· {t['name']}：卖出 ¥{t['sell_price']:,.2f} → 现价 ¥{t['now']:,.2f}"
                             f"（**{t['chg_pct']:+.1f}%**，{t['days']} 天）{t['verdict']}")
        lines.append("")

    # 大盘
    if m1:
        i0, i1 = float(m0.get("大盘指数") or 0), float(m1.get("大盘指数") or 0)
        ichg = (i1 / i0 - 1) * 100 if i0 else 0.0
        lines.append("**🌡 大盘**")
        lines.append(f"指数 {i0:,.0f} → **{i1:,.0f}**（{_pct(ichg)}）｜"
                     f"{m1.get('趋势', '—')} / {m1.get('风险等级', '—')}｜"
                     f"量能 {m1.get('量能', '—')}｜情绪分 {m1.get('情绪分', '—')}")
        lines.append("")

    # 巡检 + 健康
    daily_ok = len([d for d in active if d in snap_days])
    alert_days = len([d for d in active if d in alerts["day_set"]])
    lines.append(f"**🔔 巡检与运行健康**（统计自 {active[0][5:]} 起，共 {len(active)} 天）")
    lines.append(f"· 日报快照 **{daily_ok}/{exp_daily}** 天"
                 + ("✅" if not missing_daily
                    else "⚠️ 缺失：" + "、".join(d[5:] for d in missing_daily[:5])))
    per_day = max(1, len(cfg.alerts.get("times") or list(range(9, 24)) + [0]))
    lines.append(f"· 异动巡检 **{alerts['runs']}** 次（每天 {per_day} 次）｜"
                 f"有记录的日子 **{alert_days}/{len(active)}** 天"
                 + ("✅" if not missing_alerts
                    else "⚠️ 无记录：" + "、".join(d[5:] for d in missing_alerts[:5])))
    lines.append(f"· 期间打扰你 **{alerts['pushed']}** 次（推送消息数）｜"
                 f"命中信号 {alerts['hits']} 条"
                 + (f"，其中建仓信号 **{alerts['signals']}** 条 🟢" if alerts["signals"]
                    else "，无建仓信号"))
    lines.append("")

    # 想法摘录
    if sim["thoughts"]:
        lines.append("**📝 最新操盘手记**")
        lines.append(f"> {sim['thoughts'][:300]}")
        lines.append("")

    lines.append(f"_生成于 {datetime.now():%Y-%m-%d %H:%M}；数据源：{cfg.data_dir()}_")
    meta = {"days": days, "snapshots": daily_ok, "expected_daily": exp_daily,
            "alert_runs": alerts["runs"], "expected_alert_runs": exp_alerts,
            "alert_hits": alerts["hits"], "signals": alerts["signals"],
            "pushed": alerts["pushed"],
            "missing_daily": missing_daily, "missing_alert_days": missing_alerts,
            "sim": sim["tracks"], "range": [start_d, end_d]}
    return "\n".join(lines), meta


def save_weekly(cfg: AppConfig, text: str) -> Path:
    out_dir = cfg.data_dir() / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    iso = date.today().isocalendar()
    path = out_dir / f"{iso[0]}-W{iso[1]:02d}.md"
    path.write_text(text, encoding="utf-8")
    return path


def to_docx_markdown(text: str) -> str:
    """把企业微信用的 Markdown 调成 md_to_docx 能渲染成「标题 + 列表」的形式。

    周报的章节标题是整行加粗（`**📊 我的持仓**`），md_to_docx 只认 `#` 标题；
    条目符号用的是 `·`，也要换成 `-` 才会变成真正的项目符号列表。
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.fullmatch(r"\*\*(.+?)\*\*", stripped)
        if m:
            out.append(f"## {m.group(1)}")
        elif stripped.startswith("· "):
            out.append("- " + stripped[2:])
        else:
            out.append(line)
    return "\n".join(out)


def save_weekly_docx(cfg: AppConfig, text: str) -> Path | None:
    """把周报同时存一份 Word（用户明确要求：周报也要 Word，不然只有一段文字太短）。

    复用 tools/md_to_docx.py 的渲染器，排版与求职材料那一套一致。
    失败返回 None——周报的文字推送不能被附件问题拖垮。
    """
    try:
        from tools.md_to_docx import convert_text
        out_dir = cfg.data_dir() / "weekly"
        out_dir.mkdir(parents=True, exist_ok=True)
        iso = date.today().isocalendar()
        path = out_dir / f"{iso[0]}-W{iso[1]:02d}.docx"
        convert_text(to_docx_markdown(text), path)
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 周报 Word 生成失败（文字推送照常）：{type(exc).__name__}: {exc}")
        return None


def main() -> int:
    args = sys.argv[1:]
    days = 7
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            print("--days 需要整数")
            return 2
    cfg = load_config()
    setup_logging(cfg.log_dir())

    text, meta = build_weekly(cfg, days)
    # Word 版本：先生成，成功则在推送文案里告诉用户「完整周报见附件」
    docx_path = None if "--no-docx" in args else save_weekly_docx(cfg, text)
    if docx_path:
        text = f"{text}\n\n完整周报见下方附件（Word）：{docx_path.name}"
    path = save_weekly(cfg, text)
    print(text)
    print(f"\n[已保存] {path}")
    if docx_path:
        print(f"[已保存] {docx_path}")
    print(f"[统计] {json.dumps(meta, ensure_ascii=False)}")

    if "--dry-run" in args:
        print("\n[dry-run] 未推送")
        return 0
    hook = str(cfg.notify.get("wecom_webhook") or "").strip()
    if not hook:
        print("\n[跳过推送] 未配置企业微信机器人地址")
        return 1
    from cs2analyst.notify.wecom import send, send_file
    ok = send(text, hook)
    print(f"\n[推送{'成功' if ok else '失败'}]")
    if ok and docx_path and docx_path.exists():
        # 附件失败不影响已发出的文字推送
        if send_file(hook, docx_path):
            print(f"[附件已推送] {docx_path.name}")
        else:
            print("[附件推送失败] 文字已发出，可稍后单独补发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
