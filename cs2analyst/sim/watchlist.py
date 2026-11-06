"""模拟盘观察列表加载。"""
from __future__ import annotations

import json
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


def load_watchlist(path: Path) -> list[str]:
    """加载模拟盘观察列表（JSON 数组或 {"items": [...]} 结构）。返回饰品名列表。"""
    path = Path(path)
    if not path.exists():
        log.warning("观察列表不存在：%s（模拟盘将跳过）", path)
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("items") or []
    names: list[str] = []
    for entry in data:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and entry.get("market_hash_name"):
            names.append(entry["market_hash_name"])
    return names
