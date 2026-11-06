"""饰品名称映射：英文 marketHashName → 中文名（报告展示用）。

数据来源优先级：
1. 本地缓存 data/name_map.json（一次解析，长期复用）
2. 用户自定义对照 config/name_overrides.json（优先级最高，可手工补录）
3. 开放平台「饰品基础信息」接口 /open/cs2/v1/base（中英对照全量，每日限 1 次）
4. 官网单品详情页 SSR 载荷（无严格限额，逐条兜底）

说明：API 查询必须使用英文 marketHashName，中文名仅用于报告展示。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


class NameResolver:
    """英文名 → 中文名解析器（带本地缓存与多级兜底）。"""

    def __init__(self, cache_path: Path, overrides_path: Path | None = None,
                 client=None, scraper=None, max_page_lookups: int = 40):
        self.cache_path = Path(cache_path)
        self.overrides_path = Path(overrides_path) if overrides_path else None
        self.client = client          # SteamDTClient（用于 /open/cs2/v1/base）
        self.scraper = scraper        # SteamDTScraper（用于单品页兜底）
        self.max_page_lookups = max_page_lookups
        self._data = self._load()
        self.overrides = self._load_overrides()

    # ---------------- 缓存读写 ----------------
    def _load(self) -> dict:
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and isinstance(data.get("map"), dict):
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("名称缓存读取失败（%s），将重建", exc)
        return {"_base_fetched": "", "map": {}}

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cache_path)

    def _load_overrides(self) -> dict:
        if self.overrides_path and self.overrides_path.exists():
            try:
                with open(self.overrides_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return {k: v for k, v in data.items()
                        if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")}
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("名称对照表读取失败：%s", exc)
        return {}

    # ---------------- 解析 ----------------
    def resolve(self, market_hash_name: str) -> str:
        """返回中文名；解析不到时返回空字符串（报告将只显示英文名）。"""
        return self.resolve_many([market_hash_name]).get(market_hash_name, "")

    def resolve_many(self, names: list[str]) -> dict[str, str]:
        """批量解析。返回 {英文名: 中文名}（未解析到的值为空字符串）。"""
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            return {}
        # 1) 用户自定义对照（最高优先级）
        for n in names:
            if n in self.overrides:
                self._data["map"][n] = self.overrides[n]
        missing = [n for n in names if not self._data["map"].get(n)]
        # 2) 开放平台全量基础信息（每日 1 次）
        if missing and self.client is not None:
            today = datetime.now().strftime("%Y-%m-%d")
            if self._data.get("_base_fetched") != today:
                self._fetch_base()
                missing = [n for n in names if not self._data["map"].get(n)]
        # 3) 官网单品页逐条兜底
        if missing and self.scraper is not None:
            for n in missing[:self.max_page_lookups]:
                cn = self._fetch_item_page(n)
                if cn:
                    self._data["map"][n] = cn
        self._save()
        return {n: self._data["map"].get(n, "") for n in names}

    def refresh_catalog(self, days: int = 7) -> int:
        """定期刷新全量中英对照表（默认 7 天一次），返回新增条数。

        全量目录来自开放平台「饰品基础信息」接口（每日限 1 次），含 4 万+ 饰品；
        刷新后网页问答可以直接识别任意中文饰品名。
        """
        last = str(self._data.get("_base_fetched") or "")
        if last:
            try:
                age = (datetime.now().date() - datetime.strptime(last, "%Y-%m-%d").date()).days
                if age < days:
                    return 0
            except ValueError:
                pass
        if self.client is None:
            return 0
        before = len(self._data.get("map") or {})
        self._fetch_base()
        self._save()
        return len(self._data.get("map") or {}) - before

    # ---------------- 数据源 ----------------
    def _fetch_base(self) -> None:
        """拉取全量中英对照（/open/cs2/v1/base，每日限额 1 次）。"""
        try:
            data = self.client._request("GET", "/open/cs2/v1/base")
        except Exception as exc:
            log.warning("基础信息接口不可用（%s），改用单品页兜底", exc)
            return
        items = data.get("data") or []
        added = 0
        for it in items:
            mhn = it.get("marketHashName")
            cn = it.get("name")
            if mhn and cn and not self._data["map"].get(mhn):
                self._data["map"][mhn] = cn
                added += 1
        self._data["_base_fetched"] = datetime.now().strftime("%Y-%m-%d")
        log.info("名称对照表已更新：新增 %d 条（缓存共 %d 条）",
                 added, len(self._data["map"]))

    def _fetch_item_page(self, market_hash_name: str) -> str:
        try:
            detail = self.scraper.item_detail(market_hash_name)
            return str(detail.get("name") or "")
        except Exception as exc:
            log.debug("单品页名称解析失败 %s: %s", market_hash_name, exc)
            return ""
