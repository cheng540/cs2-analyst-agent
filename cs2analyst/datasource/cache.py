"""本地数据缓存：原子写入 JSON 快照，积累历史数据（K线合并、每日价格快照）。

所有数据仅保存在本地 data/ 目录，不上传任何第三方服务器。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)


class DataCache:
    """data/ 目录下的 JSON 文件缓存。

    目录结构：
      data/
        market/broad_kline_2.json        # 大盘日K（跨天合并去重）
        market/broad_index.json          # 大盘指数快照
        items/{safe_name}/kline_2.json   # 单品日K（跨天合并去重）
        snapshots/YYYY-MM-DD_prices.json # 每日价格快照（积累流动性时序）
    """

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 底层读写 ----
    def save(self, rel_path: str, obj: Any) -> Path:
        """原子写入 JSON（临时文件 + 替换）。"""
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, p)
        return p

    def load(self, rel_path: str) -> Any | None:
        p = self.root / rel_path
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("缓存文件读取失败 %s: %s", p, exc)
            return None

    # ---- 业务封装 ----
    def merge_kline(self, rel_path: str, new_rows: list[list]) -> list[list]:
        """合并K线：旧缓存 + 新数据按时间戳去重，返回完整行列表并落盘。"""
        old = self.load(rel_path) or []
        merged: dict[int, list] = {}
        for row in old:
            if isinstance(row, list) and row:
                try:
                    merged[int(row[0])] = row
                except (TypeError, ValueError):
                    continue
        for row in new_rows:
            if isinstance(row, list) and row:
                try:
                    merged[int(row[0])] = row
                except (TypeError, ValueError):
                    continue
        rows = [merged[k] for k in sorted(merged)]
        self.save(rel_path, rows)
        return rows

    def save_daily_prices(self, payload: dict) -> Path:
        """保存当日价格快照（用于积累在售/求购量时序）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.save(f"snapshots/{today}_prices.json", payload)

    def recent_price_snapshots(self, days: int = 14) -> list[dict]:
        """读取最近 N 天的价格快照（升序）。"""
        snap_dir = self.root / "snapshots"
        if not snap_dir.exists():
            return []
        files = sorted(snap_dir.glob("*_prices.json"))[-days:]
        out = []
        for f in files:
            data = self.load(str(f.relative_to(self.root)))
            if data:
                out.append(data)
        return out
