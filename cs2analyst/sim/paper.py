"""模拟盘（纸上交易）：虚拟资金自由操盘，每日记录净值/操作/想法。

规则（我的资金管理框架 + 平台交易规则）：
- 初始资金、费率、现金储备、单票仓位上限均可在 config.yaml 的 sim 段配置；
- **T+7 交易规则**：买入后 7 天内不可卖出（锁定期内卖出指令自动顺延，并记入手记）；
- 买入/加仓按「信心度 → 目标仓位」计算，扣减现金，保留最低现金储备；
- 大盘高风险时提高现金储备下限（防守），但不禁止买入——好机会小仓参与；
- 卖出收取手续费（模拟悠悠有品卖家佣金），买入免手续费；
- 每日本地持久化：状态、成交记录、每日净值日志（data/sim/）。
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..engine.decision import Action, Decision
from ..logging_setup import get_logger

log = get_logger(__name__)


def _as_date(s: str) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


class PaperTrader:
    """模拟盘账户（单轨）。"""

    def __init__(self, state_path: Path, initial_capital: float = 3000.0,
                 fee_rate: float = 0.01, min_cash_ratio: float = 0.15,
                 high_risk_cash_ratio: float = 0.30, max_position_pct: float = 25.0,
                 hold_days_min: int = 7, label: str = "A"):
        self.state_path = Path(state_path)
        self.fee_rate = fee_rate
        self.min_cash_ratio = min_cash_ratio
        self.high_risk_cash_ratio = high_risk_cash_ratio
        self.max_position_pct = max_position_pct
        self.hold_days_min = int(hold_days_min)
        self.label = label
        self.state = self._load(initial_capital)

    # ------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------
    def _load(self, initial_capital: float) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                log.info("模拟盘状态已加载：初始资金 ¥%.2f，现金 ¥%.2f，持仓 %d 项，已运行 %d 天",
                         state["initial_capital"], state["cash"],
                         len(state.get("positions", {})), len(state.get("journal", [])))
                return state
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("模拟盘状态文件损坏（%s），重新初始化", exc)
        state = {
            "created": datetime.now().strftime("%Y-%m-%d"),
            "label": self.label,
            "initial_capital": float(initial_capital),
            "fee_rate": self.fee_rate,
            "hold_days_min": self.hold_days_min,
            "cash": float(initial_capital),
            "positions": {},        # {name: {qty, avg_cost, buy_date, locked_until}}
            "trade_log": [],        # [{date,time,action,name,qty,price,amount,fee,reason}]
            "journal": [],          # [{date, market, cash, positions_value, net_value, return_pct, trades, thoughts}]
        }
        self._save(state)
        return state

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------
    # 账户计算
    # ------------------------------------------------------------
    def equity(self, prices: dict[str, float]) -> float:
        """总权益 = 现金 + 持仓市值（按当日价格标记）。"""
        value = 0.0
        for name, pos in self.state["positions"].items():
            price = prices.get(name)
            if price is not None and price > 0:
                value += pos["qty"] * price
        return round(self.state["cash"] + value, 2)

    def position_value(self, name: str, price: float) -> float:
        pos = self.state["positions"].get(name)
        return pos["qty"] * price if pos else 0.0

    def _cash_reserve(self, equity: float, market_risk: str) -> float:
        ratio = self.high_risk_cash_ratio if market_risk == "高风险" else self.min_cash_ratio
        return round(equity * ratio, 2)

    # ------------------------------------------------------------
    # 交易执行
    # ------------------------------------------------------------
    def _trade(self, date_str: str, action: Action, name: str, qty: int,
               price: float, reason: str) -> None:
        """记录一笔成交并更新现金/持仓。买入会重新计算 T+N 锁定期。"""
        amount = round(qty * price, 2)
        if action in (Action.BUY, Action.ADD):
            fee = 0.0
            self.state["cash"] = round(self.state["cash"] - amount, 2)
            lock_until = _as_date(date_str) + timedelta(days=self.hold_days_min)
            pos = self.state["positions"].get(name)
            if pos:
                total_cost = pos["qty"] * pos["avg_cost"] + amount
                pos["qty"] += qty
                pos["avg_cost"] = round(total_cost / pos["qty"], 2)
                # 加仓会延长锁定期（取较晚者）
                pos["locked_until"] = max(str(pos.get("locked_until") or ""),
                                          lock_until.isoformat())
            else:
                self.state["positions"][name] = {
                    "qty": qty, "avg_cost": round(price, 2),
                    "buy_date": date_str, "locked_until": lock_until.isoformat(),
                }
        else:  # REDUCE / CLEAR
            fee = round(amount * self.fee_rate, 2)
            proceeds = round(amount - fee, 2)
            self.state["cash"] = round(self.state["cash"] + proceeds, 2)
            pos = self.state["positions"].get(name)
            if pos:
                pos["qty"] -= qty
                if pos["qty"] <= 0:
                    del self.state["positions"][name]
        self.state["trade_log"].append({
            "date": date_str,
            "time": datetime.now().strftime("%H:%M:%S"),
            "action": action.value,
            "name": name,
            "qty": qty,
            "price": price,
            "amount": amount,
            "fee": fee,
            "reason": reason[:200],
        })
        log.info("  [%s轨] 模拟盘成交：%s %s %d 件 @¥%.2f（金额 ¥%.2f，手续费 ¥%.2f）",
                 self.label, action.value, name, qty, price, amount, fee)

    def lock_status(self, name: str, date_str: str) -> tuple[bool, str, int]:
        """返回 (是否锁定, 可售日期, 剩余天数)。"""
        pos = self.state["positions"].get(name)
        if not pos:
            return False, "", 0
        until = str(pos.get("locked_until") or "")
        if not until:
            return False, "", 0
        try:
            days_left = (_as_date(until) - _as_date(date_str)).days
        except ValueError:
            return False, until, 0
        return days_left > 0, until, max(0, days_left)

    def execute_day(self, date_str: str, market: Any,
                    decisions: dict[str, Decision],
                    prices: dict[str, float]) -> dict:
        """执行当日决策并写入日志。同一日期只执行一次（幂等）。返回当日摘要。"""
        for jr in self.state["journal"]:
            if jr.get("date") == date_str:
                log.info("模拟盘今日（%s）已执行过，跳过重复执行", date_str)
                return jr
        eq = self.equity(prices)
        reserve = self._cash_reserve(eq, market.risk)
        trades = 0
        locked_notes: list[str] = []

        for name, d in decisions.items():
            price = prices.get(name)
            if not price or price <= 0 or d.action == Action.HOLD:
                continue
            pos = self.state["positions"].get(name)
            pos_value = pos["qty"] * price if pos else 0.0

            if d.action == Action.BUY:
                if pos:
                    continue  # 已有持仓应走 ADD
                target = d.target_weight_pct or 6.0
                target_amount = min(eq * target / 100.0 - pos_value,
                                    self.state["cash"] - reserve)
                if target_amount >= price:
                    qty = math.floor(target_amount / price)
                    if qty >= 1:
                        self._trade(date_str, d.action, name, qty, price,
                                    "；".join(d.reasons))
                        trades += 1
            elif d.action == Action.ADD:
                if not pos:
                    continue
                target = d.target_weight_pct or 6.0
                target_amount = min(eq * target / 100.0 - pos_value,
                                    self.state["cash"] - reserve)
                if target_amount >= price:
                    qty = math.floor(target_amount / price)
                    if qty >= 1:
                        self._trade(date_str, d.action, name, qty, price,
                                    "；".join(d.reasons))
                        trades += 1
            elif d.action in (Action.REDUCE, Action.CLEAR):
                if not pos:
                    continue
                # T+N 交易规则：锁定期内不可卖出，顺延到可售日
                locked, until, days_left = self.lock_status(name, date_str)
                if locked:
                    note = (f"{name}：T+{self.hold_days_min} 锁定期内"
                            f"（可售日 {until}，还剩 {days_left} 天），本次卖出顺延")
                    locked_notes.append(note)
                    log.info("  [%s轨] %s", self.label, note)
                    continue
                if d.action == Action.REDUCE:
                    qty = min(max(1, math.ceil(pos["qty"] * 0.4)), pos["qty"])
                else:
                    qty = pos["qty"]
                self._trade(date_str, d.action, name, qty, price,
                            "；".join(d.reasons))
                trades += 1

        # ---- 当日结算与想法记录 ----
        eq_after = self.equity(prices)
        ret_pct = (eq_after / self.state["initial_capital"] - 1) * 100
        thoughts = self._compose_thoughts(market, decisions, trades, locked_notes)
        entry = {
            "date": date_str,
            "track": self.label,
            "market": {"trend": market.trend, "risk": market.risk,
                       "tone": market.base_tone},
            "cash": self.state["cash"],
            "positions_value": round(eq_after - self.state["cash"], 2),
            "net_value": eq_after,
            "return_pct": round(ret_pct, 2),
            "trades": trades,
            "locked_notes": locked_notes,
            "thoughts": thoughts,
        }
        self.state["journal"].append(entry)
        self._save(self.state)
        log.info("[%s轨] 模拟盘结算：净值 ¥%.2f（%+.2f%%），现金 ¥%.2f，持仓 %d 项%s",
                 self.label, eq_after, ret_pct, self.state["cash"],
                 len(self.state["positions"]),
                 f"，锁定期顺延 {len(locked_notes)} 笔" if locked_notes else "")
        return entry

    @staticmethod
    def _compose_thoughts(market: Any, decisions: dict[str, Decision],
                          trades: int, locked_notes: list[str] | None = None) -> str:
        """生成当日操盘手记（我的想法）。"""
        parts = [f"大盘{market.trend}、{market.risk}。"]
        acts = []
        for name, d in decisions.items():
            if d.action == Action.HOLD:
                continue
            acts.append(f"{name}：{d.label}（{'；'.join(d.reasons)[:80]}）")
        if acts:
            parts.append("今日操作：" + "；".join(acts[:6]) + "。")
        else:
            parts.append("今日无操作：所有标的不满足介入/退出条件，耐心等待信号。")
        if locked_notes:
            parts.append(f"T+{7} 锁定期内有 {len(locked_notes)} 笔卖出指令顺延（平台规则不可当日买入当日卖出）。")
        if trades:
            parts.append("已按计划执行成交，纪律优先，不被短期波动干扰。")
        else:
            parts.append("现金保持观望，宁可错过不可做错，等待赔率更优的机会。")
        return "".join(parts)

    # ------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------
    def snapshot(self, prices: dict[str, float], as_of: str | None = None) -> dict:
        """当前账户快照（用于报告/命令行）。as_of 用于计算锁定期剩余天数。"""
        eq = self.equity(prices)
        today = as_of or datetime.now().strftime("%Y-%m-%d")
        positions = []
        for name, pos in self.state["positions"].items():
            price = prices.get(name, pos["avg_cost"])
            locked, until, days_left = self.lock_status(name, today)
            positions.append({
                "name": name, "qty": pos["qty"], "avg_cost": pos["avg_cost"],
                "price": price,
                "pnl_pct": round((price / pos["avg_cost"] - 1) * 100, 2),
                "value": round(pos["qty"] * price, 2),
                "locked_until": until,
                "days_left": days_left,
                "locked": locked,
            })
        journal = self.state["journal"]
        return {
            "label": self.label,
            "created": self.state["created"],
            "initial_capital": self.state["initial_capital"],
            "hold_days_min": self.hold_days_min,
            "cash": self.state["cash"],
            "net_value": eq,
            "return_pct": round((eq / self.state["initial_capital"] - 1) * 100, 2),
            "positions": positions,
            "journal": journal,
            "trade_count": len(self.state["trade_log"]),
        }


def reset_state(state_path: Path) -> None:
    """重置模拟盘（删除状态文件）。"""
    p = Path(state_path)
    if p.exists():
        p.unlink()
