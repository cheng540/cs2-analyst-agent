"""LLM 客户端（OpenAI 兼容接口，默认 DeepSeek）。

特性：
- 未配置 api_key 或 enabled=false 时自动禁用（不影响主流程）；
- 网络/接口异常抛出异常，由调用方决定是否降级；
- 支持 JSON 模式（DeepSeek 支持 response_format={"type":"json_object"}）；
- **分场景配置（profile）**：不同用途用不同模型与思考模式，见下；
- **空正文/截断防护**：思考模式下思考 token 会占用 max_tokens，预算不够时
  接口会返回 finish_reason=length 且 content 为空——这里会自动加倍预算重试一次，
  仍为空则抛错（而不是静默返回空字符串，让调用方以为"模型没意见"）。

分场景配置（2026-09-24 实测后确定，配置写在 config.yaml 的 llm.profiles）：

    judgement  每日研判 + B 轨决策   flash + 思考   质量提升最大，后台任务不在乎 7 秒
    chat       网页问答              flash 不思考   交互场景，多轮工具调用对延迟敏感
    alert      盘中告警点评          flash 不思考   150 字短评，延迟优先
    review     周报/月报复盘         v4-pro + 思考  低频重活，用最强的

为什么把 `deepseek-chat` 换掉：它是**已退役的别名**，官方文档明确说该模型已 retired，
请求由 DeepSeek-V4.1-Flash 承接、按 Flash 计价，且沿用旧行为（**不开思考**）。
能力上等于"最弱的那一档且不用推理"，还随时可能被官方改映射。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_MODEL = "deepseek-flash"
FALLBACK_PROFILES: dict[str, dict] = {
    "judgement": {"model": "deepseek-flash", "thinking": True, "max_tokens": 6000},
    "chat": {"model": "deepseek-flash", "thinking": False, "max_tokens": 4000},
    "alert": {"model": "deepseek-flash", "thinking": False, "max_tokens": 1200},
    "review": {"model": "deepseek-v4-pro", "thinking": True, "max_tokens": 8000},
}


class LLMClient:
    def __init__(self, cfg: dict, profile: str | None = None):
        base = dict(cfg or {})
        prof = dict((base.get("profiles") or {}).get(profile) or {}) if profile else {}
        merged = {**base, **prof}
        self.profile = profile or ""
        self.cfg = merged
        self.enabled = bool(merged.get("enabled", True)) and bool(
            str(merged.get("api_key", "")).strip())
        self.base_url = str(merged.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = str(merged.get("model", DEFAULT_MODEL))
        self.timeout = float(merged.get("timeout", 180))
        self.max_tokens = int(merged.get("max_tokens", 4000))
        self.max_output_cap = int(merged.get("max_output_cap", 32000))
        # thinking: True=显式开启 / False=显式关闭 / None=不传（用服务端默认，当前默认是开启）
        self.thinking = merged.get("thinking")
        self.temperature = float(merged.get("temperature", 0.4))
        self.api_key = str(merged.get("api_key", "")).strip()
        self.last_usage: dict = {}
        self.last_finish_reason = ""
        self._session = requests.Session()
        if self.thinking:
            # 官方文档：思考模式不支持 temperature/presence_penalty/frequency_penalty，
            # 传了不报错但也不生效——所以开启思考时我们干脆不传温度，避免"假装有控制权"
            log.debug("[%s] %s 开启思考模式：temperature 将被忽略", self.profile or "default", self.model)

    # ---------------- 请求构造 ----------------
    def _payload(self, messages: list[dict], json_mode: bool,
                 temperature: float | None, max_tokens: int | None,
                 tools: list[dict] | None = None) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if self.thinking is not None:
            payload["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        if not self.thinking:      # 非思考模式下温度才真正生效
            payload["temperature"] = self.temperature if temperature is None else temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _request(self, payload: dict, retry: int, what: str) -> tuple[dict, str]:
        """POST 一次（含重试、空正文与截断防护），返回 (完整响应, 正文)。"""
        url = f"{self.base_url}/chat/completions"
        attempts = max(1, int(retry))
        budget = int(payload.get("max_tokens") or self.max_tokens)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.post(
                    url, json=payload, timeout=self.timeout,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"})
                if resp.status_code in (429, 500, 502, 503, 504):
                    log.warning("%s 接口返回 %d（第 %d 次），稍后重试",
                                what, resp.status_code, attempt)
                    time.sleep(3 * attempt)
                    continue
                data = resp.json()
                if resp.status_code != 200 or "choices" not in data:
                    raise RuntimeError(f"LLM 接口错误: HTTP {resp.status_code} "
                                       f"{json.dumps(data, ensure_ascii=False)[:300]}")
                self.last_usage = data.get("usage") or {}
                choice = (data.get("choices") or [{}])[0]
                self.last_finish_reason = str(choice.get("finish_reason") or "")
                message = choice.get("message") or {}
                content = (message.get("content") or "")
                # 工具调用轮次的正文本来就是空的（内容在 tool_calls 里）——这不是故障
                if message.get("tool_calls") or self.last_finish_reason == "tool_calls":
                    return data, content
                if content.strip() and self.last_finish_reason != "length":
                    return data, content
                # 空正文或截断：思考 token 很可能吃光了预算 → 加倍预算再来一次
                bigger = min(max(budget * 2, 2000), self.max_output_cap)
                if attempt < attempts:
                    if bigger > budget:
                        log.warning("%s：finish_reason=%s、正文 %d 字，max_tokens %d→%d 重试",
                                    what, self.last_finish_reason or "?", len(content),
                                    budget, bigger)
                        budget = bigger
                    else:
                        log.warning("%s：finish_reason=%s、正文 %d 字，预算已达上限 %d，同额重试",
                                    what, self.last_finish_reason or "?", len(content), budget)
                    payload = {**payload, "max_tokens": budget}
                    continue
                if not content.strip():
                    raise RuntimeError(
                        f"{what} 返回空正文（finish_reason={self.last_finish_reason or '?'}，"
                        f"max_tokens={budget}）——多半是思考 token 吃光了输出预算，"
                        "请调大该 profile 的 max_tokens")
                log.warning("%s：正文可能被截断（finish_reason=length）", what)
                return data, content
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("%s 请求异常（第 %d 次）: %s", what, attempt, exc)
                time.sleep(3 * attempt)
        raise RuntimeError(f"{what} 请求失败（已重试 {attempts} 次）: {last_exc}")

    # ---------------- 对外接口 ----------------
    def chat(self, messages: list[dict], json_mode: bool = False,
             temperature: float | None = None, max_tokens: int | None = None,
             retry: int = 2) -> str:
        """调用对话接口，返回文本内容。"""
        if not self.enabled:
            raise RuntimeError("LLM 未启用或未配置 api_key")
        payload = self._payload(messages, json_mode, temperature, max_tokens)
        _data, content = self._request(payload, retry, "对话")
        return content

    def chat_json(self, messages: list[dict], **kwargs) -> dict:
        """JSON 模式对话并解析为 dict（容忍 ```json 包裹）。"""
        text = self.chat(messages, json_mode=True, **kwargs)
        return parse_json_block(text)

    def chat_with_tools(self, messages: list[dict], tools: list[dict],
                        temperature: float | None = None,
                        max_tokens: int | None = None,
                        retry: int = 2) -> dict:
        """支持函数调用的对话，返回原始 message 对象（含 tool_calls 与 reasoning_content）。

        注意：带 tools 的请求在思考模式下**必须把 reasoning_content 原样回传**
        （官方要求，否则 400）。所以调用方要把本函数返回的整个 message 追加进
        messages，不要只挑 role/content/tool_calls 重组。
        """
        if not self.enabled:
            raise RuntimeError("LLM 未启用或未配置 api_key")
        payload = self._payload(messages, False, temperature, max_tokens, tools=tools)
        data, _content = self._request(payload, retry, "工具调用")
        return ((data.get("choices") or [{}])[0].get("message") or {})


def parse_json_block(text: str) -> dict:
    """从模型输出中稳健地提取 JSON 对象。"""
    if not text:
        raise ValueError("模型输出为空")
    cleaned = text.strip()
    # 去掉 markdown 代码块
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
        raise ValueError("模型输出 JSON 不是对象")
    except json.JSONDecodeError:
        # 退一步：截取第一个 { 到最后一个 }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(cleaned[start:end + 1])
            if isinstance(data, dict):
                return data
        raise
