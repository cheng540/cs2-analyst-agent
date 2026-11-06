"""SteamDT 官网公开接口爬虫客户端（无需 API Key，无需登录）。

仅访问公开无鉴权接口，作为开放平台 API 的补充数据源：
- GET /api/index/trend/v1/summary        大盘指数 + 今日逐小时历史 + 涨跌类型/连涨跌天数
                                          + 情绪分趋势 + 在线人数趋势
- GET /api/index/transaction/trend/v1/all 2023年至今每日 [日期, 成交额(元), 成交量(件)]
- GET /api/index/players/trend/v1/all     每日玩家/在线人数趋势
- POST /api/index/item/change/v1/list     涨跌榜（含市场名与各平台价格）

说明：单品K线/成交明细等接口需要登录态（返回 errorCode 108），故单品级数据
仍以开放平台 API（免费 Key）为准；本爬虫只取公开的大盘级数据。
请求频率极低（每次运行个位数请求），请勿高频调用。
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

from ..logging_setup import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.steamdt.com/api"


def parse_daily_rows(rows: list, col_names: list[str]) -> pd.DataFrame:
    """把 [[日期, v1, v2...], ...] 解析为 DataFrame（去重、按日期升序）。"""
    recs = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        if not isinstance(row[0], str):  # 日期必须是字符串（YYYY-MM-DD）
            continue
        try:
            rec = {"date": pd.to_datetime(row[0])}
            for i, col in enumerate(col_names, start=1):
                rec[col] = float(row[i]) if i < len(row) and row[i] not in (None, "") else float("nan")
            recs.append(rec)
        except (TypeError, ValueError):
            continue
    df = pd.DataFrame(recs, columns=["date", *col_names])
    if df.empty:
        return df
    # 同日期保留最后一条（最新数据覆盖）
    return df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


class SteamDTScraper:
    """SteamDT 公开接口爬虫客户端。"""

    def __init__(self, timeout: float = 15, retry: int = 2, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry = retry
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Accept": "application/json",
            "Referer": "https://www.steamdt.com/mkt",
        })

    def _request(self, method: str, path: str, *, params=None, body=None) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.retry + 1):
            try:
                resp = self._session.request(method, url, params=params, json=body,
                                             timeout=self.timeout)
                if resp.status_code >= 500:
                    log.warning("SteamDT 官网 5xx %s，第 %d 次尝试", path, attempt)
                    time.sleep(2 * attempt)
                    continue
                data = resp.json()
                if not data.get("success", False):
                    code = data.get("errorCode")
                    msg = data.get("errorMsg", "")
                    raise RuntimeError(f"SteamDT 官网接口失败: errorCode={code} {msg}")
                return data
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("官网接口异常 %s（第 %d 次）: %s", path, attempt, exc)
                time.sleep(2 * attempt)
        raise RuntimeError(f"官网接口请求失败（已重试 {self.retry} 次）: {path} -> {last_exc}")

    # ------------------------------------------------------------
    # 大盘
    # ------------------------------------------------------------
    def market_summary(self) -> dict:
        """大盘指数 + 今日逐小时历史 + 情绪分趋势 + 在线人数趋势。"""
        data = self._request("GET", "/index/trend/v1/summary")
        return data.get("data") or {}

    def transaction_daily(self) -> pd.DataFrame:
        """每日成交额/成交量（2023年至今）。列: date, turnover, volume。"""
        data = self._request("GET", "/index/transaction/trend/v1/all")
        return parse_daily_rows(data.get("data") or [], ["turnover", "volume"])

    def players_daily(self) -> pd.DataFrame:
        """每日玩家/在线人数趋势。列: date, online, players。"""
        data = self._request("GET", "/index/players/trend/v1/all")
        return parse_daily_rows(data.get("data") or [], ["online", "players"])

    # ------------------------------------------------------------
    # 榜单
    # ------------------------------------------------------------
    def item_change_list(self, page: int = 1, page_size: int = 50) -> dict:
        """在售价涨跌榜。返回原始 data 字典（含 list）。"""
        data = self._request("POST", "/index/item/change/v1/list",
                             body={"page": page, "pageSize": page_size})
        return data.get("data") or {}

    # ------------------------------------------------------------
    # 单品（SSR 详情页，免登录）
    # ------------------------------------------------------------
    def item_detail_html(self, market_hash_name: str) -> str:
        """拉取单品详情页 HTML（SSR 渲染，包含商品数据载荷）。"""
        from urllib.parse import quote
        url = f"https://www.steamdt.com/mkt/{quote(market_hash_name)}"
        for attempt in range(1, self.retry + 1):
            try:
                resp = self._session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code >= 500:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"单品页返回 HTTP {resp.status_code}")
            except requests.RequestException as exc:
                log.warning("单品页请求异常 %s（第 %d 次）: %s", market_hash_name, attempt, exc)
                time.sleep(2 * attempt)
        raise RuntimeError(f"单品页拉取失败（已重试 {self.retry} 次）: {market_hash_name}")

    def item_detail(self, market_hash_name: str) -> dict:
        """解析单品详情页，返回商品数据字典。"""
        from .nuxt_payload import extract_item_dict
        html = self.item_detail_html(market_hash_name)
        return extract_item_dict(html)
