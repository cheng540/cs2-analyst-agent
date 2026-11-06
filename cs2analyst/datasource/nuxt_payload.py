"""Nuxt 3 SSR 载荷（devalue 格式）解析器。

SteamDT 官网单品详情页（/mkt/{marketHashName}）把商品数据渲染在
<script type="application/json" id="__NUXT_DATA__"> 中。该载荷是 Nuxt 3 的
扁平化引用格式：数组下标即引用（值位置的整数=引用，真实整数以字符串存储）。

本模块提供通用解析器与单品数据提取，仅用于解析公开页面数据。
"""
from __future__ import annotations

import json
import re
from typing import Any

_MARKERS = ("ShallowReactive", "Reactive", "Ref", "ShallowRef")

_PAYLOAD_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def resolve_nuxt_payload(payload: list, max_depth: int = 16) -> Any:
    """解析 Nuxt 3 载荷：把引用下标还原为对象。"""
    def _resolve(v: Any, depth: int, seen: frozenset) -> Any:
        if depth > max_depth:
            return "<deep>"
        if isinstance(v, list):
            # 标记形如 ["ShallowReactive", idx]
            if (len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)
                    and 0 <= v[1] < len(payload) and v[0] in _MARKERS):
                return _resolve(payload[v[1]], depth + 1, seen)
            return [_resolve(x, depth + 1, seen) for x in v]
        if isinstance(v, dict):
            return {k: _resolve(vv, depth + 1, seen) for k, vv in v.items()}
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(payload):
            if v in seen:
                return f"<cycle {v}>"
            return _resolve(payload[v], depth + 1, seen | {v})
        return v

    root = _resolve(payload[1], 0, frozenset({1}))
    return root if isinstance(root, dict) else {}


def parse_payload_from_html(html: str) -> Any:
    """从页面 HTML 提取并解析 Nuxt 载荷（失败抛 ValueError）。"""
    m = _PAYLOAD_RE.search(html)
    if not m:
        raise ValueError("页面中未找到 __NUXT_DATA__ 载荷")
    payload = json.loads(m.group(1))
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("载荷格式异常")
    return resolve_nuxt_payload(payload)


def extract_item_dict(html: str) -> dict:
    """从单品详情页 HTML 提取主商品对象（含价格/存世量/涨跌等）。"""
    root = parse_payload_from_html(html)
    data = root.get("data") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        raise ValueError("载荷中未找到 data")
    for val in data.values():
        inner = val.get("data") if isinstance(val, dict) else None
        if isinstance(inner, dict) and "marketHashName" in inner and "sellingPriceList" in inner:
            return inner
    raise ValueError("载荷中未找到商品数据对象")
