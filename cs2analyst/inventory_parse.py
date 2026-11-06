"""把「从平台复制的文本」解析成持仓候选条目。

为什么做这个（而不是识图）：
    持仓录入的痛点是"十几条要一条条敲英文名"，不是"看不出图里写了什么"。
    文本解析可控：错了能看出来、能改；识图错一个字符，报告会按另一个物品报价且不报错。

定位：**只负责给出初值 + 可疑点，不负责准确**。
    解析结果一律进前端预览表，由人确认后才写库。所以这里的每个 warning 都是给用户看的。

支持的典型行（从悠悠/Steam/BUFF 库存页复制出来的）：
    AK-47 | Redline (Field-Tested)          1    169.00
    AK-47 | Redline (Field-Tested)  x1  ¥169
    手套武器箱	2	184
    法玛斯 | ZX81 彩色（崭新出厂） 164
    - USP消音版 | 印花集 (崭新出厂) 237.9 1件
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 全角 → 半角（只处理会影响解析的字符）
_FW = str.maketrans("０１２３４５６７８９（）￥，　｜＊ｘ", "0123456789()¥, |*x")

_BULLET = re.compile(r"^\s*(?:[-*·•]|\d{1,2}[.)、]|[①-⑳])\s*")
_WEAR_EN = r"(?:Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred|FN|MW|FT|WW|BS)"
_WEAR_TAIL = re.compile(rf"\(\s*{_WEAR_EN}\s*\)\s*$", re.IGNORECASE)
_CN_WEAR = {
    "崭新出厂": "Factory New", "略有磨损": "Minimal Wear", "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn", "战痕累累": "Battle-Scarred",
}
_CN_WEAR_TAIL = re.compile(rf"[（(]\s*(?:{'|'.join(_CN_WEAR)})\s*[)）]\s*$")

_HEADER_WORDS = ("名称", "饰品", "单价", "价格", "数量", "件数", "成本", "买入", "market", "name",
                 "price", "qty", "quantity", "合计", "总计", "小计")
_SKIP_LINE = re.compile(r"^(?:#|//|【|——|—{2,}|=)")
# 装饰行/分隔行：全是符号，或含"分隔/表头/以下/备注"这类词
_DECOR = re.compile(r"[★☆=\-_~·•*#]{3,}|^(?:分隔|表头|以下|备注|说明|共\s*\d+\s*件)")

# 数量写法：×1 / x1 / 1件 / 2个 / 3把
_RE_QTY_MARK = re.compile(r"^[×xX*]\s*(\d{1,4})$|^(\d{1,4})\s*(?:件|个|把|双)$")
# 价格写法：¥169 / 169.00 / 1,690 元
_RE_PRICE = re.compile(r"^[¥￥]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*元?$")
# 纯整数（可能是数量也可能是价格）
_RE_INT = re.compile(r"^\d{1,4}$")


def _clean(line: str) -> str:
    text = str(line or "").translate(_FW)
    text = _BULLET.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_header(line: str) -> bool:
    low = line.lower()
    hits = sum(1 for w in _HEADER_WORDS if w in low)
    # 表头通常同时出现「名称/单价/数量」里至少两个词，且不含物品名特征
    return hits >= 2 and "|" not in line and "(" not in line


def _split_numbers(line: str) -> tuple[str, list[str]]:
    """把行尾的数字段剥下来。名称里的数字（AK-47、M4A1-S、Glock-18）因为有连字符/字母，不会被剥。"""
    tokens = line.split(" ")
    nums: list[str] = []
    while tokens and len(nums) < 3:
        last = tokens[-1]
        if _RE_QTY_MARK.match(last) or _RE_PRICE.match(last) or _RE_INT.match(last):
            nums.insert(0, tokens.pop())
            continue
        break
    return " ".join(tokens).strip(), nums


def _parse_numbers(nums: list[str]) -> tuple[int, float | None, list[str]]:
    """从数字段推断 (数量, 单价)。返回 warnings。"""
    warnings: list[str] = []
    qty_marked: int | None = None
    prices: list[float] = []
    ints: list[int] = []
    consumed_total: int | None = None      # 已判定为「总价」的那个数，避免重复提示

    for tok in nums:
        mark = _RE_QTY_MARK.match(tok)
        if mark:
            qty_marked = int(mark.group(1) or mark.group(2))
            continue
        price = _RE_PRICE.match(tok)
        if price:
            raw = price.group(1).replace(",", "")
            value = float(raw)
            if "." in raw or tok.startswith(("¥", "￥")):
                prices.append(value)
            else:
                ints.append(int(value))
            continue
        if _RE_INT.match(tok):
            ints.append(int(tok))

    price = prices[-1] if prices else None
    qty = qty_marked

    if price is None and ints:
        if len(ints) >= 3 and abs(ints[0] * ints[1] - ints[-1]) <= max(1.0, 0.02 * ints[-1]):
            # 「3 92 276」= 3 件 × 92 = 276，最后一列是总价
            qty, price = ints[0], float(ints[1])
            consumed_total = ints[-1]
            warnings.append(f"识别为「{ints[0]} 件 × {ints[1]} = {ints[-1]}」，已按件数 {ints[0]}、单价 {ints[1]} 处理。")
        elif len(ints) == 2:
            first, last = ints
            if first <= 500 and last > first:
                qty, price = first, float(last)
            else:
                price = float(last)
                warnings.append("两个数字无法确定哪个是单价，已取最后一个，请核对。")
        else:
            value = ints[0]
            if value <= 20 and qty is None:
                # 小整数更可能是「数量」而不是价格。宁可价格留空让人补，
                # 也不能把 1 件当成 ¥1 —— 错价格会静默算错盈亏，比缺价格危险得多。
                qty = value
                warnings.append("只识别到一个较小的整数，已当作数量；单价留空，请补填。")
            else:
                price = float(value)
                if value <= 20:
                    warnings.append("只识别到一个整数，已当作单价；如果它其实是数量，请改。")
    elif price is not None and ints and qty is None:
        candidate = ints[0]
        if 1 <= candidate <= 500 and price > candidate:
            qty = candidate
        elif 1 <= candidate <= 500:
            warnings.append("有一个整数可能是数量，但数值不小于单价，已忽略（按 1 件处理）。")

    if qty is None:
        qty = 1

    # 总价校验：若还有一个整数 ≈ 数量 × 单价，说明那是金额小计，不是数量
    if price is not None and len(ints) >= 2:
        for value in ints:
            if value == consumed_total:
                continue
            if abs(value - qty * price) <= max(1.0, 0.02 * qty * price) and value != price:
                warnings.append(f"行内有 {value}，接近「数量×单价」的总价，已忽略。")
                break

    return max(1, qty), price, warnings


def _strip_cn_wear(name: str) -> str:
    return _CN_WEAR_TAIL.sub("", name).strip()


def load_name_lookup(root: Path, extra_paths: list[Path] | None = None) -> dict[str, str]:
    """构建「中文名 → 官方英文名」反查表。

    默认来源：config/name_overrides.json（手工补充）与 data/name_map.json（系统自动维护）。
    `extra_paths` 用于覆盖数据目录的场景（如演示模式读 data/demo_run/name_map.json）。
    键做三种规范化（原样 / 去空格 / 去磨损括号），因为用户复制的写法不统一。
    """
    root = Path(root)
    lookup: dict[str, str] = {}
    sources = [root / "config" / "name_overrides.json", root / "data" / "name_map.json"]
    sources += [Path(p) for p in (extra_paths or [])]
    for path in sources:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        mapping = data.get("map") if isinstance(data, dict) and isinstance(data.get("map"), dict) else data
        if not isinstance(mapping, dict):
            continue
        for en, cn in mapping.items():
            if str(en).startswith("_") or not isinstance(cn, str) or not en:
                continue
            for key in {_clean(cn), _clean(cn).replace(" ", ""), _strip_cn_wear(_clean(cn)).replace(" ", "")}:
                if key:
                    lookup.setdefault(key, en)
    return lookup


def _resolve_name(name: str, lookup: dict[str, str]) -> tuple[str, str, list[str]]:
    """返回 (最终名称, 来源说明, warnings)。"""
    warnings: list[str] = []
    cleaned = name.strip()
    if not cleaned:
        return "", "", ["名称为空"]

    for key in (cleaned, cleaned.replace(" ", ""), _strip_cn_wear(cleaned).replace(" ", "")):
        hit = lookup.get(key)
        if hit:
            return hit, "中文名反查", warnings

    has_ascii = bool(re.search(r"[A-Za-z]", cleaned))
    has_cn = bool(re.search(r"[\u4e00-\u9fff]", cleaned))

    if not has_ascii and has_cn:
        warnings.append("只有中文名，系统认不出对应物品，请补官方英文名（带磨损档）。")
        return cleaned, "未识别", warnings

    if has_cn and has_ascii:
        warnings.append("名称里中英文混排，可能不是官方英文名，请核对。")

    # 武器箱/胶囊/贴纸/探员这类没有磨损档，只有带「|」的枪皮刀皮才要求写磨损
    if "|" in cleaned and not _WEAR_TAIL.search(cleaned):
        warnings.append("名称缺少磨损档（如 (Field-Tested)），写错会按另一个物品报价。")

    note = []
    low = cleaned.lower()
    if has_cn and "纪念品" in cleaned and not low.startswith("souvenir"):
        note.append("提到「纪念品」，但英文名不是 Souvenir 版")
    if has_cn and "暗金" in cleaned and "stattrak" not in low:
        note.append("提到「暗金」，但英文名没有 StatTrak")
    if has_cn and ("刀" in cleaned or "★" in cleaned) and not cleaned.startswith("★"):
        note.append("提到刀具，但英文名没有 ★ 前缀")
    warnings.extend(note)

    return cleaned, ("直接识别" if has_ascii else "未识别"), warnings


def parse_line(raw: str, lookup: dict[str, str] | None = None) -> dict | None:
    lookup = lookup or {}
    line = _clean(raw)
    if not line or _SKIP_LINE.match(line) or _DECOR.search(line) or _is_header(line):
        return None

    # 有制表符时先按表切：第一段最可能是名称
    if "\t" in str(raw):
        parts = [p.strip() for p in str(raw).replace("\t", " ").split(" ") if p.strip()]
        name, nums = _split_numbers(" ".join(parts))
    else:
        name, nums = _split_numbers(line)

    if not name:
        return None

    final_name, matched_by, warnings = _resolve_name(name, lookup)
    qty, price, num_warnings = _parse_numbers(nums)
    warnings = warnings + num_warnings

    if price is None:
        warnings.append("没识别到价格，导入后需要补填（否则盈亏算不对）。")
        confidence = "low"
    elif matched_by == "中文名反查" and not warnings:
        confidence = "high"
    elif matched_by == "直接识别" and _WEAR_TAIL.search(final_name) and not warnings:
        confidence = "high"
    elif matched_by == "未识别":
        confidence = "low"
    else:
        confidence = "medium"

    return {
        "raw": raw.strip(),
        "market_hash_name": final_name,
        "quantity": qty,
        "buy_price": round(price, 2) if price is not None else None,
        "confidence": confidence,
        "matched_by": matched_by,
        "warnings": warnings,
    }


def parse_inventory_text(text: str, lookup: dict[str, str] | None = None) -> dict:
    """整段文本 → 候选条目。空行、表头、注释行会被跳过并计数。"""
    items: list[dict] = []
    skipped = 0
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        parsed = parse_line(raw, lookup)
        if parsed is None:
            skipped += 1
            continue
        items.append(parsed)

    merged: dict[str, dict] = {}
    merged_rows = 0
    for item in items:
        key = item["market_hash_name"].lower()
        prev = merged.get(key)
        if prev is None:
            item["lots"] = ([item["buy_price"]] * item["quantity"]
                            if item["buy_price"] else [])
            merged[key] = item
            continue

        # 同一物品出现多行 = 分批买入：数量相加、价格取加权均价、逐件成本记进 lots
        merged_rows += 1
        total_qty = prev["quantity"] + item["quantity"]
        a, b = prev.get("buy_price"), item.get("buy_price")
        if a is not None and b is not None:
            avg = (a * prev["quantity"] + b * item["quantity"]) / total_qty
            prev["buy_price"] = round(avg, 2)
            prev["lots"] = (prev.get("lots") or []) + [b] * item["quantity"]
        prev["quantity"] = total_qty
        prev["_merged_rows"] = prev.get("_merged_rows", 0) + 1
        prev["warnings"] = list(dict.fromkeys(prev["warnings"] + item["warnings"]))
        rank = {"high": 0, "medium": 1, "low": 2}
        if rank[item["confidence"]] > rank[prev["confidence"]]:
            prev["confidence"] = item["confidence"]

    out = list(merged.values())
    for item in out:
        count = item.pop("_merged_rows", 0)
        if count:
            lots = item.get("lots") or []
            varied = len({round(v, 2) for v in lots}) > 1
            item["warnings"].append(
                f"同名出现 {count + 1} 次，已合并为 {item['quantity']} 件、均价 {item['buy_price']}"
                + (f"、分件成本 {[round(v, 2) for v in lots]}" if varied else "") + "。")
        if item.get("lots") and len(item["lots"]) != item["quantity"]:
            item["lots"] = []                  # 与数量对不上就丢掉，让系统退回均价
    duplicates = merged_rows
    summary = {
        "total": len(out),
        "skipped_lines": skipped,
        "duplicates": duplicates,
        "need_review": sum(1 for i in out if i["confidence"] != "high"),
        "missing_price": sum(1 for i in out if i["buy_price"] is None),
    }
    return {
        "items": out,
        "summary": summary,
        "hint": "解析只给初值：重点核对「英文名 + 磨损档 + 单价」三列，确认后再导入。"
                "名称写错会按另一个物品报价，而且不会报错。",
    }
