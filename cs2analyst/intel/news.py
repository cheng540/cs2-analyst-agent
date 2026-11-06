"""情报采集：V社/官方更新公告 + 市场异动，供 LLM 研判层使用。

- 官方更新/公告：Steam 新闻接口（appid=730，公开无需鉴权）
- 市场异动：SteamDT 官网「涨跌榜」公开接口（在售价/在售量/求购价变化）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

from ..logging_setup import get_logger

log = get_logger(__name__)

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


def _fmt_price(v) -> str:
    try:
        return f"¥{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def summarise_anomaly(row: dict) -> str:
    """把一条涨跌榜数据整理成一句话描述。"""
    try:
        before = float(row.get("beforeSellPrice") or 0)
        now = float(row.get("sellPrice") or 0)
    except (TypeError, ValueError):
        before = now = 0.0
    pct = f"{(now / before - 1) * 100:+.1f}%" if before > 0 else "-"
    parts = [f"在售价 {_fmt_price(before)}→{_fmt_price(now)}（{pct}）"]
    try:
        bsc, sc = int(row.get("beforeSellCount") or 0), int(row.get("sellCount") or 0)
        if bsc or sc:
            parts.append(f"在售量 {bsc}→{sc}")
    except (TypeError, ValueError):
        pass
    try:
        bp, pp = float(row.get("beforePurchasePrice") or 0), float(row.get("purchasePrice") or 0)
        if bp or pp:
            parts.append(f"求购价 {_fmt_price(bp)}→{_fmt_price(pp)}")
    except (TypeError, ValueError):
        pass
    return "，".join(parts)


class IntelCollector:
    """情报采集器。"""

    def __init__(self, cfg_intel: dict | None = None, scraper=None):
        cfg = cfg_intel or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.appid = int(cfg.get("news_appid", 730))
        self.news_count = int(cfg.get("news_count", 12))
        self.news_maxlen = int(cfg.get("news_maxlen", 500))
        self.anomaly_size = int(cfg.get("anomaly_page_size", 20))
        self.scraper = scraper
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; cs2-analyst/1.0)"})

    # ---------------- 官方更新公告 ----------------
    def update_records(self) -> list[dict]:
        """SteamDT「CS2 更新记录」接口（中文摘要 + 官方市场影响评级）。"""
        if self.scraper is None:
            return []
        try:
            data = self.scraper._request(
                "POST", "/index/cs2-update-record/v1/page",
                body={"page": 1, "pageSize": self.news_count})
        except Exception as exc:
            log.warning("CS2 更新记录获取失败: %s", exc)
            return []
        out = []
        for it in ((data.get("data") or {}).get("list") or []):
            if not isinstance(it, dict):
                continue
            out.append({
                "title": str(it.get("title") or "").strip(),
                "date_str": str(it.get("updateTime") or "")[:10],
                "feed": "CS2 更新记录",
                "market_impact": str(it.get("marketImpactLevel") or ""),
                "gameplay_impact": str(it.get("gameplayImpactLevel") or ""),
                "update_type": str(it.get("updateType") or ""),
                "contents": " ".join(str(it.get("summaryContent") or "").split())[:self.news_maxlen],
            })
        log.info("CS2 更新记录：获取 %d 条", len(out))
        return out

    def steam_news(self) -> list[dict]:
        try:
            resp = self._session.get(
                STEAM_NEWS_URL,
                params={"appid": self.appid, "count": self.news_count,
                        "maxlength": self.news_maxlen, "format": "json"},
                timeout=45)
            data = resp.json()
            items = ((data.get("appnews") or {}).get("newsitems") or [])
        except Exception as exc:
            log.warning("Steam 新闻获取失败: %s", exc)
            return []
        out = []
        for it in items:
            try:
                ts = int(it.get("date") or 0)
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            except (TypeError, ValueError, OSError):
                date_str = ""
            out.append({
                "title": str(it.get("title") or "").strip(),
                "url": str(it.get("url") or ""),
                "date_str": date_str,
                "feed": str(it.get("feedlabel") or ""),
                "contents": " ".join(str(it.get("contents") or "").split())[:self.news_maxlen],
            })
        log.info("Steam 新闻/公告：获取 %d 条", len(out))
        return out

    # ---------------- 市场异动 ----------------
    def market_anomalies(self) -> list[dict]:
        if self.scraper is None:
            return []
        try:
            data = self.scraper.item_change_list(page=1, page_size=self.anomaly_size)
        except Exception as exc:
            log.warning("市场异动获取失败: %s", exc)
            return []
        out = []
        for row in (data.get("list") or []):
            if not isinstance(row, dict):
                continue
            out.append({
                "name": row.get("name") or "",
                "marketHashName": row.get("marketHashName") or "",
                "platform": row.get("platform") or "",
                "summary": summarise_anomaly(row),
            })
        log.info("市场异动：获取 %d 条", len(out))
        return out

    # ---------------- 汇总与存档 ----------------
    def collect(self, out_dir: Path | None = None) -> dict:
        if not self.enabled:
            return {}
        # 优先用 SteamDT「CS2 更新记录」（中文 + 市场影响评级），失败再退回 Steam 新闻
        news = self.update_records()
        if not news:
            news = self.steam_news()
        intel = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "news": news,
            "anomalies": self.market_anomalies(),
        }
        if out_dir:
            try:
                out_dir = Path(out_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{intel['date']}.json").write_text(
                    json.dumps(intel, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                log.warning("情报存档失败: %s", exc)
        return intel
