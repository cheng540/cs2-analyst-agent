"""网页问答引擎：工具取数 → 大模型判断 → 数字交叉校验 → 存档。

防幻觉三件套：
1. 事实归工具：模型必须调用工具拿数据，禁止凭记忆报价格；
2. 数字校验：答案里出现的 ¥ / % 数字，必须在工具返回的数据中能找到，否则标注"未经验证"；
3. 依据透明：每条回答都附带工具调用与原始数据，用户可自行核对。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..llm.client import LLMClient
from ..logging_setup import get_logger
from .tools import TOOL_SCHEMAS, ToolBox

log = get_logger(__name__)

MAX_TOOL_ROUNDS = 4
MAX_HISTORY = 8

SYSTEM_PROMPT = """你是「CS2 饰品持仓分析助手」，通过网页回答用户关于饰品行情、个人持仓与模拟盘的问题。

铁律（必须遵守）：
1. 任何价格、涨跌幅、指标、盈亏数字都必须来自工具返回的数据，禁止凭记忆或推测给数字；
   工具返回 error 或没有的数据，就如实说"这个我查不到"，绝不允许编造。
2. 回答前先判断需要哪些数据，并调用相应工具（单品快照/技术分析/大盘环境/我的持仓/模拟盘/最新情报/经验库）。
   涉及"值不值得买/卖"这类问题，至少要知道：单品实时价 + 技术面 + 大盘环境。
3. 结论要具体：给方向（值得/不值得/观望/等什么价位）、给理由（引用具体数字）、给关键风险。
4. 推测性内容必须显式标注"（推测）"，不能说得像事实。
5. 平台规则：悠悠有品 T+7（买入后 7 天内不能卖出）；买入免手续费、**卖出收 1%**，
   卖出所得进钱包，**提现再收 1%**——所以「继续买饰品」成本 1%、「提现成现金」约 2%。
   给交易建议时必须把这两点考虑进去。
6. 中文回答，简洁（一般 300 字以内），可用短列表；不要输出 JSON；不要空话套话。
7. 若用户说的饰品名称不确定，把候选列给他让他确认，不要自己猜一个。
8. 经验库工具 search_knowledge 返回的是**历史案例、平台规则、饰品常识、公告**，
   用来解释「以前类似情况怎么判断、后来怎么走」「为什么这么定规则」。
   其中的数字是**当时的历史数字**，绝不能当现在的价格/持仓用；
   问「现在多少钱」必须走单品快照类工具。引用案例要说清日期与来源。"""


def _normalize_number(tok: str) -> str:
    t = tok.replace("¥", "").replace(",", "").replace("%", "").strip()
    try:
        f = float(t)
    except ValueError:
        return t
    if f == int(f):
        return str(int(f))
    return f"{f:.4f}".rstrip("0").rstrip(".")


def extract_significant_numbers(text: str) -> list[str]:
    """提取需要核对的数字（¥金额、百分比、以及 2 位以上的数字）。"""
    # 先去掉日期与常见型号标记，避免误报
    cleaned = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", text)
    cleaned = re.sub(r"T\+\d+", " ", cleaned)
    cleaned = re.sub(r"MA\d+", " ", cleaned)
    cleaned = re.sub(r"\d+\s*(个月|月|日|天|年|件|笔|次|人|根)", " ", cleaned)
    toks = re.findall(r"¥\s?-?\d[\d,]*(?:\.\d+)?%?|-?\d[\d,]*(?:\.\d+)?%", cleaned)
    out: list[str] = []
    for t in toks:
        if t.strip() in ("", "-"):
            continue
        norm = _normalize_number(t)
        # 过滤掉极小的整数（如 1、2 这种序号）
        try:
            if abs(float(norm)) < 3 and "%" not in t and "¥" not in t:
                continue
        except ValueError:
            pass
        out.append(norm)
    return list(dict.fromkeys(out))


def verify_numbers(answer: str, evidence_text: str) -> list[str]:
    """校验答案中的数字是否出现在工具数据里，返回未能核对的数字。"""
    if not evidence_text:
        return extract_significant_numbers(answer)
    evidence_nums = {_normalize_number(t) for t in
                     re.findall(r"-?\d[\d,]*(?:\.\d+)?", evidence_text)}
    # 允许四舍五入后的近似（例如 173.0 → 173）
    evidence_rounded = set()
    for n in evidence_nums:
        try:
            f = float(n)
            evidence_rounded.add(f"{f:.0f}")
            evidence_rounded.add(f"{f:.1f}")
            evidence_rounded.add(f"{f:.2f}")
        except ValueError:
            continue
    unverified: list[str] = []
    for tok in extract_significant_numbers(answer):
        if tok in evidence_nums or tok in evidence_rounded:
            continue
        try:
            f = float(tok)
            if any(abs(f - float(e)) / max(abs(f), 1e-9) < 0.02 for e in evidence_nums
                   if re.fullmatch(r"-?\d+(\.\d+)?", e)):
                continue
        except (ValueError, ZeroDivisionError):
            pass
        unverified.append(tok)
    return unverified[:12]


class ChatAssistant:
    """带工具与校验的问答助手。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.llm = LLMClient(cfg.llm, profile="chat")   # 交互类：不思考，延迟优先
        self.tools = ToolBox(cfg)
        self.registry = self.tools.registry()
        self.chat_dir = cfg.data_dir() / "chat"
        self.chat_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 配额 ----------------
    def questions_today(self) -> int:
        path = self.chat_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        if not path.exists():
            return 0
        try:
            return sum(1 for _ in path.open("r", encoding="utf-8"))
        except OSError:
            return 0

    def quota_left(self) -> int:
        limit = int(self.cfg.chat.get("max_questions_per_day", 50))
        return max(0, limit - self.questions_today())

    # ---------------- 主流程 ----------------
    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        question = str(question or "").strip()
        if not question:
            return {"error": "问题为空"}
        if not self.llm.enabled:
            return {"error": "未配置 DeepSeek API Key，问答功能不可用"}
        if self.quota_left() <= 0:
            return {"error": f"今日提问次数已达上限（{self.cfg.chat.get('max_questions_per_day')} 次），明天再来"}

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in (history or [])[-MAX_HISTORY:]:
            role = h.get("role")
            content = str(h.get("content") or "")[:1500]
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        tool_trace: list[dict] = []
        answer = ""
        error = ""
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            try:
                msg = self.llm.chat_with_tools(messages, TOOL_SCHEMAS, temperature=0.3)
            except Exception as exc:
                error = f"模型调用失败：{exc}"
                break
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                answer = (msg.get("content") or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                func = self.registry.get(name)
                if func is None:
                    result: Any = {"error": f"未知工具 {name}"}
                else:
                    try:
                        result = func(**args) if args else func()
                    except Exception as exc:
                        result = {"error": f"工具 {name} 执行失败：{exc}"}
                tool_trace.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                                 "name": name,
                                 "content": json.dumps(result, ensure_ascii=False)[:6000]})
        else:
            answer = "（工具调用轮次过多，我把问题理解得太复杂了，请拆成更具体的小问题再问一次）"

        if not answer and not error:
            answer = "（模型没有返回内容，请换个问法再试）"

        # 平台规则也属于「事实」，一并纳入数字核对范围（避免把 1%、提现 1%、7 天误判为编造）
        from ..rules import trading_rules_text
        rules_text = (trading_rules_text(self.cfg.sim)
                      + f"；模拟盘初始资金 {self.cfg.sim.get('initial_capital')}")
        evidence_text = json.dumps(tool_trace, ensure_ascii=False) + rules_text
        unverified = verify_numbers(answer, evidence_text) if answer else []

        result = {
            "question": question,
            "answer": answer,
            "tools_used": [t["tool"] for t in tool_trace],
            "evidence": tool_trace,
            "rules": rules_text,
            "unverified_numbers": unverified,
            "usage": self.llm.last_usage,
            "time": datetime.now().strftime("%H:%M:%S"),
            "error": error,
        }
        self._archive(result)
        log.info("问答完成：用了 %d 个工具，%s，tokens=%s",
                 len(tool_trace), f"未核对数字 {unverified}" if unverified else "数字全部核对通过",
                 self.llm.last_usage.get("total_tokens", "?"))
        return result

    def _archive(self, result: dict) -> None:
        try:
            path = self.chat_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**result, "ts": datetime.now().isoformat(timespec="seconds")},
                                    ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("问答存档失败：%s", exc)
