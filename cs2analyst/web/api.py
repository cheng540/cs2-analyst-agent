"""本地端 Web 控制台的 JSON API。

三条硬约束（交接文档 3.2 / 3.5，改动前请先读）：

1. **写数据的只有云端 pipeline**。本地端默认只读；唯一的写入口是 `/api/config`
   与 `/api/inventory`，它们只改 `config/`，绝不写 `data/`、`reports/`。
2. **写接口只对 127.0.0.1 开放**。线上 demo 部署同一份代码时开 `readonly=True`，
   所有写接口一律 403 —— 线上永远只读。
3. **密钥永不回传明文**。GET 配置只给 `sk-****1234` 这种掩码；写接口的返回值里
   也不回显密钥。

所有文件写入都走 `cs2analyst.web.store`（文件锁 + 原子替换 + 写前重读）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import load_config
from ..web import store

log = logging.getLogger("cs2analyst.web.api")

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}

# 允许通过 Web 改的配置项：白名单 + 类型 + 取值范围。
# 用白名单而不是"什么都让改"，是因为这些值直接影响真金白银的决策。
_SECRET_KEYS = {"steamdt.api_key", "llm.api_key", "notify.wecom_webhook"}

# 监控列表上限：再多巡检一轮的取价次数会明显拖长，也影响接口额度
MAX_WATCHLIST = 50

CONFIG_SPEC: dict[str, tuple[str, float, float]] = {
    # 点号路径: (类型, 最小值, 最大值)  —— 字符串类型忽略范围
    "steamdt.api_key": ("str", 0, 0),
    "steamdt.platform": ("str", 0, 0),
    "steamdt.timeout": ("int", 5, 120),
    "llm.api_key": ("str", 0, 0),
    "llm.model": ("str", 0, 0),
    "llm.enabled": ("bool", 0, 0),
    "notify.enabled": ("bool", 0, 0),
    "notify.wecom_webhook": ("str", 0, 0),
    "notify.only_if_actions": ("bool", 0, 0),
    "alerts.enabled": ("bool", 0, 0),
    "alerts.times": ("intlist", 0, 23),
    "alerts.summary_times": ("intlist", 0, 23),
    "alerts.urgent_drop_pct": ("float", 0.5, 50),
    "alerts.urgent_rise_pct": ("float", 0.5, 50),
    "alerts.ai_commentary": ("bool", 0, 0),
    "alerts.interval_seconds": ("float", 0, 30),
    "schedule.enabled": ("bool", 0, 0),
    "schedule.hour": ("int", 0, 23),
    "schedule.minute": ("int", 0, 59),
    # ---- 报告开关与时间（客户自主选择）----
    "reports.daily_enabled": ("bool", 0, 0),
    "reports.daily_time": ("time", 0, 0),
    "reports.noon_enabled": ("bool", 0, 0),
    "reports.noon_time": ("time", 0, 0),
    "reports.weekly_enabled": ("bool", 0, 0),
    "reports.weekly_day": ("int", 0, 6),
    "reports.weekly_time": ("time", 0, 0),
    # ---- 巡检间隔（分钟）----
    "alerts.interval_minutes": ("int", 15, 1440),
    "chat.max_questions_per_day": ("int", 0, 500),
    "sim.initial_capital": ("float", 100, 10_000_000),
    "sim.max_position_pct": ("float", 1, 100),
    "decision.stop_loss_pct": ("float", 0.05, 0.9),
    "decision.hold_loss_pct": ("float", 0.05, 0.9),
    "decision.take_profit_pct": ("float", 0.05, 5),
}

_INVENTORY_FIELDS = ("market_hash_name", "wear", "quantity", "buy_price", "buy_date",
                     "platform", "special_style", "note", "manual_price", "lots",
                     "status", "unlocked")


class ApiError(Exception):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _as_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} 必须是数字") from None


def _as_int(value: Any, field: str) -> int:
    try:
        number = float(value)
        if number != int(number):
            raise ValueError
        return int(number)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} 必须是整数") from None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是"):
        return True
    if text in ("0", "false", "no", "n", "off", "否", ""):
        return False
    raise ApiError(400, f"无法识别的布尔值：{value}")


def coerce_config_value(key: str, value: Any) -> Any:
    """按白名单校验并转换单个配置值。"""
    spec = CONFIG_SPEC.get(key)
    if spec is None:
        raise ApiError(400, f"不允许从网页修改该项：{key}")
    kind, low, high = spec
    if kind == "str":
        text = str(value if value is not None else "").strip()
        if key in _SECRET_KEYS and text.startswith("*"):
            raise ApiError(400, f"{key} 传回了掩码值，请传真实密钥或留空表示不修改")
        return text
    if kind == "time":
        raw = str(value if value is not None else "").strip().replace("：", ":")
        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", raw)
        if not match:
            raise ApiError(400, f"{key} 的时间格式应为 HH:MM，例如 17:30（收到 {value}）")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ApiError(400, f"{key} 的时间超出范围：{raw}")
        return f"{hour:02d}:{minute:02d}"
    if kind == "bool":
        return _as_bool(value)
    if kind == "intlist":
        if isinstance(value, str):
            items = [x for x in value.replace("，", ",").split(",") if x.strip()]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise ApiError(400, f"{key} 必须是时间点列表")
        out = []
        for item in items:
            hour = _as_int(item, key)
            if not low <= hour <= high:
                raise ApiError(400, f"{key} 的时间点必须在 {low:g}–{high:g} 之间：{hour}")
            out.append(hour)
        return sorted(set(out))
    if kind == "int":
        number = _as_int(value, key)
    else:
        number = _as_float(value, key)
    if not low <= number <= high:
        raise ApiError(400, f"{key} 必须在 {low:g}–{high:g} 之间（收到 {number:g}）")
    return number


def validate_inventory_item(item: Any) -> dict:
    """校验一条持仓。返回只含已知字段的干净字典。"""
    if not isinstance(item, dict):
        raise ApiError(400, "持仓条目必须是对象")
    name = str(item.get("market_hash_name") or "").strip()
    if not name:
        raise ApiError(400, "market_hash_name（英文标准名）不能为空")
    qty = _as_int(item.get("quantity", 1), "quantity")
    if qty < 0:
        raise ApiError(400, "quantity 不能为负")
    price = _as_float(item.get("buy_price", 0), "buy_price")
    if price < 0:
        raise ApiError(400, "buy_price 不能为负")
    lots = item.get("lots") or []
    if isinstance(lots, str):
        lots = [x for x in lots.replace("，", ",").split(",") if x.strip()]
    if not isinstance(lots, (list, tuple)):
        raise ApiError(400, "lots 必须是数组")
    clean_lots: list[float] = []
    for value in lots:
        number = _as_float(value, "lots")
        if number > 0:
            clean_lots.append(round(number, 2))
    out = {
        "market_hash_name": name,
        "wear": str(item.get("wear") or ""),
        "quantity": qty,
        "buy_price": round(price, 2),
        "buy_date": str(item.get("buy_date") or ""),
        "platform": str(item.get("platform") or "YOUPIN"),
        "special_style": str(item.get("special_style") or ""),
        "note": str(item.get("note") or ""),
        "manual_price": round(_as_float(item.get("manual_price", 0) or 0, "manual_price"), 2),
        "lots": clean_lots,
        "status": str(item.get("status") or ""),
        "unlocked": _as_bool(item.get("unlocked", False)),
    }
    return out


class ConsoleApi:
    """把项目数据投影成前端用的 JSON。实例是线程安全的（内部只读 + 局部缓存）。"""

    def __init__(self, root: Path, *, readonly: bool = False,
                 data_dir: str | Path | None = None,
                 report_dir: str | Path | None = None):
        """`data_dir` / `report_dir` 可覆盖：演示模式要读 `data/demo_run`，不能读真实数据。"""
        self.root = Path(root).resolve()
        self.readonly = bool(readonly)
        self._data_override = Path(data_dir) if data_dir else None
        self._report_override = Path(report_dir) if report_dir else None
        self._lock = threading.RLock()
        self._cfg_cache: tuple[int, Any] | None = None
        self._assistant: Any = None
        self._assistant_cfg_stamp = -1

    # ================= 基础 =================
    @property
    def config_path(self) -> Path:
        return self.root / "config" / "config.yaml"

    def cfg(self):
        """配置按 mtime 缓存：向导改完配置，下一次请求就生效，无需重启服务。"""
        try:
            stamp = self.config_path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        with self._lock:
            if self._cfg_cache and self._cfg_cache[0] == stamp:
                return self._cfg_cache[1]
        cfg = load_config(self.config_path)
        with self._lock:
            self._cfg_cache = (stamp, cfg)
        return cfg

    def _data(self) -> Path:
        if self._data_override is None:
            return self.root / "data"
        return self._data_override if self._data_override.is_absolute() \
            else (self.root / self._data_override)

    def _reports(self) -> Path:
        if self._report_override is None:
            return self.root / "reports"
        return self._report_override if self._report_override.is_absolute() \
            else (self.root / self._report_override)

    def _inventory_path(self) -> Path:
        """演示模式下读 data/demo_run/inventory_demo.json，否则读真实持仓。"""
        if self._data_override is not None:
            demo_inv = self._data() / "inventory_demo.json"
            if demo_inv.exists():
                return demo_inv
        return self.root / "config" / "inventory.json"

    # ================= 分发 =================
    def dispatch(self, method: str, path: str, query: dict, body: Any,
                 client_ip: str) -> tuple[int, dict]:
        """返回 (status, payload)。所有异常都收敛成 JSON，不把堆栈抛给前端。"""
        try:
            return self._route(method.upper(), path.rstrip("/") or "/", query or {},
                               body if body is not None else {}, client_ip or "")
        except ApiError as exc:
            return exc.status, {"error": exc.message}
        except Exception as exc:                      # 兜底：不让单个接口打挂服务
            log.exception("API 处理失败：%s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _route(self, method: str, path: str, query: dict, body: dict,
               client_ip: str) -> tuple[int, dict]:
        # ------ 只读接口 ------
        if method == "GET":
            if path in ("/api/health", "/api/health/"):
                return 200, self.health()
            if path == "/api/summary":
                return 200, self.summary()
            if path == "/api/holdings":
                return 200, self.holdings()
            if path == "/api/history":
                return 200, self.history(query.get("days", 30))
            if path == "/api/kline":
                return 200, self.kline(query.get("name", ""), query.get("days", 180))
            if path == "/api/reports":
                return 200, self.reports()
            if path == "/api/sim":
                return 200, self.sim()
            if path == "/api/knowledge/search":
                return 200, self.knowledge_search(query.get("q", ""), query.get("k", 5))
            if path == "/api/knowledge/stats":
                return 200, self.knowledge_stats()
            if path == "/api/config":
                return 200, self.config_view()
            if path == "/api/inventory":
                return 200, self.inventory_view()
            if path == "/api/setup":
                return 200, self.setup_status()
            if path == "/api/watchlist":
                return 200, self.watchlist_view()
            if path == "/api/plan":
                return 200, self.plan_view()

        # ------ 写接口（仅本机） ------
        if method == "POST" and path == "/api/ask":
            return 200, self.ask(body)
        if method == "POST" and path == "/api/config":
            self.require_local(client_ip)
            return 200, self.config_write(body)
        if method == "POST" and path == "/api/setup":
            self.require_local(client_ip)
            return 200, self.setup_apply(body)
        if path == "/api/inventory":
            self.require_local(client_ip)
            if method == "POST":
                return 200, self.inventory_add(body)
            if method == "PUT":
                return 200, self.inventory_replace(body)
            if method == "DELETE":
                return 200, self.inventory_delete(query.get("index", body.get("index")))
        if path == "/api/inventory/parse":
            self.require_local(client_ip)
            if method == "POST":
                return 200, self.inventory_parse(body)
        if path == "/api/inventory/bulk":
            self.require_local(client_ip)
            if method == "POST":
                return 200, self.inventory_bulk(body)
        if path == "/api/inventory/vision":
            self.require_local(client_ip)
            if method == "POST":
                return 200, self.inventory_vision(body)
        if path == "/api/watchlist":
            self.require_local(client_ip)
            if method == "POST":
                return 200, self.watchlist_add(body)
            if method == "DELETE":
                return 200, self.watchlist_delete(
                    query.get("name") or body.get("market_hash_name") or body.get("name"))
        raise ApiError(404, f"接口不存在：{method} {path}")

    # ================= 写权限 =================
    def require_local(self, client_ip: str) -> None:
        """写操作校验：线上只读模式直接 403；非本机来源也 403。"""
        if self.readonly:
            raise ApiError(403, "线上演示环境为只读，配置请在本机修改")
        host = (client_ip or "").strip()
        if host.startswith("::ffff:"):
            host = host[len("::ffff:"):]
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host not in LOCAL_HOSTS:
            raise ApiError(403, f"配置写入只允许本机访问（当前来源 {host}）")

    # ================= 只读数据 =================
    def _analysis_dates(self) -> list[str]:
        files = sorted(self._data().glob("analysis/*.json"))
        return [f.stem for f in files]

    def latest_analysis(self) -> dict:
        files = sorted(self._data().glob("analysis/*.json"))
        if not files:
            raise ApiError(404, "还没有分析快照：先跑一次 python main.py run（或 demo）")
        data = store.read_json(files[-1], default=None)
        if not isinstance(data, dict):
            raise ApiError(500, f"分析快照损坏：{files[-1].name}")
        return data

    def health(self) -> dict:
        cfg = self.cfg()
        dates = self._analysis_dates()
        latest = dates[-1] if dates else ""
        age = None
        if latest:
            try:
                age = (date.today() - date.fromisoformat(latest)).days
            except ValueError:
                age = None
        return {
            "ok": True,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "readonly": self.readonly,
            "latest_analysis": latest,
            "stale_days": age,
            "has_steamdt_key": cfg.has_api_key(),
            "has_llm_key": cfg.has_llm_key(),
            "report_count": len(list(self._reports().glob("*.docx"))),
            "version": "1.1",
        }

    def summary(self) -> dict:
        data = self.latest_analysis()
        market = data.get("market") or {}
        portfolio = data.get("portfolio") or {}
        llm = self._latest_llm()
        signals = []
        for item in (data.get("items") or []):
            signals.append({
                "name": item.get("中文名") or item.get("饰品"),
                "name_en": item.get("饰品"),
                "action": item.get("系统建议"),
                "conviction": item.get("信心度"),
                "pnl_pct": item.get("盈亏%"),
                "here": True,
            })
        for item in (data.get("watchlist") or [])[:6]:
            signals.append({
                "name": item.get("中文名") or item.get("饰品"),
                "name_en": item.get("饰品"),
                "action": item.get("系统建议"),
                "conviction": item.get("AI信心") or item.get("信心度"),
                "score": item.get("评分"),
                "here": False,
            })
        return {
            "date": data.get("date"),
            "market": {
                "trend": market.get("趋势"),
                "risk": market.get("风险等级"),
                "volume": market.get("量能"),
                "index": market.get("大盘指数"),
                "chg_pct": market.get("较昨日%"),
                "sentiment": market.get("情绪分"),
                "stance": market.get("操作基调"),
            },
            "portfolio": {
                "cost": portfolio.get("总成本"),
                "value": portfolio.get("总市值"),
                "pnl": portfolio.get("总盈亏"),
                "pnl_pct": portfolio.get("盈亏%"),
                "items": portfolio.get("条目数"),
                "pieces": portfolio.get("件数"),
            },
            "signals": signals,
            "judgement": {
                "market_view": (llm or {}).get("market_view"),
                "portfolio_advice": (llm or {}).get("portfolio_advice"),
                "risk_warnings": (llm or {}).get("risk_warnings") or [],
                "model": (llm or {}).get("model"),
                "error": (llm or {}).get("error") or "",
            },
            "reports": self._recent_reports(3),
            "freshness": self._freshness(data.get("date")),
        }

    def _latest_llm(self) -> dict:
        files = sorted(self._data().glob("llm/*.json"))
        if not files:
            return {}
        data = store.read_json(files[-1], default={})
        return data if isinstance(data, dict) else {}

    def _freshness(self, day: str | None) -> dict:
        try:
            days = (date.today() - date.fromisoformat(str(day))).days
        except (TypeError, ValueError):
            return {"day": day, "days_old": None, "stale": True, "hint": "没有有效日期"}
        if days <= 0:
            return {"day": day, "days_old": days, "stale": False, "hint": "今天的数据"}
        return {
            "day": day,
            "days_old": days,
            "stale": days >= 2,
            "hint": f"数据距今天 {days} 天，云端流水线可能没跑" if days >= 2 else "数据较新",
        }

    def _recent_reports(self, n: int = 5) -> list[dict]:
        files = sorted(self._reports().glob("*.docx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:n]
        return [{
            "name": f.name,
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "url": f"/file/reports/{f.name}",
        } for f in files]

    def holdings(self) -> dict:
        data = self.latest_analysis()
        inv = {str(it.get("market_hash_name")): it
               for it in (store.read_json(self._inventory_path(), default=[]) or [])
               if isinstance(it, dict)}
        rows = []
        for item in (data.get("items") or []):
            name = str(item.get("饰品") or "")
            meta = inv.get(name) or {}
            rows.append({
                "name": name,
                "name_cn": item.get("中文名") or name,
                "qty": item.get("数量"),
                "cost": item.get("成本价"),
                "price": item.get("现价"),
                "pnl_pct": item.get("盈亏%"),
                "position": item.get("位置"),
                "trend": item.get("趋势"),
                "volume_price": item.get("量价"),
                "liquidity": item.get("流动性"),
                "support": item.get("最近支撑"),
                "resistance": item.get("最近压力"),
                "action": item.get("系统建议"),
                "conviction": item.get("信心度"),
                "reasons": item.get("理由") or [],
                "target_weight": item.get("目标仓位%"),
                "unlocked": bool(meta.get("unlocked", False)),
                "buy_date": meta.get("buy_date") or "",
                "note": meta.get("note") or "",
                "status": meta.get("status") or "",
                "lots": meta.get("lots") or [],
            })
        return {
            "date": data.get("date"),
            "portfolio": data.get("portfolio") or {},
            "rows": rows,
            "watchlist": data.get("watchlist") or [],
            "freshness": self._freshness(data.get("date")),
        }

    def history(self, days: Any = 30) -> dict:
        """大盘指数 + 持仓市值曲线（ECharts 用）。

        市值曲线的口径：用**当前持仓**去乘每个快照日的市场最低在售价，
        所以它是"当前这批货在过去几天值多少"，不是当时的真实持仓结构。
        前端会标注这一点，不能当成历史净值曲线看。
        """
        try:
            days = max(5, min(365, int(days)))
        except (TypeError, ValueError):
            days = 30
        dates, index_values = self._market_series(days)
        value_map, cost_map = self._portfolio_series()
        value_dates = sorted(value_map)[-days:]
        return {
            "market": {"dates": dates, "index": index_values},
            "portfolio": {
                "dates": value_dates,
                "value": [value_map[d] for d in value_dates],
                "cost": [cost_map[d] for d in value_dates],
                "basis": "按当前持仓回算（用快照日市场最低在售价），非历史净值",
            },
        }

    def _market_series(self, days: int) -> tuple[list[str], list[float]]:
        rows = store.read_json(self._data() / "market" / "broad_kline_2.json", default=[])
        by_day: dict[str, float] = {}
        if isinstance(rows, list):
            for row in rows:
                try:
                    ts, close = float(row[0]), float(row[2])
                except (TypeError, ValueError, IndexError):
                    continue
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                by_day[day] = close          # 同日多根取最后一根
        if not by_day:
            idx = store.read_json(self._data() / "market" / "broad_index.json", default={})
            today = datetime.now().strftime("%Y-%m-%d")
            if isinstance(idx, dict) and idx.get("broadMarketIndex"):
                by_day[today] = float(idx["broadMarketIndex"])
        days_sorted = sorted(by_day)[-days:]
        return days_sorted, [round(by_day[d], 2) for d in days_sorted]

    def _portfolio_series(self) -> tuple[dict[str, float], dict[str, float]]:
        inventory = [it for it in (store.read_json(self._inventory_path(), default=[]) or [])
                     if isinstance(it, dict) and it.get("market_hash_name")]
        if not inventory:
            return {}, {}
        cost_total = 0.0
        for item in inventory:
            try:
                cost_total += float(item.get("buy_price") or 0) * int(item.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
        value_map: dict[str, float] = {}
        cost_map: dict[str, float] = {}
        for path in sorted(self._data().glob("snapshots/*_prices.json")):
            snap = store.read_json(path, default=None)
            if not isinstance(snap, dict):
                continue
            day = str(snap.get("_date") or path.name.split("_")[0])
            total = 0.0
            covered = 0
            for item in inventory:
                name = str(item["market_hash_name"])
                rows = snap.get(name)
                if not isinstance(rows, list) or not rows:
                    continue
                prices = [float(r.get("sellPrice")) for r in rows
                          if isinstance(r, dict) and r.get("sellPrice")]
                if not prices:
                    continue
                total += min(prices) * int(item.get("quantity") or 0)
                covered += 1
            if covered:
                value_map[day] = round(total, 2)
                cost_map[day] = round(cost_total, 2)
        return value_map, cost_map

    def kline(self, name: str, days: Any = 180) -> dict:
        """单品K线（读本地缓存，不打接口 → 不会触发限流）。"""
        name = str(name or "").strip()
        if not name:
            raise ApiError(400, "缺少 name 参数")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:120]
        rows = store.read_json(self._data() / "items" / safe / "kline_2.json", default=None)
        if not isinstance(rows, list) or not rows:
            return {"name": name, "dates": [], "close": [], "note": "本地没有该单品的K线缓存"}
        try:
            days = max(5, min(1000, int(days)))
        except (TypeError, ValueError):
            days = 180
        out = []
        for row in rows:
            try:
                out.append((datetime.fromtimestamp(float(row[0])).strftime("%Y-%m-%d"),
                            round(float(row[2]), 2)))
            except (TypeError, ValueError, IndexError):
                continue
        out = out[-days:]
        return {"name": name, "dates": [d for d, _ in out], "close": [c for _, c in out]}

    def reports(self) -> dict:
        return {"reports": self._recent_reports(40)}

    def sim(self) -> dict:
        tracks = []
        for label, fname in (("A 轨（纯规则）", "state_a.json"), ("B 轨（规则+AI）", "state_b.json")):
            state = store.read_json(self._data() / "sim" / fname, default=None)
            if not isinstance(state, dict):
                tracks.append({"label": label, "available": False})
                continue
            journal = state.get("journal") or []
            init = float(state.get("initial_capital") or 0)
            cash = float(state.get("cash") or 0)
            net = float(journal[-1].get("net_value")) if journal else cash
            tracks.append({
                "label": label,
                "available": True,
                "net_value": round(net, 2),
                "return_pct": round((net / init - 1) * 100, 2) if init else 0,
                "cash": round(cash, 2),
                "days": len(journal),
                "positions": [{
                    "name": name, "qty": pos.get("qty"), "avg_cost": pos.get("avg_cost"),
                    "locked_until": pos.get("locked_until"),
                } for name, pos in (state.get("positions") or {}).items()],
                "last_thoughts": str((journal[-1] if journal else {}).get("thoughts") or "")[:300],
            })
        return {"tracks": tracks}

    def knowledge_search(self, query: str, k: Any = 5) -> dict:
        from ..rag.store import search_knowledge
        try:
            k = max(1, min(10, int(k)))
        except (TypeError, ValueError):
            k = 5
        return search_knowledge(self.root, str(query or ""), k=k, cfg_sim=self.cfg().sim)

    def knowledge_stats(self) -> dict:
        from ..rag.corpus import stats
        from ..rag.store import load_index
        index = load_index(self.root, auto_build=False)
        return {"documents": len(index), "by_kind": stats(index.docs)["by_kind"] if index.docs else {}}

    # ================= 问答 =================
    def ask(self, body: dict) -> dict:
        question = str((body or {}).get("question") or "").strip()
        if not question:
            raise ApiError(400, "问题为空")
        cfg = self.cfg()
        if not cfg.has_llm_key():
            raise ApiError(400, "还没有配置 DeepSeek API Key：先在「设置」里填，或设置环境变量 DEEPSEEK_API_KEY")
        assistant = self._get_assistant()
        history = body.get("history") or []
        if not isinstance(history, list):
            history = []
        result = assistant.ask(question, history[-8:])
        evidence = [{
            "tool": t.get("tool"),
            "args": t.get("args"),
            "result": json.dumps(t.get("result"), ensure_ascii=False)[:2000],
        } for t in (result.get("evidence") or [])]
        payload = {k: v for k, v in result.items() if k != "evidence"}
        payload["evidence"] = evidence
        payload["quota_left"] = assistant.quota_left()
        return payload

    def _get_assistant(self):
        """按配置 mtime 复用助手实例：改完 key 立刻生效，不用重启服务。"""
        try:
            stamp = self.config_path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        with self._lock:
            if self._assistant is not None and self._assistant_cfg_stamp == stamp:
                return self._assistant
        from ..chat.assistant import ChatAssistant
        assistant = ChatAssistant(self.cfg())
        with self._lock:
            self._assistant = assistant
            self._assistant_cfg_stamp = stamp
        return assistant

    # ================= 配置（读掩码 / 写本地） =================
    def _env_overrides(self) -> dict[str, str]:
        mapping = {
            "steamdt.api_key": "STEAMDT_API_KEY",
            "llm.api_key": "DEEPSEEK_API_KEY",
            "notify.wecom_webhook": "WECOM_WEBHOOK",
        }
        return {key: env for key, env in mapping.items()
                if os.environ.get(env, "").strip()}

    def config_view(self) -> dict:
        cfg = self.cfg()
        groups = {
            "数据源（SteamDT）": {
                "steamdt.api_key": cfg.steamdt.get("api_key"),
                "steamdt.platform": cfg.steamdt.get("platform"),
                "steamdt.timeout": cfg.steamdt.get("timeout"),
            },
            "大模型（DeepSeek）": {
                "llm.api_key": cfg.llm.get("api_key"),
                "llm.model": cfg.llm.get("model"),
                "llm.enabled": cfg.llm.get("enabled"),
            },
            "推送（企业微信）": {
                "notify.enabled": cfg.notify.get("enabled"),
                "notify.wecom_webhook": cfg.notify.get("wecom_webhook"),
                "notify.only_if_actions": cfg.notify.get("only_if_actions"),
            },
            "盘中巡检": {
                "alerts.enabled": cfg.alerts.get("enabled"),
                "alerts.times": cfg.alerts.get("times"),
                "alerts.summary_times": cfg.alerts.get("summary_times"),
                "alerts.urgent_drop_pct": cfg.alerts.get("urgent_drop_pct"),
                "alerts.urgent_rise_pct": cfg.alerts.get("urgent_rise_pct"),
                "alerts.ai_commentary": cfg.alerts.get("ai_commentary"),
                "alerts.interval_seconds": cfg.alerts.get("interval_seconds"),
            },
            "策略参数（改动影响真实建议）": {
                "decision.stop_loss_pct": cfg.decision.get("stop_loss_pct"),
                "decision.hold_loss_pct": cfg.decision.get("hold_loss_pct"),
                "decision.take_profit_pct": cfg.decision.get("take_profit_pct"),
                "sim.initial_capital": cfg.sim.get("initial_capital"),
                "sim.max_position_pct": cfg.sim.get("max_position_pct"),
                "chat.max_questions_per_day": cfg.chat.get("max_questions_per_day"),
            },
            "报告与巡检（可自选开关）": {
                "reports.daily_enabled": cfg.reports.get("daily_enabled", True),
                "reports.daily_time": cfg.reports.get("daily_time", "17:30"),
                "reports.noon_enabled": cfg.reports.get("noon_enabled", True),
                "reports.noon_time": cfg.reports.get("noon_time", "12:10"),
                "reports.weekly_enabled": cfg.reports.get("weekly_enabled", True),
                "reports.weekly_day": cfg.reports.get("weekly_day", 6),
                "reports.weekly_time": cfg.reports.get("weekly_time", "20:00"),
                "alerts.interval_minutes": cfg.alerts.get("interval_minutes", 60),
            },
            "调度": {
                "schedule.enabled": cfg.schedule.get("enabled"),
                "schedule.hour": cfg.schedule.get("hour"),
                "schedule.minute": cfg.schedule.get("minute"),
            },
        }
        env_keys = self._env_overrides()
        masked: dict[str, dict] = {}
        for title, values in groups.items():
            masked[title] = {}
            for key, value in values.items():
                item = {"value": store.mask_secret(value) if key in _SECRET_KEYS else value,
                        "secret": key in _SECRET_KEYS,
                        "env": key in env_keys,
                        "set": bool(str(value or "").strip()) if key in _SECRET_KEYS else True}
                masked[title][key] = item
        return {
            "readonly": self.readonly,
            "groups": masked,
            "env_keys": sorted(env_keys),
            "config_path": str(self.config_path),
        }

    def config_write(self, body: dict) -> dict:
        updates = (body or {}).get("updates")
        if updates is None:                       # 也接受扁平写法 {"steamdt.api_key": "..."}
            updates = {k: v for k, v in (body or {}).items() if "." in k}
        if not isinstance(updates, dict) or not updates:
            raise ApiError(400, "没有要修改的配置项")
        clean: dict[str, Any] = {}
        for key, value in updates.items():
            if key in self._env_overrides():
                raise ApiError(400, f"{key} 由环境变量提供，网页改不了（请改环境变量）")
            clean[key] = coerce_config_value(key, value)
        if not clean:
            raise ApiError(400, "没有可写入的配置项")
        store.backup_file(self.config_path)
        hit = store.patch_yaml_scalars(self.config_path, clean)
        with self._lock:                          # 让下一次请求重新加载配置
            self._cfg_cache = None
            self._assistant = None
        log.info("网页修改配置：%s", "、".join(sorted(clean)))
        return {
            "ok": True,
            "written": sorted(clean),
            "created_lines": sorted(k for k, v in hit.items() if not v),
            "note": "已写入 config/config.yaml（注释保留，写前有备份）",
        }

    # ================= 持仓 CRUD =================
    def inventory_view(self) -> dict:
        items = store.read_json(self._inventory_path(), default=[])
        if not isinstance(items, list):
            items = []
        return {"items": items, "count": len(items), "readonly": self.readonly}

    def _write_inventory(self, items: list[dict]) -> None:
        store.backup_file(self._inventory_path())
        store.update_json(self._inventory_path(), lambda _old: items, default=[])

    def inventory_add(self, body: dict) -> dict:
        item = validate_inventory_item((body or {}).get("item") or body)
        current = store.read_json(self._inventory_path(), default=[])
        if not isinstance(current, list):
            current = []
        merged = current + [item]
        self._write_inventory(merged)
        return {"ok": True, "count": len(merged), "item": item}

    def inventory_replace(self, body: dict) -> dict:
        index = self._index_of(body)
        item = validate_inventory_item((body or {}).get("item") or body)
        current = store.read_json(self._inventory_path(), default=[])
        if not isinstance(current, list) or not 0 <= index < len(current):
            raise ApiError(404, "要修改的持仓不存在")
        current[index] = item
        self._write_inventory(current)
        return {"ok": True, "count": len(current), "index": index, "item": item}

    def inventory_delete(self, index: Any) -> dict:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ApiError(400, "缺少要删除的行号 index") from None
        current = store.read_json(self._inventory_path(), default=[])
        if not isinstance(current, list) or not 0 <= idx < len(current):
            raise ApiError(404, "要删除的持仓不存在")
        removed = current.pop(idx)
        self._write_inventory(current)
        return {"ok": True, "count": len(current), "removed": removed.get("market_hash_name")}

    def _index_of(self, body: dict) -> int:
        raw = (body or {}).get("index")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ApiError(400, "缺少 index（第几行，从 0 开始）") from None

    # ================= 批量导入（粘贴 / 识图） =================
    def _inventory_items(self) -> list[dict]:
        items = store.read_json(self._inventory_path(), default=[])
        return items if isinstance(items, list) else []

    def _mark_existing(self, result: dict) -> dict:
        existing = {str(i.get("market_hash_name") or "").strip().lower()
                    for i in self._inventory_items()}
        for item in result.get("items") or []:
            item["exists"] = str(item.get("market_hash_name") or "").strip().lower() in existing
        result.setdefault("summary", {})["already_exists"] = sum(
            1 for i in result.get("items") or [] if i.get("exists"))
        return result

    def inventory_vision(self, body: dict) -> dict:
        """截图 → 候选条目（调用 deepseek-flash 的多模态能力，**不写库**）。"""
        from ..inventory_parse import load_name_lookup
        from ..vision_ingest import VisionError, images_to_items

        images = (body or {}).get("images")
        if not isinstance(images, list) or not images:
            raise ApiError(400, "没有收到图片")
        if len(images) > 3:
            raise ApiError(400, "一次最多识别 3 张图")

        cfg = self.cfg()
        lookup = load_name_lookup(self.root, [self._data() / "name_map.json"])
        try:
            result = images_to_items(cfg, images, lookup)
        except VisionError as exc:
            # 配置/图片/模型没读出内容：都属于「用户能自己处理」，用 400 带上可操作提示
            raise ApiError(400, str(exc)) from None
        except Exception as exc:                     # 网络、401、限流等
            log.warning("识图失败：%s", exc)
            raise ApiError(502, f"调用 DeepSeek 失败：{exc}。"
                                "可以先检查 Key 是否有效，或改用「粘贴文本」导入。") from None
        return self._mark_existing(result)

    def inventory_parse(self, body: dict) -> dict:
        """粘贴文本 → 候选条目（**只解析，不写库**）。前端必须让人确认后再调 bulk。"""
        from ..inventory_parse import load_name_lookup, parse_inventory_text

        text = str((body or {}).get("text") or "")
        if not text.strip():
            raise ApiError(400, "粘贴的内容是空的")
        if len(text) > 20000:
            raise ApiError(400, "文本太长（超过 2 万字符），请分批粘贴")

        lookup = load_name_lookup(self.root, [self._data() / "name_map.json"])
        result = parse_inventory_text(text, lookup)
        if not result["items"]:
            raise ApiError(400, "没识别出任何条目：每行至少要有一个饰品名称")

        self._mark_existing(result)
        result["lookup_size"] = len(lookup)
        result["source"] = "paste"
        result["note"] = ("解析结果只是初值。请重点核对英文名（含磨损档）与单价，"
                          "确认后再导入——名称写错会按另一个物品报价，而且不会报错。")
        return result

    def inventory_bulk(self, body: dict) -> dict:
        """确认导入：先全部校验，再一次性写盘（不会写一半）。"""
        raw_items = (body or {}).get("items")
        mode = str((body or {}).get("mode") or "append").lower()
        if mode not in ("append", "replace"):
            raise ApiError(400, "mode 只能是 append（追加）或 replace（替换全部）")
        if not isinstance(raw_items, list) or not raw_items:
            raise ApiError(400, "没有要导入的条目")
        if len(raw_items) > 200:
            raise ApiError(400, "一次最多导入 200 条，请分批")

        clean = [validate_inventory_item(it) for it in raw_items]
        current = self._inventory_items()

        if mode == "replace":
            merged = clean
            note = f"已用 {len(clean)} 条替换原有 {len(current)} 条"
        else:
            existing = {str(i.get("market_hash_name") or "").strip().lower() for i in current}
            dup = [c["market_hash_name"] for c in clean
                   if c["market_hash_name"].strip().lower() in existing]
            if dup:
                shown = "、".join(dup[:3]) + (f" 等 {len(dup)} 条" if len(dup) > 3 else "")
                raise ApiError(400, f"这些已经存在于持仓里：{shown}。"
                                    "请选择「替换全部」，或先删掉原有条目再追加。")
            merged = current + clean
            note = f"追加 {len(clean)} 条，共 {len(merged)} 条"

        self._write_inventory(merged)
        return {"ok": True, "count": len(merged), "imported": len(clean),
                "mode": mode, "note": note}


    def watchlist_path(self) -> Path:
        """与模拟盘共用同一份观察池：加进来的标的既参与模拟盘，也会被巡检监控。"""
        return self.root / "config" / "sim_watchlist.json"

    def _watchlist_raw(self) -> dict:
        data = store.read_json(self.watchlist_path(), default=None)
        if isinstance(data, list):                     # 兼容纯数组写法
            return {"items": data}
        if not isinstance(data, dict):
            return {"items": []}
        data.setdefault("items", [])
        if not isinstance(data["items"], list):
            data["items"] = []
        return data

    @staticmethod
    def _clean_item_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name:
            raise ApiError(400, "饰品名不能为空")
        if len(name) > 120:
            raise ApiError(400, "饰品名太长（>120 字符），请用 Steam 官方英文名")
        if "\n" in name or "\r" in name:
            raise ApiError(400, "饰品名不能包含换行")
        return name

    def watchlist_view(self) -> dict:
        data = self._watchlist_raw()
        items = []
        for entry in data["items"]:
            if isinstance(entry, str):
                items.append({"market_hash_name": entry, "note": ""})
            elif isinstance(entry, dict) and entry.get("market_hash_name"):
                items.append({"market_hash_name": entry["market_hash_name"],
                              "note": str(entry.get("note") or "")})
        return {
            "items": items,
            "count": len(items),
            "max": MAX_WATCHLIST,
            "readonly": self.readonly,
            "path": "config/sim_watchlist.json",
            "hint": "这里是「额外监控」的标的：持仓会自动监控，不用加进来。"
                    "名称必须是 Steam 官方英文名（含磨损后缀，如 AK-47 | Redline (Field-Tested)）。"
                    "新增的标的会在下一次日报分析里出现，并被巡检纳入监控。",
        }

    def watchlist_add(self, body: dict) -> dict:
        name = self._clean_item_name((body or {}).get("market_hash_name") or (body or {}).get("name"))
        note = str((body or {}).get("note") or "").strip()[:200]
        data = self._watchlist_raw()
        existing = {str(e.get("market_hash_name") if isinstance(e, dict) else e)
                    for e in data["items"]}
        if name in existing:
            raise ApiError(400, f"「{name}」已经在监控列表里了")
        if len(data["items"]) >= MAX_WATCHLIST:
            raise ApiError(400, f"监控列表最多 {MAX_WATCHLIST} 个标的，先删几个再添加")
        data["items"].append({"market_hash_name": name, **({"note": note} if note else {})})
        self._write_watchlist(data)
        return {"ok": True, "count": len(data["items"]), "item": {"market_hash_name": name}}

    def watchlist_delete(self, name: Any) -> dict:
        target = self._clean_item_name(name)
        data = self._watchlist_raw()
        kept = [e for e in data["items"]
                if str(e.get("market_hash_name") if isinstance(e, dict) else e) != target]
        if len(kept) == len(data["items"]):
            raise ApiError(404, f"监控列表里没有「{target}」")
        data["items"] = kept
        self._write_watchlist(data)
        return {"ok": True, "count": len(kept), "removed": target}

    def _write_watchlist(self, data: dict) -> None:
        store.backup_file(self.watchlist_path())
        store.update_json(self.watchlist_path(), lambda _old: data,
                          default={"items": []})

    def plan_view(self) -> dict:
        """当前生效的报告/巡检计划（网页「设置」页展示，也可用来核对是否真的关掉了）。"""
        from ..policy import (
            KIND_LABELS, alerts_enabled, alerts_interval_minutes, describe_plan,
            next_run_hint, report,
        )
        cfg = self.cfg()
        plans = []
        for kind, label in KIND_LABELS.items():
            conf = report(cfg, kind)
            plans.append({
                "kind": kind,
                "label": label,
                "enabled": conf["enabled"],
                "hour": conf["hour"],
                "minute": conf["minute"],
                "weekday": conf.get("weekday"),
                "next": next_run_hint(cfg, kind),
            })
        return {
            "plans": plans,
            "alerts": {
                "enabled": alerts_enabled(cfg),
                "interval_minutes": alerts_interval_minutes(cfg),
            },
            "lines": describe_plan(cfg),
            "cron_hint": "改完开关后，服务器上执行 python tools/install_cron.py --install 让 cron 与配置对齐；"
                         "本地直接 python main.py schedule 常驻即可，无需改 cron。",
        }

    # ================= 首次配置向导 =================
    def setup_status(self) -> dict:
        cfg = self.cfg()
        items = store.read_json(self._inventory_path(), default=[]) or []
        checks = {
            "steamdt_key": cfg.has_api_key(),
            "llm_key": cfg.has_llm_key(),
            "webhook": bool(str(cfg.notify.get("wecom_webhook") or "").strip()),
            "inventory": isinstance(items, list) and len(items) > 0,
            "report": bool(list(self._reports().glob("*.docx"))),
        }
        missing = [k for k, ok in checks.items() if not ok and k in
                   ("steamdt_key", "llm_key", "inventory")]
        return {
            "readonly": self.readonly,
            "checks": checks,
            "missing": missing,
            "configured": not missing,
            "inventory_count": len(items) if isinstance(items, list) else 0,
            "env_keys": sorted(self._env_overrides()),
        }

    def setup_apply(self, body: dict) -> dict:
        """向导一次性写入：密钥 + 推送 + 巡检时间 + （可选）导入持仓。

        先全部校验再落盘：任何一项不合法就整单拒绝，避免写了一半的配置。
        """
        body = body or {}
        updates: dict[str, Any] = {}
        for key in ("steamdt.api_key", "llm.api_key", "notify.wecom_webhook",
                    "notify.enabled", "alerts.enabled", "alerts.times",
                    "alerts.summary_times", "alerts.urgent_drop_pct",
                    "alerts.urgent_rise_pct", "alerts.ai_commentary",
                    "schedule.hour", "schedule.minute", "llm.model"):
            if key not in body:
                continue
            value = body[key]
            if key in _SECRET_KEYS and (value is None or str(value).strip() == ""):
                continue                      # 留空 = 不修改
            if key in self._env_overrides():
                continue                      # 环境变量优先，忽略网页值
            updates[key] = coerce_config_value(key, value)

        raw_items = body.get("inventory")
        items: list[dict] | None = None
        if isinstance(raw_items, list):
            if not raw_items:
                raise ApiError(400, "导入的持仓为空")
            items = [validate_inventory_item(it) for it in raw_items]

        if not updates and items is None:
            raise ApiError(400, "没有要保存的内容")

        written: list[str] = []
        if updates:
            store.backup_file(self.config_path)
            store.patch_yaml_scalars(self.config_path, updates)
            written = sorted(updates)
            with self._lock:
                self._cfg_cache = None
                self._assistant = None
        if items is not None:
            self._write_inventory(items)

        status = self.setup_status()
        return {
            "ok": True,
            "written": written,
            "inventory_count": status["inventory_count"],
            "checks": status["checks"],
            "missing": status["missing"],
            "next_command": "python main.py run",
            "note": "配置已写入 config/（仅本机）。云端 pipeline 不需要重启，下次运行自动读取。",
        }
