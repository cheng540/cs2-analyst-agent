"""持仓数据加载：本地 JSON/CSV 文件。

JSON 格式（config/inventory.json）：
[
  {
    "market_hash_name": "AK-47 | Redline (Field-Tested)",
    "wear": "FT",
    "quantity": 2,
    "buy_price": 96.5,
    "buy_date": "2026-03-10",
    "platform": "YOUPIN",
    "special_style": "",
    "note": "",
    "lots": [90.0, 103.0],     // 可选：同一饰品多次买入、价格不同时逐件记录
    "status": "",              // 可选：用户意图，如「待售」（已决定卖出但还没卖掉）
    "unlocked": false          // 可选：用户确认「已过 7 天锁定期」（buy_date 缺失时的兜底）
  }
]

CSV 表头：market_hash_name,wear,quantity,buy_price,buy_date,platform,special_style,note,lots,status,unlocked
（CSV 里 lots 用逗号分隔且**必须加引号**，如 "90,103"——否则逗号会被当成列分隔符；
  忘了加引号也不会算错：分件数对不上数量时会自动退回均价，并在报告里标注）
后续如需对接悠悠有品库存接口，在此模块新增 InventoryAdapter 即可（插件化预留）。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Holding:
    """单条持仓记录。"""
    market_hash_name: str
    wear: str = ""
    quantity: int = 1
    buy_price: float = 0.0
    buy_date: str = ""
    platform: str = "YOUPIN"
    special_style: str = ""
    note: str = ""
    manual_price: float = 0.0   # 手动估值（贴纸等无API行情的品类，按最新市价填写）
    # 分件成本：同一饰品多次买入、价格不同时逐件记录（如 [164, 234]）。
    # 不填则按 buy_price 均价展开——用于「先卖哪一件」这类按件分析。
    lots: list = field(default_factory=list)
    # 用户意图标记：如「待售」（已决定卖出，只是还没卖掉）。
    # 系统据此直接给出清仓建议，而不是继续建议"持有观望"。
    status: str = ""
    # 用户确认「早就过了 7 天锁定期」（买入日期没记录时的兜底开关）。
    # 为什么需要：T+7 锁定期靠 buy_date + 7 天算，日期缺失时系统只能说"锁定期未知"，
    # 告警点评就写成「若已过锁定期…若仍在锁定期…」这种两头都说的废话。
    # 用户明确说过「手上的这些早就过了七天」，所以置 true；**以后新买的仍要填真实 buy_date**。
    unlocked: bool = False

    @property
    def total_cost(self) -> float:
        return round(self.buy_price * self.quantity, 2)

    @property
    def lot_prices(self) -> list[float]:
        """逐件成本；未记录或数量对不上时，按均价展开 quantity 份。"""
        vals: list[float] = []
        for x in (self.lots or []):
            try:
                v = float(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
        if vals and len(vals) == self.quantity:
            return vals
        return [round(self.buy_price, 2)] * max(0, self.quantity)

    @property
    def lots_differ(self) -> bool:
        """各件成本是否不同（决定报告里要不要单列分件成本）。"""
        vals = self.lot_prices
        return len(vals) > 1 and len({round(v, 2) for v in vals}) > 1

    @property
    def sell_intent(self) -> bool:
        """用户是否已决定卖出（status 或备注里含「待售」）。"""
        text = f"{self.status} {self.note}"
        return "待售" in text


def variant_warnings(h: Holding) -> list[str]:
    """名称版本一致性检查：备注里的「纪念品 / 暗金 / 刀具」必须与英文名一致。

    2026-09-24 实际踩到：备注写着「SG 553 (纪念品) | 白骨」，但 market_hash_name 填的是
    普通版 `SG 553 | Bleached`。系统按普通版报价 ¥2.79，而用户手里是纪念品版（真实在售 ¥1.28），
    结果「卖出价比报告价低 53%」看起来像流动性差/卖亏了，实际是**名字用错了**。
    —— 这是第二次「名字写错伪装成别的问题」（第一次是把探员写成贴纸，伪装成「数据源不支持」）。
    """
    name = str(h.market_hash_name or "")
    low = name.lower()
    note = str(h.note or "")
    out: list[str] = []
    if "纪念品" in note and not low.startswith("souvenir"):
        out.append("备注写了「纪念品」，英文名却不是 Souvenir 版（两者是两个物品，价格可能差几倍）")
    if ("暗金" in note or "stattrak" in note.lower()) and "stattrak" not in low:
        out.append("备注写了「暗金/StatTrak」，英文名里却没有 StatTrak")
    if ("★" in note or "刀具" in note) and not name.startswith("★"):
        out.append("备注写了刀具（★/刀具），英文名里却没有 ★")
    return out


def load_inventory(path: Path) -> list[Holding]:
    """从 JSON 或 CSV 加载持仓列表。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"持仓文件不存在: {path}。请将持仓明细填入该文件（参考 config/inventory.example.json）")
    suffix = path.suffix.lower()
    if suffix == ".json":
        items = _load_json(path)
    elif suffix == ".csv":
        items = _load_csv(path)
    else:
        raise ValueError(f"不支持的持仓文件格式: {suffix}（支持 .json / .csv）")
    known = {f.name for f in fields(Holding)}
    holdings: list[Holding] = []
    for it in items:
        if not it.get("market_hash_name"):
            continue
        extra = sorted(set(it) - known)
        if extra:
            log.warning("持仓条目 %s 含未知字段（已忽略）：%s", it["market_hash_name"], "、".join(extra))
        holdings.append(Holding(**{k: v for k, v in it.items() if k in known}))
    if not holdings:
        raise ValueError(f"持仓文件为空或格式错误: {path}")
    # 名称版本一致性：写错名字会让系统按另一个物品报价（且完全不报错），必须显式警告
    for h in holdings:
        for w in variant_warnings(h):
            log.warning("⚠ 持仓名称可能不一致：%s —— %s（请核对悠悠有品里的准确名称）",
                        h.market_hash_name, w)
    log.info("已加载持仓 %d 个条目，共 %d 件饰品%s", len(holdings), sum(h.quantity for h in holdings),
             f"（其中待售 {sum(1 for h in holdings if h.sell_intent)} 条）"
             if any(h.sell_intent for h in holdings) else "")
    return holdings


def _load_json(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("持仓 JSON 必须是数组格式")
    return [d for d in data if isinstance(d, dict)]


def _load_csv(path: Path) -> list[dict]:
    items: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row.get("market_hash_name"):
                continue
            items.append({
                "market_hash_name": row["market_hash_name"].strip(),
                "wear": row.get("wear", "").strip(),
                "quantity": int(float(row.get("quantity", 1) or 1)),
                "buy_price": float(row.get("buy_price", 0) or 0),
                "buy_date": row.get("buy_date", "").strip(),
                "platform": row.get("platform", "YOUPIN").strip(),
                "special_style": row.get("special_style", "").strip(),
                "note": row.get("note", "").strip(),
                "manual_price": float(row.get("manual_price", 0) or 0),
                # lots 用逗号分隔（中英文逗号都认）：164,234
                "lots": [float(x) for x in
                         (row.get("lots") or "").replace("，", ",").split(",") if x.strip()],
                "status": row.get("status", "").strip(),
                # CSV 里写 1/true/yes 都算已过锁定期
                "unlocked": row.get("unlocked", "").strip().lower() in ("1", "true", "yes", "y", "是"),
            })
    return items
