"""SteamDT 开放平台 API 客户端。

接口文档: https://doc.steamdt.com/
- POST /open/cs2/broad/v1/kline   大盘K线  {type:1时/2日/3周, maxTime}
- GET  /open/cs2/broad/v1/index   大盘最新指数
- POST /open/cs2/item/v1/kline    单品K线  {marketHashName, type, platform, specialStyle}
- POST /open/cs2/v1/price/batch   批量价格 {marketHashNames:[...] 最多100}
- GET  /open/cs2/v1/price/avg     近7天均价 ?marketHashName=

K线返回 data: [[更新时间, 开盘, 收盘, 最高, 最低], ...]（不含成交量）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..logging_setup import get_logger

log = get_logger(__name__)

KLINE_TYPES = {1: "时K", 2: "日K", 3: "周K"}


def _fmt_err(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return f"errorCode={body.get('errorCode')}, errorMsg={body.get('errorMsg')}"
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"


class SteamDTClient:
    """SteamDT OpenAPI 客户端。

    - Bearer 认证
    - 网络错误/5xx/限速自动重试（指数退避）
    - 单品K线按 request_gap 节流，避免触发限速
    - offline_dir 非空时直接从本地 JSON 读数据（演示/断网兜底）
    """

    def __init__(self, cfg_steamdt: dict, offline_dir: Path | None = None):
        self.base_url = str(cfg_steamdt.get("base_url", "https://open.steamdt.com")).rstrip("/")
        self.api_key = str(cfg_steamdt.get("api_key", "")).strip()
        self.timeout = float(cfg_steamdt.get("timeout", 20))
        self.retry = int(cfg_steamdt.get("retry", 3))
        self.platform = str(cfg_steamdt.get("platform", "ALL"))
        self.special_style = str(cfg_steamdt.get("special_style", ""))
        self.request_gap = float(cfg_steamdt.get("request_gap", 1.2))
        self.offline_dir = Path(offline_dir) if offline_dir else None
        self._gap_lock = threading.Lock()
        self._last_item_kline_ts = 0.0
        self._session = requests.Session()

    # ------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, *, params=None, body=None) -> dict:
        """发起请求并处理重试；返回解析后的 JSON 字典。"""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.retry + 1):
            try:
                resp = self._session.request(
                    method, url, headers=self._headers(), params=params,
                    json=body, timeout=self.timeout,
                )
                if resp.status_code == 429:
                    log.warning("限速(429) %s，第 %d 次尝试", path, attempt)
                    time.sleep(5 * attempt)
                    continue
                if resp.status_code >= 500:
                    log.warning("服务端错误 %d %s，第 %d 次尝试", resp.status_code, path, attempt)
                    time.sleep(2 * attempt)
                    continue
                data = resp.json()
                if not data.get("success", False):
                    raise RuntimeError(f"SteamDT 接口返回失败: {_fmt_err(resp)}")
                return data
            except (requests.RequestException, ValueError) as exc:  # 网络/JSON 错误
                last_exc = exc
                log.warning("请求异常 %s（第 %d 次）: %s", path, attempt, exc)
                time.sleep(2 * attempt)
        raise RuntimeError(f"请求失败（已重试 {self.retry} 次）: {url} -> {last_exc}")

    def _item_kline_gate(self) -> None:
        """单品K线节流：保证相邻两次请求间隔 >= request_gap 秒。"""
        with self._gap_lock:
            wait = self.request_gap - (time.monotonic() - self._last_item_kline_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_item_kline_ts = time.monotonic()

    # ------------------------------------------------------------
    # 离线模式
    # ------------------------------------------------------------
    def _offline_load(self, name: str) -> dict | None:
        if self.offline_dir is None:
            return None
        p = self.offline_dir / f"{name}.json"
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _offline_require(self, name: str) -> dict:
        """离线模式读取数据；文件缺失时抛出明确错误（绝不回退到真实网络）。"""
        payload = self._offline_load(name)
        if payload is None:
            raise RuntimeError(f"离线数据缺失: {name}.json（演示模式下该饰品无行情数据）")
        return payload

    # ------------------------------------------------------------
    # 大盘
    # ------------------------------------------------------------
    def broad_kline(self, ktype: int = 2, max_time: int | None = None) -> pd.DataFrame:
        """大盘K线。返回 DataFrame[ts, open, close, high, low]（时间升序）。"""
        if self.offline_dir is not None:
            payload = self._offline_require(f"broad_kline_{ktype}")
            return parse_kline_rows(payload.get("data") or [])
        body: dict[str, Any] = {"type": ktype}
        if max_time:
            body["maxTime"] = max_time
        data = self._request("POST", "/open/cs2/broad/v1/kline", body=body)
        return parse_kline_rows(data.get("data") or [])

    def broad_index(self) -> dict:
        """大盘最新指数。"""
        if self.offline_dir is not None:
            payload = self._offline_require("broad_index")
            return payload.get("data") or {}
        data = self._request("GET", "/open/cs2/broad/v1/index")
        return data.get("data") or {}

    # ------------------------------------------------------------
    # 单品
    # ------------------------------------------------------------
    def item_kline(self, market_hash_name: str, ktype: int = 2,
                   platform: str | None = None, special_style: str | None = None) -> pd.DataFrame:
        """单品K线（不含成交量）。"""
        if self.offline_dir is not None:
            safe = _safe_name(market_hash_name)
            payload = self._offline_require(f"item_kline_{safe}_{ktype}")
            return parse_kline_rows(payload.get("data") or [])
        self._item_kline_gate()
        body: dict[str, Any] = {
            "marketHashName": market_hash_name,
            "type": ktype,
            "platform": platform or self.platform or "ALL",
        }
        style = special_style or self.special_style
        if style:
            body["specialStyle"] = style
        data = self._request("POST", "/open/cs2/item/v1/kline", body=body)
        return parse_kline_rows(data.get("data") or [])

    def batch_price(self, names: list[str]) -> dict[str, list[dict]]:
        """批量查询价格/在售/求购。返回 {marketHashName: [platform价格行, ...]}。"""
        if self.offline_dir is not None:
            payload = self._offline_require("batch_price")
            rows = payload.get("data") or []
            return {r.get("marketHashName"): (r.get("dataList") or [])
                    for r in rows if r.get("marketHashName")}
        out: dict[str, list[dict]] = {}
        # 每批最多 100 个
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            data = self._request("POST", "/open/cs2/v1/price/batch", body={"marketHashNames": chunk})
            for row in data.get("data") or []:
                out[row.get("marketHashName")] = row.get("dataList") or []
        return out

    def avg7_price(self, market_hash_name: str) -> dict:
        """近7天全平台均价。"""
        if self.offline_dir is not None:
            safe = _safe_name(market_hash_name)
            payload = self._offline_require(f"avg7_{safe}")
            return payload.get("data") or {}
        data = self._request("GET", "/open/cs2/v1/price/avg",
                             params={"marketHashName": market_hash_name})
        return data.get("data") or {}


def parse_kline_rows(rows: list) -> pd.DataFrame:
    """解析 K 线原始数组 [[ts, open, close, high, low], ...] -> DataFrame。"""
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "close", "high", "low"])
    recs = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts, o, c, h, l = row[0], row[1], row[2], row[3], row[4]
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if ts > 1e11:  # 毫秒 -> 秒（秒级时间戳当前约 1.7e9，毫秒级约 1.7e12）
            ts = ts / 1000.0
        recs.append({
            "ts": int(ts),
            "open": float(o), "close": float(c),
            "high": float(h), "low": float(l),
        })
    df = pd.DataFrame(recs)
    if not df.empty:
        # 同时间戳保留最后一条（最新数据覆盖旧数据）
        df = df.drop_duplicates(subset=["ts"], keep="last").sort_values("ts").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Shanghai").dt.date
    return df


def _safe_name(name: str) -> str:
    """将饰品名转为安全文件名。"""
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return keep[:120] or "unnamed"
