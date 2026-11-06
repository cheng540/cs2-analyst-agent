"""网页问答的工具层（AI 只能通过这里的函数取数，禁止凭记忆报价）。

设计原则（防幻觉）：
1. 所有事实（价格/指标/盈亏）都由这里用真实接口与 pandas 计算得出；
2. 模型只能看到工具返回的 JSON，回答时必须引用其中的数字；
3. 取不到数据时返回 error，模型必须如实告知用户"查不到"，不得编造。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..analysis.indicators import (add_ma_columns, position_percentile,
                                   support_resistance, trend_judge, ma_alignment,
                                   volume_proxy)
from ..analysis.item import analyze_item
from ..config import AppConfig, PROJECT_ROOT
from ..datasource.cache import DataCache
from ..datasource.inventory import load_inventory
from ..datasource.steamdt import SteamDTClient
from ..engine.decision import decide
from ..logging_setup import get_logger

log = get_logger(__name__)

CACHE_TTL = 600  # 工具结果缓存 10 分钟，避免重复打接口


def _is_ascii_only(text: str) -> bool:
    """是否只含 ASCII（判断用户是否直接给了英文名）。"""
    return all(ord(c) < 128 for c in text) and any(c.isalpha() for c in text)


@dataclass
class ToolBox:
    """给 AI 用的工具箱。"""
    cfg: AppConfig
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict)

    # ---------------- 基础设施 ----------------
    def _cached(self, key: str, fn: Callable[[], Any], ttl: int = CACHE_TTL) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (time.time(), value)
        return value

    def _client(self) -> SteamDTClient:
        return SteamDTClient(self.cfg.steamdt)

    def _scraper(self):
        from ..datasource.steamdt_scraper import SteamDTScraper
        return SteamDTScraper(timeout=float(self.cfg.steamdt.get("timeout", 15)), retry=2)

    def _name_map_cn2en(self) -> dict[str, str]:
        """中文名 → 英文名（含模糊匹配用的辅助索引）。"""
        data = DataCache(self.cfg.data_dir()).load("name_map.json") or {}
        mapping = data.get("map") or {}
        cn2en = {cn: en for en, cn in mapping.items() if cn}
        return cn2en

    def resolve_name(self, query: str) -> tuple[str | None, list[str]]:
        """把用户输入（中文/英文/简称）解析成官方英文名。

        返回 (英文名, 候选列表)。解析不到时英文名为 None。
        匹配时会忽略空格、竖线、括号等标点，所以「AWP 二西莫夫」也能匹配
        「AWP | 二西莫夫 (战痕累累)」。
        """
        q = str(query or "").strip()
        if not q:
            return None, []
        mapping = self._name_map_cn2en()
        if not mapping:
            return (q, []) if _is_ascii_only(q) else (None, [])

        def norm(s: str) -> str:
            return re.sub(r"[\s\|\(\)（）\[\]【】\-_·、,，.。/\\:：;；'\"“”‘’]+", "",
                          str(s).lower())

        qn = norm(q)
        # 1) 规范化后的完全匹配（中文名或英文名）
        for cn, en in mapping.items():
            if norm(cn) == qn or norm(en) == qn:
                return en, []
        # 2) 包含匹配
        candidates: list[tuple[str, str]] = []
        for cn, en in mapping.items():
            if qn and (qn in norm(cn) or qn in norm(en)):
                candidates.append((cn, en))
        if len(candidates) == 1:
            return candidates[0][1], []
        if candidates:
            return None, [cn for cn, _ in candidates][:8]
        # 3) 纯英文输入 → 当作英文名直接用（可能是缓存里没有的新饰品）
        if _is_ascii_only(q):
            return q, []
        return None, []

    # ---------------- 工具 1：单品快照（官网，快，无严格限额） ----------------
    def get_item_snapshot(self, name: str) -> dict:
        en, candidates = self.resolve_name(name)
        if not en:
            return {"error": f"无法确定「{name}」对应的饰品", "candidates": candidates,
                    "提示": "请让用户在候选中确认，或用 Steam 官方英文全名（含磨损后缀）"}
        def _fetch() -> dict:
            from ..analysis.item import _to_float, _to_int
            detail = self._scraper().item_detail(en)
            prices = []
            for r in (detail.get("sellingPriceList") or []):
                if isinstance(r, dict):
                    p = _to_float(r.get("price"))
                    if p and p > 0:
                        prices.append({"平台": r.get("platformName") or r.get("platform"),
                                       "在售价": round(p, 2)})
            prices.sort(key=lambda x: x["在售价"])
            if prices:
                vals = [x["在售价"] for x in prices]
                floor = sorted(vals)[len(vals) // 2] * 0.3
                prices = [x for x in prices if x["在售价"] >= floor]
            return {
                "饰品": en,
                "中文名": detail.get("name") or "",
                "各平台在售价": prices[:6],
                "最低在售价": prices[0]["在售价"] if prices else None,
                "量比（今日成交量/近5日均量）": _to_float(detail.get("volumeRatio")),
                "当日成交数": _to_int(detail.get("transactionCount")),
                "存世量": _to_int(detail.get("surviveNum")),
                "持有人数": _to_int(detail.get("holdersNum")),
                "换手率%": _to_float(detail.get("turnoverRate")),
                "近1日涨跌%": _to_float(detail.get("diff1Day")),
                "近7日涨跌%": _to_float(detail.get("diff7Day")),
                "近30日涨跌%": _to_float(detail.get("diff30Day")),
                "近6月涨跌%": _to_float(detail.get("diff6Month")),
                "数据来源": "SteamDT 官网单品页（实时）",
            }
        try:
            return self._cached(f"snap:{en}", _fetch, ttl=300)
        except Exception as exc:
            return {"error": f"获取「{en}」快照失败：{exc}"}

    # ---------------- 工具 2：单品技术分析（K线 + 指标） ----------------
    def get_item_analysis(self, name: str) -> dict:
        en, candidates = self.resolve_name(name)
        if not en:
            return {"error": f"无法确定「{name}」对应的饰品", "candidates": candidates}
        def _fetch() -> dict:
            client = self._client()
            k = client.item_kline(en, 2)
            if k is None or k.empty or len(k) < 20:
                return {"error": f"{en} 的K线数据不足（{0 if k is None else len(k)} 根），无法做技术分析"}
            ma_windows = self.cfg.analysis["ma_windows"]
            d = add_ma_columns(k, ma_windows)
            pct, label = position_percentile(
                d["close"], self.cfg.analysis["percentile_window_days"],
                self.cfg.analysis["position_low"], self.cfg.analysis["position_high"])
            sr = support_resistance(d, self.cfg.analysis["support_lookback_days"],
                                    self.cfg.analysis["swing_window"],
                                    self.cfg.analysis["support_tolerance"],
                                    self.cfg.analysis.get("break_lookback_days", 20))
            vol = volume_proxy(k, self.cfg.analysis["volume_expand_ratio"],
                               self.cfg.analysis["volume_shrink_ratio"],
                               self.cfg.analysis["stagnation_return"])
            last = d.iloc[-1]
            return {
                "饰品": en,
                "K线根数": len(k),
                "最新收盘": round(float(last["close"]), 2),
                "均线": {f"MA{w}": (round(float(last[f'ma{w}']), 2)
                                   if last[f"ma{w}"] == last[f"ma{w}"] else None)
                        for w in ma_windows},
                "均线排列": ma_alignment(d, ma_windows),
                "趋势判定": trend_judge(d, ma_windows),
                "近6个月位置": f"{label}（百分位 {pct}%）",
                "支撑位": sr["supports"][:3],
                "压力位": sr["resistances"][:3],
                "最近支撑": sr["nearest_support"],
                "最近压力": sr["nearest_resistance"],
                "强支撑": sr["strong_support"],
                "已跌破的前支撑": sr["broken_support"],
                "量价（振幅代理）": vol["state"],
                "振幅比": vol["amp_ratio"],
                "近5日涨跌%": vol["ret_5d"],
                "近3日涨跌%": vol["ret_3d"],
                "数据来源": "SteamDT 开放平台日K + 系统指标计算",
            }
        try:
            return self._cached(f"kline:{en}", _fetch, ttl=600)
        except Exception as exc:
            return {"error": f"获取「{en}」技术分析失败：{exc}"}

    # ---------------- 工具 3：大盘环境 ----------------
    def get_market_environment(self) -> dict:
        def _fetch() -> dict:
            from ..analysis.market import analyze_market, analyze_market_stats
            from ..datasource.steamdt import parse_kline_rows
            cache = DataCache(self.cfg.data_dir())
            rows = cache.load("market/broad_kline_2.json") or []
            daily = parse_kline_rows(rows)
            out: dict[str, Any] = {}
            if not daily.empty:
                env = analyze_market(daily, daily.iloc[::5], {}, self.cfg.analysis["ma_windows"],
                                     self.cfg.analysis)
                out.update({"趋势": env.trend, "风险等级": env.risk, "量能": env.volume_state,
                            "均线排列": env.ma_state, "操作基调": env.base_tone,
                            "最新收盘": round(float(daily["close"].iloc[-1]), 2),
                            "数据日期": str(daily["date"].iloc[-1])})
            try:
                scraper = self._scraper()
                summary = scraper.market_summary()
                m = summary.get("marketIndexTrend") or {}
                perf = summary.get("transPerformanceTrend") or {}
                out["大盘指数"] = m.get("broadMarketIndex")
                out["较昨日%"] = m.get("diffYesterdayRatio")
                out["涨跌类型"] = m.get("riseFallType")
                out["连涨跌天数"] = m.get("riseFallDays")
                out["平台情绪分"] = perf.get("performanceScore")
                txn = scraper.transaction_daily()
                if txn is not None and not txn.empty:
                    from ..analysis.indicators import series_momentum_ratio
                    r = series_momentum_ratio(txn["turnover"])
                    out["真实成交额动量比"] = r.get("ratio")
                    out["成交额状态"] = r.get("state")
            except Exception as exc:
                out["官网统计获取失败"] = str(exc)
            out["数据来源"] = "开放平台日K缓存 + SteamDT 官网统计（实时）"
            return out
        try:
            return self._cached("market", _fetch, ttl=600)
        except Exception as exc:
            return {"error": f"获取大盘环境失败：{exc}"}

    # ---------------- 工具 4：我的持仓（读当日分析快照） ----------------
    def get_my_portfolio(self) -> dict:
        def _fetch() -> dict:
            cache = DataCache(self.cfg.data_dir())
            files = sorted((cache.root / "analysis").glob("*.json")) \
                if (cache.root / "analysis").exists() else []
            if files:
                snap = json.loads(files[-1].read_text(encoding="utf-8"))
                snap["数据来源"] = f"系统每日运行保存的分析快照（{snap.get('date')}）"
                snap["提示"] = "如需某件饰品的最新实时数据，请调用 get_item_snapshot / get_item_analysis"
                return snap
            # 没有快照 → 现场分析持仓（较慢）
            holdings = load_inventory(self.cfg.inventory_path())
            client = self._client()
            items = []
            total_cost = total_value = 0.0
            for h in holdings:
                try:
                    k = client.item_kline(h.market_hash_name, 2)
                    a = analyze_item(h, k, [], None, None, self.cfg.analysis, self.cfg.decision)
                    if not a.data_ok:
                        continue
                    total_cost += a.total_cost
                    total_value += a.market_value
                    items.append({"饰品": h.market_hash_name, "数量": h.quantity,
                                  "成本价": h.buy_price, "现价": a.cur_price,
                                  "盈亏%": round(a.pnl_pct * 100, 2), "趋势": a.trend,
                                  "位置": a.position_label})
                except Exception:
                    continue
            return {"持仓": items, "总成本": round(total_cost, 2),
                    "总市值": round(total_value, 2),
                    "总盈亏%": round((total_value / total_cost - 1) * 100, 2) if total_cost else None,
                    "数据来源": "现场分析（无当日快照）"}
        try:
            return self._cached("portfolio", _fetch, ttl=600)
        except Exception as exc:
            return {"error": f"读取持仓失败：{exc}"}

    # ---------------- 工具 5：模拟盘状态 ----------------
    def get_sim_status(self) -> dict:
        def _fetch() -> dict:
            out: dict[str, Any] = {}
            for label, path in (("A轨(纯规则)", self.cfg.sim_state_path()),
                                ("B轨(规则+AI)", self.cfg.sim_state_path_b())):
                try:
                    st = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    out[label] = "暂无数据"
                    continue
                journal = st.get("journal") or []
                init = float(st.get("initial_capital") or 0)
                cash = float(st.get("cash") or 0)
                net = float(journal[-1].get("net_value")) if journal else cash
                out[label] = {
                    "初始资金": init, "当前净值": net,
                    "累计收益%": round((net / init - 1) * 100, 2) if init else None,
                    "现金": cash,
                    "持仓": [{"饰品": n, "数量": p.get("qty"), "成本": p.get("avg_cost"),
                              "锁定期至": p.get("locked_until")}
                             for n, p in (st.get("positions") or {}).items()],
                    "运行天数": len(journal),
                    "最近手记": (journal[-1].get("thoughts") if journal else ""),
                }
            out["规则"] = (f"T+{self.cfg.sim.get('hold_days_min')} 交易规则；"
                           f"卖出费 {float(self.cfg.sim.get('fee_rate', 0.01)) * 100:g}%，"
                           f"钱包提现另收 {float(self.cfg.sim.get('withdraw_fee_rate', 0.01)) * 100:g}%")
            return out
        return self._cached("sim", _fetch, ttl=120)

    # ---------------- 工具 6：最新情报 ----------------
    def get_latest_intel(self) -> dict:
        def _fetch() -> dict:
            cache = DataCache(self.cfg.data_dir())
            files = sorted((cache.root / "intel").glob("*.json")) \
                if (cache.root / "intel").exists() else []
            if not files:
                return {"error": "暂无情报数据（系统每日运行时会采集）"}
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            return {
                "日期": data.get("date"),
                "官方更新与公告": [
                    {"标题": n.get("title"), "日期": n.get("date_str"),
                     "市场影响评级": n.get("market_impact"), "玩法影响评级": n.get("gameplay_impact"),
                     "摘要": str(n.get("contents") or "")[:200]}
                    for n in (data.get("news") or [])[:8]],
                "市场异动": [{"饰品": a.get("name"), "变化": a.get("summary")}
                             for a in (data.get("anomalies") or [])[:10]],
                "数据来源": "SteamDT 官网 CS2 更新记录 + 涨跌榜",
            }
        return self._cached("intel", _fetch, ttl=1800)

    # ---------------- 经验库（RAG，只用于非结构化知识） ----------------
    def search_knowledge(self, query: str) -> dict:
        """检索经验库：历史案例（当时判断 + 之后走势）、平台规则、饰品常识、官方公告。

        ⚠️ 这里**不返回任何实时价格/持仓数字**。价格与持仓只能由其他工具取，
        否则就是把"检索片段"当事实用，属项目明令禁止的幻觉来源。
        """
        from ..rag.store import search_knowledge as _search

        try:
            result = _search(PROJECT_ROOT, query, k=4, cfg_sim=self.cfg.sim)
        except Exception as exc:      # 检索失败不能让整个问答挂掉
            log.warning("经验库检索失败：%s", exc)
            return {"error": f"经验库检索失败：{exc}"}
        return result

    # ---------------- 工具注册表 ----------------
    def registry(self) -> dict[str, Callable[..., dict]]:
        return {
            "get_item_snapshot": self.get_item_snapshot,
            "get_item_analysis": self.get_item_analysis,
            "get_market_environment": self.get_market_environment,
            "get_my_portfolio": self.get_my_portfolio,
            "get_sim_status": self.get_sim_status,
            "get_latest_intel": self.get_latest_intel,
            "search_knowledge": self.search_knowledge,
        }


# ---------------- OpenAI 风格的工具声明（给模型看） ----------------
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_item_snapshot",
            "description": "查询某个 CS2 饰品的实时快照：各平台在售价、最低价、量比、当日成交、存世量、"
                           "近1/7/30日与近6月涨跌幅。支持中文名或英文名（如“蝴蝶刀 多普勒”或英文全名）。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "饰品名称（中文或英文）"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_analysis",
            "description": "对某个饰品做技术分析：均线（MA5/10/20）、趋势判定、近6个月位置百分位、"
                           "支撑压力位、是否跌破前支撑、量价状态、近3/5日涨跌。结论必须基于此数据。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "饰品名称（中文或英文）"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_environment",
            "description": "获取当前 CS2 大盘环境：趋势、风险等级、量能、大盘指数、真实成交额动量、平台情绪分。"
                           "回答“现在能不能买/该不该卖”这类问题前必须先调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_portfolio",
            "description": "读取用户当前的真实持仓与系统给出的分析（含每件饰品的成本、现价、盈亏、趋势、位置、建议）。"
                           "回答“我的持仓怎么样”必须调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sim_status",
            "description": "读取模拟盘 A/B 双轨的净值、收益、持仓与最近操盘手记（遵守 T+7 规则）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_intel",
            "description": "获取最新情报：V社/CS2 官方更新公告（含官方市场影响评级）与市场异动（涨跌榜）。"
                           "回答“这个更新对行情有什么影响”必须调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索经验库（非结构化知识）：历史案例（某日对某件的判断 + 之后实际走势）、"
                           "平台规则（费率/T+7/提现）、饰品常识（磨损/纪念品/暗金/箱子）、CS2 官方公告。"
                           "用于回答「以前类似情况怎么判断、后来怎么走」「为什么这么定规则」"
                           "「这件饰品属于什么类型」。"
                           "⚠️ 本工具不提供任何实时价格与持仓数字，那些必须用其他工具查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "检索词，自然语言关键词即可，如「纪念品 普通版 区别」"}
                },
                "required": ["query"],
            },
        },
    },
]
