"""价格异动告警（盘中巡检 + 分级推送）。

设计：
- 巡检时间由 cron 控制（默认 9:00–24:00 每小时，共 16 次），其余时间不检查、不推送；
- 监控范围 = 全部持仓 + 每日 AI 筛选出的关注标的（含所有买点信号标的）；
- 取价优先用开放平台批量价格接口（1 次请求拿全部），失败退化为官网单品页快照（限量，避免打爆官网）；
- 触发规则（可在 config/config.yaml 的 alerts 段调整）：
    · 相比上次检查跌/涨超阈值        · 相比昨日价格跌/涨超阈值
    · 在售量骤变（抛压或扫货）      · 亏损/盈利跨 10 个百分点档位
    · 跌破最近支撑位                · 建仓信号：系统建议为「买入/加仓」
- **分级推送**（巡检加密后的关键设计，防止告警疲劳）：
    · 紧急 → 立即推送：建仓信号 / 有效破位 / 盈亏跨档 / 单次跌≥4% / 单次涨≥6%
    · 温和 → 只写日志，不打扰；由 12:00 与 22:00 两条「盘中概览」一次性交代
  → 用户每天固定最多 2 条汇总 + 少量真正紧急的消息；
- 防轰炸闸门：跌/涨两类紧急消息每天各最多推 3 条，超出自动折叠进下一次汇总；
  建仓信号 / 破位 / 盈亏跨档永不折叠（这些是真需要你看见的）；
- 去抖：同一标的同一类信号按**滚动 24 小时**去重（不是自然日，避免 23:00 与 0:00 重复）；
- 每次巡检的取价都会落盘到 data/intraday/日期.jsonl，积累日内价格曲线
  （可供日后做盘中挂单成交、日内波动率统计）；
- 推送带 AI 点评（单次巡检合并成一次模型调用，成本约几分钱）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..datasource.inventory import load_inventory
from ..datasource.steamdt import SteamDTClient, _safe_name
from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_RULES = {
    "drop_pct_vs_last": 3.0,
    "drop_pct_vs_yesterday": 5.0,
    "rise_pct_vs_last": 4.0,
    "rise_pct_vs_yesterday": 6.0,
    "sell_count_change_pct": 30.0,
    "loss_alert_pct": 40.0,
    "profit_alert_pct": 20.0,
    "break_support": True,
    "signal_alert": True,    # 系统给出「买入/加仓」建议时第一时间提醒（买点不必等日报）
    # ---- 分级推送 ----
    "urgent_drop_pct": 4.0,          # 单次（1 小时）跌幅达到该值 → 紧急
    "urgent_rise_pct": 6.0,          # 单次涨幅达到该值 → 紧急
    "summary_times": [12, 22],       # 这两个整点的巡检会额外发送「盘中概览」
    "urgent_max_per_kind_per_day": 3,  # 跌/涨两类紧急消息每天的上限，超出折叠进汇总
    "confirm_urgent": True,          # 紧急的跌/涨/破位需连续两次巡检确认（治单次跳变噪音）
    "confirm_window_minutes": 90,    # 两次确认的有效间隔（超过则重新计数）
    "cooldown_hours": 24,            # 同一标的同一类信号的滚动去重窗口（小时）
    "fallback_max_items": 8,         # 批量接口失败时，官网兜底最多抓几个标的
    "batch_retry_wait": 65,          # 批量接口失败（多为 1 次/分钟限流）后等待重试的秒数；0=不重试
    "intraday_keep_days": 180,       # 日内价格曲线保留天数
    "ai_commentary": True,
    "max_items": 30,
    "watch_top_n": 5,        # 观察池里取「最接近买点」的前 N 个纳入监控
    "outlier_pct": 60.0,     # 与上次价格偏差超过该比例视为脏数据，跳过
}


def clean_sell_prices(sells: list[float]) -> list[float]:
    """剔除占位脏价（如个别平台返回 0.09）：低于中位数 30% 的报价视为异常。"""
    vals = sorted(v for v in sells if v and v > 0)
    if not vals:
        return []
    median = vals[len(vals) // 2]
    cleaned = [v for v in vals if v >= median * 0.3]
    return cleaned or vals


def _is_buy_signal(action: Any) -> bool:
    """当日系统建议是否为可执行的买点（买入建仓 / 加仓）。"""
    text = str(action or "")
    return "买入" in text or "加仓" in text


# ------------------------------------------------------------
# 价格基准：告警必须用「用户真正能成交的价」
#
# 为什么不用「全平台最低价」：HALOSKINS 这类境外平台的一行报价会主导最低价，
# 而那一行时有时无——同一个饰品会在 201.69 与 218.00 之间来回跳，
# 制造出「破位 -7.6%」这种假信号（2026-09-24 实测踩到）。
# 用户交易基本只在悠悠有品，因此基准取悠悠报价；缺失时退国内平台中位数。
# ------------------------------------------------------------
ALERT_PLATFORM = "youpin"
BASIS_LABEL = "悠悠有品"
CN_FALLBACK_PLATFORMS = ("buff", "c5")
PLACEHOLDER_RATIO = 0.3          # 低于中位数该比例的报价视为占位脏价（如 0.09）
PRICE_MIN, PRICE_MAX = 0.01, 500_000.0   # 绝对合理性区间（挡住时间戳/占位值混入价格）


def _num(v: Any) -> float | None:
    """把可能是字符串/None/嵌套对象的字段转成有限浮点数。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def platform_prices(rows: Any, price_key: str = "sellPrice") -> dict[str, float]:
    """多平台报价 → {平台: 价格}，剔除不可用、越界与占位脏价。"""
    raw: dict[str, float] = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        plat = str(r.get("platform") or "").strip().lower()
        price = _num(r.get(price_key))
        if not plat or price is None or not (PRICE_MIN <= price <= PRICE_MAX):
            continue
        raw[plat] = price
    if len(raw) < 2:
        return raw
    vals = sorted(raw.values())
    med = vals[len(vals) // 2]
    return {k: v for k, v in raw.items() if v >= med * PLACEHOLDER_RATIO}


def pick_price(rows: Any, price_key: str = "sellPrice") -> tuple[float | None, str, dict]:
    """挑出告警基准价 → (价格, 基准说明, 各平台价)。

    1) 有悠悠有品报价 → 用悠悠（用户实际成交的地方）
    2) 没有悠悠 → 用国内平台（BUFF/C5）中位数
    3) 都没有 → 用全平台中位数（注意不是最低价，避免被单一平台主导）
    """
    by = platform_prices(rows, price_key)
    if not by:
        return None, "", {}
    if ALERT_PLATFORM in by:
        return by[ALERT_PLATFORM], BASIS_LABEL, by
    cn = sorted(by[p] for p in CN_FALLBACK_PLATFORMS if p in by)
    if cn:
        return cn[(len(cn) - 1) // 2], "国内平台中位数", by
    vals = sorted(by.values())
    # 下中位数：取偏保守的那一档（这是「能卖多少钱」的参考，宁可保守）
    return vals[(len(vals) - 1) // 2], "全平台中位数", by


@dataclass
class Alert:
    """一条异动告警。"""
    name: str
    name_cn: str
    kind: str            # 跌 / 涨 / 在售量 / 盈亏 / 破位 / 建仓信号
    detail: str
    price: float | None = None
    pct: float | None = None
    qty: int = 0
    cost: float = 0.0
    pnl_pct: float | None = None
    # 来源：持仓 / 买点信号 / AI关注(...) —— 决定 AI 点评能不能说清「这件是不是你持有的」。
    # 历史教训：这两个字段原先没有，AI 只拿到「名称+异动描述」，于是点评只能写
    # 「持仓者：若已过 T+7…若仍在锁定期…」这种两头都说的模板话（用户直接指出了这个问题）。
    source: str = ""
    buy_date: str = ""          # 持仓买入日期；空=未知 → 算不出 T+7 锁定期
    unlocked: bool = False      # 用户已确认「早就过了 7 天锁定期」（buy_date 缺失时的兜底）

    def to_line(self) -> str:
        tag = {"跌": "🔻", "涨": "🔺", "在售量": "📦", "成交量": "📦", "盈亏": "💰", "破位": "⚠️",
               "建仓信号": "🟢"}.get(self.kind, "•")
        held = f"【持仓 {self.qty} 件】" if self.qty > 0 else ""
        return f"{tag} {held}**{self.name_cn or self.name}**：{self.detail}"


@dataclass
class AlertRun:
    """一次巡检结果。"""
    time: str = ""
    checked: int = 0
    expected: int = 0        # 监控清单总数（checked < expected 说明本轮取价不完整）
    alerts: list[Alert] = field(default_factory=list)      # 本次命中的全部信号（含温和）
    urgent: list[Alert] = field(default_factory=list)      # 本次立即推送的（紧急）
    mild: list[Alert] = field(default_factory=list)        # 本次静默记录的（含被闸门折叠的）
    mode: str = ""            # ""=未推送 / "alert"=异动提醒 / "summary"=盘中概览
    message: str = ""         # 组装好的推送原文（干跑时可预览）
    ai_comment: str = ""
    error: str = ""
    skipped: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)


class AlertChecker:
    def __init__(self, cfg: AppConfig, rules: dict | None = None):
        self.cfg = cfg
        merged = dict(DEFAULT_RULES)
        merged.update(cfg.alerts or {})
        merged.update(rules or {})
        self.rules = merged
        self.root = cfg.data_dir() / "alerts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.state = self._load_state()

    # ---------------- 状态 ----------------
    def _load_state(self) -> dict:
        state: dict = {}
        try:
            if self.state_path.exists():
                state = json.loads(self.state_path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            state = {}
        state.setdefault("last_prices", {})
        state.setdefault("last_price_basis", {})  # {标的: 价源}，价源变了就不比较
        state.setdefault("last_counts", {})
        state.setdefault("sent_ts", {})       # {name|kind: ISO 时间}，滚动窗口去重
        state.setdefault("pending", {})       # {name|kind: {seen, count}}，两次确认用
        state.setdefault("mild", [])          # 尚未汇总的温和异动缓冲
        state.setdefault("urgent_by_day", {})  # {日期: {类别: 次数}}，防轰炸闸门
        state.setdefault("last_summary", "")   # 上次发送「盘中概览」的时间
        state.setdefault("last_run", "")
        # 兼容旧状态文件（sent 为按自然日计数）：迁移成时间戳，避免升级瞬间重复推送
        old_sent = state.pop("sent", None) or {}
        if old_sent and not state["sent_ts"]:
            now_iso = datetime.now().isoformat(timespec="seconds")
            for day, rec in old_sent.items():
                for key in (rec or {}):
                    state["sent_ts"][key] = now_iso
            log.info("已迁移旧去重记录 %d 条到滚动窗口", len(state["sent_ts"]))
        # 价格基准迁移：旧基准是「全平台最低价」（会被境外平台一行报价主导），
        # 新基准是「悠悠有品」——两者不可比，旧基准必须丢弃，
        # 否则升级瞬间每件持仓都会被算出一个假的 +8% 跳涨。
        if state["last_prices"] and not state["last_price_basis"]:
            log.info("价格基准已更换（全平台最低价 → 悠悠有品），清空旧价格基准 %d 条",
                     len(state["last_prices"]))
            state["last_prices"] = {}
        return state

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("告警状态保存失败：%s", exc)

    # ---------------- 监控清单 ----------------
    @staticmethod
    def _unmonitorable(name: str, manual_price: float = 0.0) -> bool:
        """无行情数据、无法监控的品类（贴纸/印花等）。"""
        return manual_price > 0 or str(name).lower().startswith("sticker")

    def _load_advice(self) -> dict[str, dict]:
        """读当日分析快照，做成「标的 → {系统建议, 评分, 最近支撑}」查询表。"""
        advice: dict[str, dict] = {}
        try:
            snap_files = sorted((self.cfg.data_dir() / "analysis").glob("*.json"))
            if not snap_files:
                return advice
            snap = json.loads(snap_files[-1].read_text(encoding="utf-8"))
            for it in (snap.get("items") or []):
                name = it.get("饰品")
                if not name:
                    continue
                advice[name] = {"action": it.get("系统建议"), "score": it.get("评分"),
                                "support": it.get("最近支撑"), "ai_stance": it.get("AI立场")}
            for w in (snap.get("watchlist") or []):
                name = w.get("饰品")
                if name and name not in advice:
                    advice[name] = {"action": w.get("系统建议"), "score": w.get("评分"),
                                    "support": w.get("最近支撑"), "ai_stance": w.get("AI立场")}
        except Exception as exc:
            log.debug("读取当日系统建议失败：%s", exc)
        return advice

    def collect_items(self) -> list[dict]:
        """监控清单 = 全部持仓（排除贴纸）+ 观察池中最接近买点的标的 + AI 明确看多/看空的标的。

        每个标的都会带上当日「系统建议」，以便巡检时能第一时间发现建仓（买入/加仓）信号
        ——买点不必等到 17:30 的日报，盘中任意一次巡检就能推到你手上。
        """
        items: list[dict] = []
        skipped: list[str] = []
        advice = self._load_advice()
        try:
            for h in load_inventory(self.cfg.inventory_path()):
                if self._unmonitorable(h.market_hash_name, h.manual_price):
                    skipped.append(h.market_hash_name)
                    continue
                a = advice.get(h.market_hash_name) or {}
                items.append({"name": h.market_hash_name, "qty": h.quantity,
                              "cost": h.buy_price, "source": "持仓",
                              "buy_date": h.buy_date, "unlocked": bool(h.unlocked),
                              "action": a.get("action"), "score": a.get("score"),
                              "support": a.get("support")})
        except Exception as exc:
            log.warning("读取持仓失败：%s", exc)

        # 观察池：先无条件收进所有「买入/加仓」信号的标的（买点绝不能被 Top N 挤掉），
        # 再按系统评分补足最接近买点的 Top N。
        top_n = int(self.rules.get("watch_top_n", 5))
        try:
            snap_files = sorted((self.cfg.data_dir() / "analysis").glob("*.json"))
            if snap_files:
                snap = json.loads(snap_files[-1].read_text(encoding="utf-8"))
                watch = sorted(snap.get("watchlist") or [],
                               key=lambda w: (w.get("评分") if isinstance(w.get("评分"), (int, float))
                                              else -99), reverse=True)
                picks: list[dict] = []
                picked: set[str] = set()
                signals = [w for w in watch if _is_buy_signal(w.get("系统建议"))]
                for w in signals + watch[:top_n]:
                    name = w.get("饰品")
                    if not name or name in picked or self._unmonitorable(name):
                        continue
                    picked.add(name)
                    picks.append(w)
                for w in picks:
                    name = w["饰品"]
                    score = w.get("评分")
                    stance = w.get("AI立场") or ""
                    tag = "AI关注"
                    if _is_buy_signal(w.get("系统建议")):
                        tag = "买点信号"
                    if isinstance(score, (int, float)):
                        tag = f"{tag}(评分{score:+.1f}{'/' + stance if stance else ''})"
                    items.append({"name": name, "qty": 0, "cost": 0,
                                  "support": w.get("最近支撑"),
                                  "action": w.get("系统建议"), "score": score,
                                  "source": tag})
        except Exception as exc:
            log.debug("读取观察池失败：%s", exc)

        # AI 明确看多/看空的高信心标的（去重后补入）
        try:
            llm_files = sorted((self.cfg.data_dir() / "llm").glob("*.json"))
            if llm_files:
                j = json.loads(llm_files[-1].read_text(encoding="utf-8"))
                for name, info in (j.get("items") or {}).items():
                    if self._unmonitorable(name):
                        continue
                    stance = str((info or {}).get("stance") or "").lower()
                    try:
                        conf = int((info or {}).get("confidence") or 0)
                    except (TypeError, ValueError):
                        conf = 0
                    if (stance in ("buy", "add") or
                            (stance in ("avoid", "reduce") and conf >= 4)):
                        items.append({"name": name, "qty": 0, "cost": 0,
                                      "source": f"AI关注({stance}/{conf})"})
        except Exception as exc:
            log.debug("读取 AI 研判失败：%s", exc)

        # 去重（持仓优先），并限制总量
        seen: dict[str, dict] = {}
        for it in items:
            n = it.get("name")
            if not n:
                continue
            if n in seen:
                if it.get("source", "").startswith("持仓"):
                    seen[n] = it
                continue
            seen[n] = it
        out = list(seen.values())[: int(self.rules.get("max_items", 30))]
        log.info("巡检清单：%d 个标的（持仓 %d + AI 关注 %d）%s",
                 len(out), sum(1 for x in out if x.get("source") == "持仓"),
                 sum(1 for x in out if x.get("source") != "持仓"),
                 f"；跳过无法监控 {len(skipped)} 个" if skipped else "")
        return out

    # ---------------- 取价 ----------------
    @staticmethod
    def _parse_batch(raw: dict) -> dict[str, dict]:
        """批量接口返回 → {标的: {price, price_basis, platforms, sell_count, ...}}。

        基准价按平台挑（优先悠悠有品），不再用「全平台最低价」——见文件头部说明。
        count_kind 也很关键：批量接口给的是「挂单量」，官网兜底给的是「当日成交数」，
        两者口径不同，绝不能拿来互相比较（否则会误报「被扫货 -99%」）。
        """
        prices: dict[str, dict] = {}
        for name, rows in (raw or {}).items():
            price, basis, platforms = pick_price(rows, "sellPrice")
            if price is None:
                continue
            counts = sum(int(r.get("sellCount") or 0) for r in (rows or [])
                         if isinstance(r, dict))
            prices[name] = {"price": price, "price_basis": basis, "platforms": platforms,
                            "sell_count": counts, "count_kind": "listings",
                            "source": "开放平台批量价格"}
        return prices

    def fetch_prices(self, names: list[str]) -> dict[str, dict]:
        """优先批量接口（1 次请求拿全部）。

        批量接口的限制是「1 次/分钟」，失败多半是撞上限流——等一分钟重试一次
        就能拿回完整覆盖，远好于退回官网逐个抓（只够抓 8 个）。重试仍失败才退化。
        """
        prices: dict[str, dict] = {}
        client: SteamDTClient | None = None
        for attempt in (1, 2):
            try:
                if client is None:
                    client = SteamDTClient(self.cfg.steamdt)
                prices = self._parse_batch(client.batch_price(names))
                log.info("批量取价成功：%d/%d", len(prices), len(names))
                break
            except Exception as exc:
                wait = float(self.rules.get("batch_retry_wait", 65) or 0)
                if attempt == 1 and wait > 0:
                    log.warning("批量取价失败（%s），%.0f 秒后重试一次", exc, wait)
                    import time
                    time.sleep(wait)
                else:
                    log.warning("批量取价失败（%s），改用官网快照", exc)
                    prices = {}

        missing = [n for n in names if n not in prices]
        if missing:
            # 官网兜底是逐页抓取，很重：限制数量，避免高频巡检把官网请求打爆
            cap = int(self.rules.get("fallback_max_items", 8) or 0)
            if cap > 0 and len(missing) > cap:
                log.warning("批量取价缺失 %d 个，官网兜底仅取前 %d 个（其余本轮跳过）",
                            len(missing), cap)
                missing = missing[:cap]
        if missing:
            try:
                from ..datasource.steamdt_scraper import SteamDTScraper
                from ..analysis.item import _to_int
                scraper = SteamDTScraper(timeout=float(self.cfg.steamdt.get("timeout", 15)), retry=1)
                gap = float(self.rules.get("interval_seconds", 1.0))
                for n in missing:
                    try:
                        detail = scraper.item_detail(n)
                        # 官网页面的 sellingPriceList 同样带 platform 字段（小写），
                        # 因此两条路径可以用同一套「按平台取基准价」的逻辑。
                        # 注意：个别平台的 price 是嵌套对象（引用未展开），
                        # platform_prices 会把它剔除——历史上它曾被解析成 17.9 亿的时间戳。
                        price, basis, platforms = pick_price(detail.get("sellingPriceList"), "price")
                        if price is not None:
                            prices[n] = {"price": price, "price_basis": basis,
                                         "platforms": platforms,
                                         "sell_count": _to_int(detail.get("transactionCount")) or 0,
                                         "count_kind": "transactions",
                                         "source": "官网单品快照"}
                    except Exception as exc:
                        log.debug("官网取价失败 %s: %s", n, exc)
                    import time
                    time.sleep(gap)
            except Exception as exc:
                log.warning("官网快照取价不可用：%s", exc)
        return prices

    # ---------------- 判定 ----------------
    def _today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _outlier_reason(name: str, price: float, last: float | None,
                        yesterday: float | None, outlier_pct: float) -> str:
        """价格异常检测：与参考价偏差超过阈值则返回原因，否则返回空字符串。"""
        for label, ref in (("上次巡检", last), ("昨日", yesterday)):
            try:
                ref_val = float(ref) if ref else 0.0
            except (TypeError, ValueError):
                ref_val = 0.0
            if ref_val <= 0:
                continue
            dev = abs(price / ref_val - 1) * 100
            if dev > outlier_pct:
                return (f"{name}：现价 ¥{price:,.2f} 与{label}价 ¥{ref_val:,.2f} 相差 "
                        f"{dev:.0f}%，疑似脏数据，本次跳过")
        return ""

    def _already_sent(self, name: str, kind: str) -> bool:
        """滚动窗口去重：窗口内推过的同类信号不再重复（默认 24 小时，可配 cooldown_hours）。"""
        hours = float(self.rules.get("cooldown_hours", 24) or 0)
        if hours <= 0:
            return False
        ts = (self.state.get("sent_ts") or {}).get(f"{name}|{kind}")
        if not ts:
            return False
        try:
            sent_at = datetime.fromisoformat(str(ts))
        except ValueError:
            return False
        return datetime.now() - sent_at < timedelta(hours=hours)

    def _mark_sent(self, name: str, kind: str) -> None:
        sent = self.state.setdefault("sent_ts", {})
        sent[f"{name}|{kind}"] = datetime.now().isoformat(timespec="seconds")
        # 只保留最近 7 天的记录，避免状态文件无限膨胀
        cutoff = datetime.now() - timedelta(days=7)
        for key, ts in list(sent.items()):
            try:
                if datetime.fromisoformat(str(ts)) < cutoff:
                    sent.pop(key, None)
            except ValueError:
                sent.pop(key, None)

    # ---------------- 两次确认（治单次跳变噪音）----------------
    # 背景：2026-09-24 实测，沙鹰印花集因「HALOSKINS 那一行时有时无」，
    # 最低价在 201.69 与 218 之间横跳，19:00「破位」、21:00「涨」都是假信号。
    # 规则：跌/涨/破位三类必须**连续两次巡检都成立**才推送；
    #       建仓信号来自每日分析结论、盈亏跨档自带分档机制 → 不需要确认。
    CONFIRM_KINDS = ("跌", "涨", "破位")

    def _needs_confirm(self, kind: str) -> bool:
        return bool(self.rules.get("confirm_urgent", True)) and kind in self.CONFIRM_KINDS

    @staticmethod
    def _pending_key(name: str, kind: str) -> str:
        return f"{name}|{kind}"

    def _confirm_window(self) -> timedelta:
        try:
            minutes = float(self.rules.get("confirm_window_minutes", 90) or 90)
        except (TypeError, ValueError):
            minutes = 90.0
        return timedelta(minutes=minutes)

    def _has_pending(self, name: str, kind: str) -> bool:
        """上一次巡检是否已经见过同类信号（有效期内）。"""
        rec = (self.state.get("pending") or {}).get(self._pending_key(name, kind))
        if not rec:
            return False
        try:
            seen = datetime.fromisoformat(str(rec.get("seen") or ""))
        except ValueError:
            return False
        return datetime.now() - seen <= self._confirm_window()

    def _set_pending(self, a: Alert) -> None:
        """记下待确认信号（含触发价与完整告警，供下次巡检核对价格是否收复）。"""
        pend = self.state.setdefault("pending", {})
        key = self._pending_key(a.name, a.kind)
        rec = pend.setdefault(key, {"count": 0})
        rec["seen"] = datetime.now().isoformat(timespec="seconds")
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["price"] = a.price
        rec["alert"] = a.__dict__
        cutoff = datetime.now() - self._confirm_window() * 3
        for k, v in list(pend.items()):
            try:
                if datetime.fromisoformat(str(v.get("seen"))) < cutoff:
                    pend.pop(k, None)
            except (ValueError, AttributeError):
                pend.pop(k, None)

    def _pending_valid(self, rec: dict) -> bool:
        try:
            seen = datetime.fromisoformat(str(rec.get("seen") or ""))
        except ValueError:
            return False
        return datetime.now() - seen <= self._confirm_window()

    @staticmethod
    def _still_holds(kind: str, now_price: float, trigger: float, tol: float) -> bool:
        """触发条件是否仍然成立（关键：跌势要「没被收复」，而不是「再次触发」）。

        价格从 100 跌到 96 后横住，「较上次检查」自然不再触发——
        若按「同一信号再次出现」来确认，就永远确认不了。所以按价格水平判断。
        """
        if trigger <= 0 or now_price <= 0:
            return False
        if kind == "跌":
            return now_price <= trigger * (1 + tol)
        if kind == "涨":
            return now_price >= trigger * (1 - tol)
        # 破位是水平判定：只要还在支撑下方，下一轮会自然重新触发，交给正常流程
        return False

    def _pending_recheck(self, prices: dict[str, dict],
                         fresh_keys: set[str]) -> list[Alert]:
        """核对待确认记录 → 返回已验证的告警（价格未收复）。

        fresh_keys：本轮重新触发了的 key，这些交给正常流程，避免重复。
        """
        out: list[Alert] = []
        try:
            tol = float(self.rules.get("confirm_tolerance_pct", 1.0) or 1.0) / 100
        except (TypeError, ValueError):
            tol = 0.01
        for key, rec in list((self.state.get("pending") or {}).items()):
            if key in fresh_keys or not self._pending_valid(rec):
                continue
            name, _, kind = key.rpartition("|")
            cur = prices.get(name)
            trigger = rec.get("price")
            if not cur or not trigger:
                continue
            if not self._still_holds(kind, float(cur.get("price") or 0), float(trigger), tol):
                log.info("待确认信号已失效（价格收复）：%s（%s）", name, kind)
                continue
            data = dict(rec.get("alert") or {})
            if not data:
                continue
            seen = str(rec.get("seen") or "")[11:16]
            data["detail"] = (f"{data.get('detail', '')}　✅ 二次确认：自 {seen} 起未收复")
            out.append(Alert(**{k: v for k, v in data.items()
                                if k in Alert.__dataclass_fields__}))
        return out

    def _clear_pending(self, name: str, kind: str) -> None:
        (self.state.get("pending") or {}).pop(self._pending_key(name, kind), None)

    # ---------------- 分级推送辅助 ----------------
    def _is_urgent(self, a: Alert) -> bool:
        """是否属于「值得立刻打扰」的紧急信号（其余进汇总，不推送）。"""
        r = self.rules
        if a.kind in ("建仓信号", "破位", "盈亏"):
            return True
        if a.kind == "跌":
            return a.pct is not None and a.pct <= -float(r.get("urgent_drop_pct", 4.0))
        if a.kind == "涨":
            return a.pct is not None and a.pct >= float(r.get("urgent_rise_pct", 6.0))
        return False        # 在售量等 → 温和

    def _flood_blocked(self, kind: str) -> bool:
        """防轰炸闸门：跌/涨每天最多各推 N 条；建仓信号/破位/盈亏永不折叠。"""
        if kind not in ("跌", "涨"):
            return False
        cap = int(self.rules.get("urgent_max_per_kind_per_day", 3) or 0)
        if cap <= 0:
            return False
        rec = (self.state.get("urgent_by_day") or {}).get(self._today_key(), {})
        return int(rec.get(kind, 0)) >= cap

    def _mark_urgent(self, kind: str) -> None:
        by_day = self.state.setdefault("urgent_by_day", {})
        rec = by_day.setdefault(self._today_key(), {})
        rec[kind] = int(rec.get(kind, 0)) + 1
        for old in sorted(by_day.keys())[:-7]:
            by_day.pop(old, None)

    def _buffer_mild(self, alerts: list[Alert]) -> None:
        buf = self.state.setdefault("mild", [])
        now = datetime.now().strftime("%H:%M")
        for a in alerts:
            buf.append({"time": now, "name": a.name, "name_cn": a.name_cn, "kind": a.kind,
                        "detail": a.detail, "price": a.price, "pct": a.pct,
                        # 持仓身份必须一起进缓冲：12:00/22:00 概览的 AI 点评靠这几个字段
                        # 判断「这件是不是我的持仓」。原先只存上面 6 个字段，重建出来全是
                        # 「非持仓」——2026-10-06 实测：7 条持仓被点评说成「全是观察标的、
                        # 无持仓，无需止损」，等于把真实持仓风险说没了（严重误导）。
                        "qty": a.qty, "cost": a.cost, "pnl_pct": a.pnl_pct,
                        "source": a.source, "buy_date": a.buy_date, "unlocked": a.unlocked})
        del buf[:-80]        # 只保留最近 80 条

    def _clear_mild(self) -> None:
        self.state["mild"] = []

    @staticmethod
    def _buffered_to_alerts(records: list[dict]) -> list[Alert]:
        # 兼容旧缓冲记录：缺 qty/cost 等字段时取默认值（0/空），行为与从前一致
        return [Alert(name=r.get("name", ""), name_cn=r.get("name_cn", ""),
                      kind=r.get("kind", ""), detail=r.get("detail", ""),
                      price=r.get("price"), pct=r.get("pct"),
                      qty=int(r.get("qty") or 0), cost=float(r.get("cost") or 0.0),
                      pnl_pct=r.get("pnl_pct"), source=r.get("source", ""),
                      buy_date=r.get("buy_date", ""),
                      unlocked=bool(r.get("unlocked", False)))
                for r in (records or [])]

    def _summary_due(self, now: datetime) -> bool:
        times = self.rules.get("summary_times") or [12, 22]
        try:
            return int(now.hour) in {int(t) for t in times}
        except (TypeError, ValueError):
            return False

    def _summary_span(self) -> str:
        """本次概览覆盖的时间段（上次汇总之后）。"""
        now = datetime.now()
        last = str(self.state.get("last_summary") or "")
        start = last[11:16] if len(last) >= 16 else "09:00"
        return f"{start}–{now:%H:%M}"

    # ---------------- 日内价格曲线 ----------------
    def save_intraday(self, prices: dict[str, dict], skip: set[str] | None = None) -> None:
        """把每次巡检的取价落盘，积累日内价格序列（供日后挂单执行/波动率统计）。

        skip = 本轮被判定为脏数据/异常的标的：必须排除，否则曲线会被
        17.9 亿这种解析错位值污染（2026-09-24 实际发生过）。
        platforms 也一并落盘——排查「价格横跳」时就是靠它定位到平台行的。
        """
        try:
            rec_prices: dict[str, float] = {}
            rec_counts: dict[str, int] = {}
            rec_plat: dict[str, dict] = {}
            for n, p in prices.items():
                if skip and n in skip:
                    continue
                if p.get("price"):
                    rec_prices[n] = p["price"]
                if p.get("sell_count"):
                    rec_counts[n] = p["sell_count"]
                if p.get("platforms"):
                    rec_plat[n] = p["platforms"]
            if not rec_prices:
                log.debug("本轮取价全部被判为异常，日内曲线不落盘")
                return
            root = self.cfg.data_dir() / "intraday"
            root.mkdir(parents=True, exist_ok=True)
            rec = {"time": datetime.now().strftime("%H:%M"),
                   "prices": rec_prices, "counts": rec_counts}
            if rec_plat:
                rec["platforms"] = rec_plat
            with (root / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            keep = int(self.rules.get("intraday_keep_days", 180) or 180)
            cutoff = datetime.now() - timedelta(days=keep)
            for f in root.glob("*.jsonl"):
                try:
                    if datetime.strptime(f.stem, "%Y-%m-%d") < cutoff:
                        f.unlink()
                except ValueError:
                    continue
        except OSError as exc:
            log.debug("日内价格落盘失败：%s", exc)

    def evaluate(self, items: list[dict], prices: dict[str, dict]) -> list[Alert]:
        """按规则判定，返回去抖后的告警列表。"""
        r = self.rules
        alerts: list[Alert] = []
        # 本轮每个成功取价标的的体检行（供 12:00/22:00 概览的 Word 用）。
        # 以前这些数据算完就丢了，文字概览只能写"触发了异动的那几条"，用户觉得太短。
        self.item_table: list[dict] = []
        self.suspicious: list[str] = []
        self.suspicious_names: set[str] = set()
        last_prices = self.state.get("last_prices") or {}
        last_basis = self.state.get("last_price_basis") or {}
        last_counts = self.state.get("last_counts") or {}
        last_counts_kind = self.state.get("last_counts_kind") or {}
        y_prices = self._yesterday_prices()
        outlier_pct = float(r.get("outlier_pct", 60.0))

        for it in items:
            name = it["name"]
            cur = prices.get(name)
            if not cur:
                continue
            price = float(cur["price"])
            cn = self._cn(name)
            # 脏数据防护：与「上次巡检价」或「昨日价」偏差过大 → 视为异常，跳过（不更新基准、不告警）
            reason = self._outlier_reason(name, price, last_prices.get(name),
                                          y_prices.get(name), outlier_pct)
            if reason:
                self.suspicious.append(reason)
                self.suspicious_names.add(name)
                log.warning("跳过疑似脏数据：%s", reason)
                continue
            cost = float(it.get("cost") or 0)
            qty = int(it.get("qty") or 0)
            pnl = (price / cost - 1) * 100 if cost > 0 else None
            # 价源一致性：上次与本次的基准平台不同（如悠悠缺失退成中位数）
            # → 「较上次检查」的涨跌没有可比性，跳过这两条规则
            cur_basis = str(cur.get("price_basis") or "")
            prev_basis = str(last_basis.get(name) or "")
            basis_ok = (not cur_basis) or (not prev_basis) or cur_basis == prev_basis
            basis_note = f"（价源：{cur_basis}）" if cur_basis and cur_basis != BASIS_LABEL else ""

            def add(kind: str, detail: str, pct: float | None = None,
                    use_cooldown: bool = True) -> None:
                # use_cooldown=False 用于「盈亏分档」这类自带去重机制的信号；
                # 有 pending 记录时允许再次生成——否则永远无法完成「两次确认」
                if use_cooldown and self._already_sent(name, kind) \
                        and not self._has_pending(name, kind):
                    return
                if basis_note:
                    detail = f"{detail}{basis_note}"
                alert = Alert(name=name, name_cn=cn, kind=kind, detail=detail,
                              price=price, pct=pct, qty=qty, cost=cost, pnl_pct=pnl,
                              source=str(it.get("source") or ""),
                              buy_date=str(it.get("buy_date") or ""),
                              unlocked=bool(it.get("unlocked")))
                alerts.append(alert)
                self._mark_sent(name, kind)
                if self._needs_confirm(kind):
                    self._set_pending(alert)

            # 1) 相比上次检查（只在价源一致时比较，否则是拿两个平台的价在比）
            chg_last: float | None = None
            chg_yest: float | None = None
            prev = last_prices.get(name)
            if prev and basis_ok:
                chg = (price / float(prev) - 1) * 100
                chg_last = chg
                if chg <= -float(r["drop_pct_vs_last"]):
                    add("跌", f"现价 ¥{price:,.2f}，较上次检查（¥{float(prev):,.2f}）**{chg:+.2f}%**",
                        pct=chg)
                elif chg >= float(r["rise_pct_vs_last"]):
                    add("涨", f"现价 ¥{price:,.2f}，较上次检查（¥{float(prev):,.2f}）**{chg:+.2f}%**",
                        pct=chg)
            elif prev and not basis_ok:
                log.debug("价源变化（%s→%s），跳过「较上次检查」比较：%s",
                          prev_basis, cur_basis, name)

            # 2) 相比昨日
            yp = y_prices.get(name)
            if yp and yp > 0:
                chg = (price / yp - 1) * 100
                chg_yest = chg
                if chg <= -float(r["drop_pct_vs_yesterday"]):
                    add("跌", f"现价 ¥{price:,.2f}，较昨日（¥{yp:,.2f}）**{chg:+.2f}%**", pct=chg)
                elif chg >= float(r["rise_pct_vs_yesterday"]):
                    add("涨", f"现价 ¥{price:,.2f}，较昨日（¥{yp:,.2f}）**{chg:+.2f}%**", pct=chg)

            # 3) 在售量骤变（抛压 / 扫货）——只有口径一致时才比较
            #    批量接口=挂单量，官网兜底=当日成交数；两者混用会造出「-99% 被扫货」的假警报
            prev_c = last_counts.get(name)
            cur_c = cur.get("sell_count")
            cur_kind = str(cur.get("count_kind") or "listings")
            prev_kind = (self.state.get("last_counts_kind") or {}).get(name)
            if prev_c and cur_c and prev_kind == cur_kind:
                chg = (float(cur_c) / float(prev_c) - 1) * 100
                if abs(chg) >= float(r["sell_count_change_pct"]):
                    if cur_kind == "listings":
                        word = "挂单骤增（抛压）" if chg > 0 else "挂单骤减（被扫货）"
                        metric = "在售量"
                        unit = "件"
                    else:
                        word = "当日成交数骤增" if chg > 0 else "当日成交数骤减"
                        metric = "成交量"
                        unit = "笔"
                    add(metric, f"{word}：{int(prev_c)} → {int(cur_c)} {unit}（**{chg:+.0f}%**）")
            elif prev_c and cur_c and prev_kind != cur_kind:
                log.debug("口径不同或缺标记（%s→%s），跳过在售量比较：%s",
                          prev_kind, cur_kind, name)

            # 4) 盈亏阈值（按 10 个百分点分档，只在跨入新档位时提醒，避免每天重复轰炸）
            if pnl is not None and qty > 0:
                bands = self.state.setdefault("pnl_bands", {}).setdefault(name, {})
                if pnl <= -float(r["loss_alert_pct"]):
                    band = int(abs(pnl) // 10) * 10
                    if band > int(bands.get("loss", 0)):
                        bands["loss"] = band
                        add("盈亏", f"亏损扩大到 **{pnl:.1f}%**（成本 ¥{cost:,.2f}，{qty} 件，"
                                   f"浮亏 ¥{(price - cost) * qty:,.0f}）", use_cooldown=False)
                elif pnl >= float(r["profit_alert_pct"]):
                    band = int(pnl // 10) * 10
                    if band > int(bands.get("profit", 0)):
                        bands["profit"] = band
                        add("盈亏", f"盈利达到 **{pnl:+.1f}%**（成本 ¥{cost:,.2f}，{qty} 件），"
                                   "可考虑分批止盈", use_cooldown=False)

            # 5) 跌破支撑
            sup = it.get("support")
            if r.get("break_support") and sup:
                try:
                    sup = float(sup)
                    if price < sup * 0.99:
                        add("破位", f"现价 ¥{price:,.2f} 跌破最近支撑 ¥{sup:,.2f}（**{(price/sup-1)*100:+.1f}%**）")
                except (TypeError, ValueError):
                    pass

            # 6) 建仓信号：系统当日给出「买入/加仓」建议 → 第一时间提醒（这是可执行的买点）
            if r.get("signal_alert", True) and _is_buy_signal(it.get("action")):
                action = str(it.get("action") or "")
                sc = it.get("score")
                sc_txt = f"评分 {float(sc):+.1f}" if isinstance(sc, (int, float)) else ""
                add("建仓信号",
                    f"系统建议「{action}」（{sc_txt}）——现价 ¥{price:,.2f} 已进入买点区间；"
                    "模拟盘会在当日 17:30 复核后按规则执行，实盘可自行决定是否跟进（T+7 锁定）")

            last_prices[name] = price
            last_basis[name] = cur_basis or prev_basis
            # 体检行：概览 Word 用（含非持仓的关注标的，方便一眼看清整个监控面）
            self.item_table.append({
                "name": name,
                "display": f"{cn}（{name}）" if cn and cn != name else name,
                # 概览 Word 的表格用短名（只有中文名）——手机上读、省行数；
                # 日报里仍然是「中文名（英文名）」的完整写法。
                "cn": cn or name,
                "price": price, "chg_last": chg_last, "chg_yest": chg_yest,
                "qty": qty, "cost": cost, "pnl_pct": pnl,
                "support": it.get("support"), "action": it.get("action"),
                "score": it.get("score"), "source": str(it.get("source") or ""),
                "unlocked": bool(it.get("unlocked")),
            })
            if cur.get("sell_count"):
                last_counts[name] = cur["sell_count"]
                last_counts_kind[name] = cur_kind

        self.state["last_prices"] = last_prices
        self.state["last_price_basis"] = last_basis
        self.state["last_counts"] = last_counts
        self.state["last_counts_kind"] = last_counts_kind
        self._save_state()
        return alerts

    def _yesterday_prices(self) -> dict[str, float]:
        """昨日价格快照（data/snapshots/）里按同一基准（悠悠有品）取价。

        快照存的是多平台原始报价，所以这里也用 pick_price 重算一遍——
        否则「今日悠悠价 vs 昨日全平台最低价」本身就是两个口径，会造出假涨跌。
        """
        out: dict[str, float] = {}
        snap_dir = self.cfg.data_dir() / "snapshots"
        files = sorted(snap_dir.glob("*_prices.json")) if snap_dir.exists() else []
        for f in reversed(files[:-1] if len(files) > 1 else files):  # 优先最近的非当天快照
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for name, rows in data.items():
                if name.startswith("_") or not isinstance(rows, list):
                    continue
                price, _basis, _plat = pick_price(rows, "sellPrice")
                if price is not None:
                    out[name] = price
            if out:
                break
        return out

    def _cn(self, name: str) -> str:
        try:
            cache = self.cfg.data_dir() / "name_map.json"
            if cache.exists():
                data = json.loads(cache.read_text(encoding="utf-8"))
                return (data.get("map") or {}).get(name, "")
        except (json.JSONDecodeError, OSError):
            pass
        return ""

    # ---------------- AI 点评 ----------------
    def _market_note(self) -> str:
        """当日大盘一句话（取最新分析快照，失败返回空字符串）。"""
        try:
            snap_files = sorted((self.cfg.data_dir() / "analysis").glob("*.json"))
            if snap_files:
                m = (json.loads(snap_files[-1].read_text(encoding="utf-8")).get("market") or {})
                return (f"大盘：{m.get('趋势')} / {m.get('风险等级')}"
                        f"｜量能 {m.get('量能')}｜情绪分 {m.get('情绪分')}")
        except Exception:
            pass
        return ""

    def _position_state(self, a: Alert) -> str:
        """把「这件是不是我的持仓、锁没锁、套了多少」压成一句话，喂给 AI 点评。

        为什么要有它：AI 点评原先只拿到「名称 + 异动描述」，不知道哪条是用户真实持有的，
        于是只能写「持仓者：若已过 T+7…若仍在锁定期…」这种两头都说的模板话。
        这里把持仓件数、成本、当前盈亏、T+7 锁定期状态一次性给它。
        """
        if a.qty <= 0 or a.cost <= 0:
            return "非持仓（观察标的；注意买入后 7 天内不能卖）"
        pnl = f"，现价对成本 {a.pnl_pct:+.1f}%" if a.pnl_pct is not None else ""
        lock = "锁定期未知（用户未记录买入日期）"
        if a.buy_date:
            try:
                until = datetime.strptime(a.buy_date, "%Y-%m-%d").date() + timedelta(days=7)
                lock = ("锁定期已过，可以卖" if until <= datetime.now().date()
                        else f"仍在锁定期，{until:%Y-%m-%d} 起才可卖")
            except ValueError:
                lock = "买入日期格式无法解析"
        elif a.unlocked:
            # 用户明确说过"手上的这些早就过了七天"（2026-09-30），不必再让他逐件补日期
            lock = "锁定期已过，可以卖（用户已确认）"
        return f"我的持仓（{a.qty} 件，成本 ¥{a.cost:,.2f}{pnl}；{lock}）"

    def ai_comment(self, alerts: list[Alert]) -> str:
        if not self.rules.get("ai_commentary") or not alerts:
            return ""
        try:
            from ..llm.client import LLMClient
            llm = LLMClient(self.cfg.llm, profile="alert")   # 短评，延迟优先（不思考）
            if not llm.enabled:
                return ""
            facts = "\n".join(
                f"- {a.name_cn or a.name}（{a.kind}）：{a.detail}"
                f"｜这件对我的状态：{self._position_state(a)}"
                for a in sorted(alerts, key=lambda x: x.kind != "建仓信号")[:10])
            market_note = self._market_note()
            from ..rules import trading_rules_text
            rules = trading_rules_text(self.cfg.sim)
            prompt = (f"以下是 CS2 饰品持仓/关注标的的价格异动（{datetime.now():%Y-%m-%d %H:%M}）：\n"
                      f"{facts}\n\n{market_note}\n\n"
                      f"【{rules}】\n\n"
                      "请用 150 字以内给出点评：①这些异动可能是什么原因（区分「普跌/普涨」与"
                      "「单品独立异动」）；②对我（用户本人）的含义——该关注什么、什么情况下需要行动。"
                      "条目多且方向一致时，直接判为普跌/普涨并说明成因，不必逐条点评。"
                      "**每一条都已经标注了『这件对我的状态』：是我的持仓（含件数、成本、当前盈亏、"
                      "T+7 锁定期是否已过）还是观察标的。请严格按该状态给结论——是我的持仓就说"
                      "「你持有 X 件、成本 ¥Y、当前浮亏 Z%」，不要写「若你是持仓者…若不是…」这类"
                      "两头都说的模板话；如果状态是「锁定期未知」，就直接说「你把买入日期告诉我，"
                      "我才能算出可售日」。**"
                      "若清单里有「建仓信号」，请优先说明这个买点是否成立（价格位置、支撑距离、大盘配合度），"
                      "以及建仓后 7 天不能卖的风险点。"
                      "给出任何操作建议时必须把上述 T+N 锁定期与手续费算进去"
                      "（例如：提示「可以卖」要说明锁定期是否已过、扣除手续费后是否划算；"
                      "对未持仓的关注标的，买入意味着 7 天内不能止损）。"
                      "只基于我给的数据，不要编造价格；不确定的地方明说。不要用「投资建议」这类套话。")
            # max_tokens 交给 alert profile（原先是硬编码 400，一旦开思考会被思考 token 吃光）
            text = llm.chat([{"role": "user", "content": prompt}])
            return text.strip()
        except Exception as exc:
            log.warning("AI 点评失败：%s", exc)
            return ""

    # ---------------- 主流程 ----------------
    def run(self, push: bool = True) -> AlertRun:
        """一次巡检：取价 → 判定 → 分级（紧急立即推 / 温和进汇总）。"""
        now = datetime.now()
        run = AlertRun(time=now.strftime("%Y-%m-%d %H:%M"))
        log.info("=" * 50)
        log.info("价格异动巡检开始：%s", run.time)
        if not self.rules.get("enabled", True):
            run.error = "告警功能未启用"
            return run
        try:
            items = self.collect_items()
            prices = self.fetch_prices([i["name"] for i in items])
            run.checked = len(prices)
            run.expected = len(items)
            run.skipped = [i["name"] for i in items if i["name"] not in prices]
            if run.checked < run.expected:
                log.warning("本轮覆盖不完整：%d/%d 个标的取价成功（%d 个失败）",
                            run.checked, run.expected, len(run.skipped))
            pending_before = set((self.state.get("pending") or {}).keys())
            run.alerts = self.evaluate(items, prices)
            run.suspicious = list(getattr(self, "suspicious", []))
            if run.suspicious:
                log.warning("本次跳过 %d 个疑似脏数据：%s", len(run.suspicious),
                            "；".join(run.suspicious))
            # 落盘放在脏数据校验之后：被判为异常的价格绝不能进日内曲线
            self.save_intraday(prices, getattr(self, "suspicious_names", None) or set())

            # ---- 两次确认：核对上轮记下的待确认信号（价格未收复 → 确认成立）----
            fresh = list(run.alerts)
            fresh_keys = {self._pending_key(a.name, a.kind) for a in fresh}
            confirmed = self._pending_recheck(prices, fresh_keys)
            run.alerts = fresh + confirmed

            # ---- 分级：紧急的立即推，温和的静默记录 ----
            urgent: list[Alert] = []
            mild: list[Alert] = []
            for a in confirmed:
                self._clear_pending(a.name, a.kind)   # 确认过一次就不再重复确认
                if self._is_urgent(a) and not self._flood_blocked(a.kind):
                    urgent.append(a)
                    self._mark_urgent(a.kind)
                    log.info("二次确认成立并推送：%s（%s）", a.name_cn or a.name, a.kind)
                else:
                    mild.append(a)
            for a in fresh:
                key = self._pending_key(a.name, a.kind)
                if self._needs_confirm(a.kind) and key not in pending_before:
                    # 首次出现 → 只记待确认，不打扰（单次跳变大概率是噪音）
                    if self._is_urgent(a):
                        a.detail = f"{a.detail}　⏳ 待下次巡检确认（防单次跳变）"
                    mild.append(a)
                    log.info("首次出现，待确认后再推：%s（%s）", a.name_cn or a.name, a.kind)
                    continue
                if self._needs_confirm(a.kind):
                    log.info("二次确认成立：%s（%s）", a.name_cn or a.name, a.kind)
                if self._is_urgent(a) and not self._flood_blocked(a.kind):
                    urgent.append(a)
                    self._mark_urgent(a.kind)
                    if self._needs_confirm(a.kind):
                        self._clear_pending(a.name, a.kind)
                else:
                    mild.append(a)
                    if self._is_urgent(a):
                        log.info("「%s」今日紧急推送已达上限，折叠进下次汇总：%s",
                                 a.kind, a.name_cn or a.name)
            run.urgent, run.mild = urgent, mild
            summary_due = self._summary_due(now)
            if mild:
                self._buffer_mild(mild)

            self.state["last_run"] = run.time
            self._save_state()

            if not urgent and not summary_due:
                log.info("巡检完成：%d 个标的｜紧急 0 条｜温和 %d 条（静默记录，不推送）",
                         run.checked, len(mild))
                self._archive(run)
                return run

            if summary_due:
                buffered = self._buffered_to_alerts(self.state.get("mild") or [])
                if len(urgent) + len(buffered) >= 3:      # 内容太少就不花模型钱了
                    run.ai_comment = self.ai_comment(urgent + buffered)
                run.mode = "summary"
                text = self._compose_summary_message(run, urgent, buffered)
                log.info("盘中概览：紧急 %d 条 / 温和 %d 条", len(urgent), len(buffered))
            else:
                run.ai_comment = self.ai_comment(urgent)
                run.mode = "alert"
                text = self._compose_alert_message(run, urgent)
                log.info("紧急推送 %d 条：%s", len(urgent),
                         "；".join(f"{a.name_cn or a.name}({a.kind})" for a in urgent))

            if push:
                self._send(text)
                if summary_due:
                    # 概览再补一份 Word：文字只能写触发了异动的几条，
                    # Word 里是全部监控标的的体检表（用户要求「概览也要 Word」）
                    self._send_summary_docx(run, urgent, buffered)
                    self._clear_mild()
                    self.state["last_summary"] = run.time
                    self._save_state()
            else:
                # 干跑：不记 mode（周报的「打扰次数」只看真正发出去的），但保留原文供预览
                run.mode = ""
                log.info("[未推送] 首行：%s", (text.splitlines() or [""])[0])
            run.message = text
            self._archive(run)
            return run
        except Exception as exc:
            run.error = str(exc)
            log.error("巡检失败：%s", exc)
            return run

    # ---------------- 推送文案 ----------------
    @staticmethod
    def _alert_title(urgent: list[Alert]) -> str:
        if any(a.kind == "建仓信号" for a in urgent):
            return "🟢 出现建仓信号"
        if any(a.kind == "破位" for a in urgent):
            return "⚠️ 跌破支撑"
        return "⚠️ 价格异动提醒"

    def _compose_alert_message(self, run: AlertRun, urgent: list[Alert]) -> str:
        lines = [f"**{self._alert_title(urgent)} · {datetime.now():%H:%M}**", ""]
        # 建仓信号置顶，其余按原顺序
        ordered = ([a for a in urgent if a.kind == "建仓信号"] +
                   [a for a in urgent if a.kind != "建仓信号"])
        for a in ordered[:12]:
            lines.append(a.to_line())
        if len(urgent) > 12:
            lines.append(f"…另有 {len(urgent) - 12} 条")
        if run.ai_comment:
            lines += ["", "**🤖 AI 点评**", f"> {run.ai_comment[:600]}"]
        if any(a.kind == "建仓信号" for a in urgent):
            lines += ["", "> 模拟盘会在当日 17:30 复核行情后按规则执行（规则化执行，非盘中成交）；"
                          "若希望改成盘中挂单成交、更贴近实盘买点，随时告知即可升级。"]
        if run.mild:
            lines += ["", f"（另有 {len(run.mild)} 条温和波动已记录，将在 12:00 / 22:00 的盘中概览里汇总）"]
        lines += ["", self._footer(run)]
        return "\n".join(lines)

    def _compose_summary_message(self, run: AlertRun, urgent: list[Alert],
                                 buffered: list[Alert]) -> str:
        """盘中概览：把静默记录的温和波动一次交代清楚（每天 12:00 与 22:00）。"""
        lines = [f"**📊 盘中概览 · {datetime.now():%H:%M}**"
                 f"（覆盖 {self._summary_span()}）", ""]
        market = self._market_note()
        if market:
            lines += [market, ""]
        if urgent:
            lines.append("**🔔 已即时推送**")
            for a in urgent[:6]:
                lines.append(a.to_line())
            lines.append("")
        lines.append("**📋 温和波动（静默记录，未打扰你）**")
        if buffered:
            shown = buffered[-10:]
            for a in shown:
                lines.append(f"{a.to_line()}　`{self._time_of(a)}`")
            if len(buffered) > len(shown):
                lines.append(f"…另有 {len(buffered) - len(shown)} 条")
        else:
            lines.append("无——这段时间所有标的价格平稳。")
        if run.ai_comment:
            lines += ["", "**🤖 AI 点评**", f"> {run.ai_comment[:600]}"]
        lines += ["", "> 完整「持仓体检表」见下方附件（Word）：全部监控标的的现价、"
                      "较上次/昨日涨跌、我的成本与浮亏、距支撑距离。"]
        lines += ["", self._footer(run)]
        return "\n".join(lines)

    def _send_summary_docx(self, run: AlertRun, urgent: list[Alert],
                           buffered: list[Alert]) -> None:
        """把「盘中概览」渲染成 Word 并作为附件推送。

        用户反馈：概览只有一段文字、太短。文字受篇幅限制只能写触发了异动的几条，
        而巡检本来就算过全部标的的现价/涨跌/成本/浮亏/距支撑——这里把它落成体检表。
        任何失败都只记日志：**文字推送已经发出，绝不能因为附件问题回滚或报错**。
        """
        try:
            from ..notify.wecom import send_file
            from ..report.docx_summary import build_summary_docx

            rows = list(getattr(self, "item_table", []) or [])
            if not rows:
                log.info("概览 Word 跳过：本轮没有体检数据")
                return
            holdings = [x for x in rows if x.get("qty") and x.get("cost")]
            watch = [x for x in rows if not (x.get("qty") and x.get("cost"))]
            total_cost = sum(float(x["cost"]) * int(x["qty"]) for x in holdings)
            total_val = sum(float(x["price"]) * int(x["qty"]) for x in holdings)
            portfolio = {
                "总成本": round(total_cost, 2), "总市值": round(total_val, 2),
                "盈亏%": round((total_val / total_cost - 1) * 100, 2) if total_cost else 0.0,
            }
            now = datetime.now()
            out = self.cfg.report_dir() / f"盘中概览_{now:%Y-%m-%d_%H时}.docx"
            # 温和波动最多列 10 条：列全了 Word 会拉到三四页，对「概览」来说太重
            mild_shown = buffered[-10:]
            mild_lines = [f"{a.name_cn or a.name}（{a.kind}）：{a.detail}" for a in mild_shown]
            if len(buffered) > len(mild_shown):
                mild_lines.append(f"…另有 {len(buffered) - len(mild_shown)} 条温和波动（见日志）")
            build_summary_docx(
                out, run_time=run.time, checked=run.checked, expected=run.expected,
                market_line=self._market_note(), portfolio=portfolio,
                holdings=holdings, watchlist=watch,
                urgent_lines=[f"{a.name_cn or a.name}：{a.detail}" for a in urgent],
                mild_lines=mild_lines,
                ai_text=run.ai_comment or "", footer=self._footer(run))
            hook = str(self.cfg.notify.get("wecom_webhook") or "").strip()
            if not hook:
                log.info("概览 Word 已生成（未配置企业微信地址，不推送）：%s", out.name)
                return
            ok = send_file(hook, out)
            log.info("概览 Word %s：%s（%.0f KB）", "已推送" if ok else "推送失败",
                     out.name, out.stat().st_size / 1024)
        except Exception as exc:  # noqa: BLE001
            log.warning("概览 Word 生成/推送失败（文字推送不受影响）：%s", exc)

    def _time_of(self, a: Alert) -> str:
        """缓冲记录里的时间（概览里标出发生时刻）。"""
        for rec in (self.state.get("mild") or []):
            if rec.get("name") == a.name and rec.get("kind") == a.kind:
                return str(rec.get("time") or "")
        return ""

    def _footer(self, run: AlertRun) -> str:
        # 覆盖不完整时必须写明，否则用户会以为「没消息 = 全部正常」
        if run.expected and run.checked < run.expected:
            cover = f"本次仅覆盖 {run.checked}/{run.expected} 个标的（{run.expected - run.checked} 个取价失败）"
        else:
            cover = f"本次巡检 {run.checked} 个标的"
        parts = [cover]
        try:
            from ..rules import trading_rules_brief
            brief = trading_rules_brief(self.cfg.sim)
            if brief:
                parts.append(brief)
        except Exception:
            pass
        parts.append("价格基准：悠悠有品（你实际交易的平台）")
        parts.append("9:00–24:00 每小时巡检；紧急信号即时推送，12/22 点盘中概览")
        return "（" + "；".join(parts) + "）"

    def _send(self, text: str) -> bool:
        try:
            from ..notify.wecom import send
            hook = str(self.cfg.notify.get("wecom_webhook") or "").strip()
            if not hook:
                log.warning("未配置推送地址，跳过推送")
                return False
            return send(text, hook)
        except Exception as exc:
            log.warning("告警推送失败：%s", exc)
            return False

    def _archive(self, run: AlertRun) -> None:
        try:
            path = self.root / f"{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "time": run.time, "checked": run.checked,
                    "alerts": [a.__dict__ for a in run.alerts],
                    "urgent": [a.__dict__ for a in run.urgent],
                    "mild": [a.__dict__ for a in run.mild],
                    "mode": run.mode,
                    "ai_comment": run.ai_comment, "error": run.error,
                    "skipped": run.skipped,
                }, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("告警存档失败：%s", exc)
