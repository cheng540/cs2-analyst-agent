"""LLM 客户端单元测试（离线，通过替换 session 模拟接口返回）。

覆盖 2026-09-24 引入的三件事：
1. 分场景配置（profile）：判断类开思考、交互类不思考；
2. 思考模式下不传 temperature（官方会忽略，传了是假控制权）；
3. **空正文/截断防护**：思考 token 吃光 max_tokens 时会返回空正文，
   客户端必须自动加倍预算重试，仍为空则抛错（而不是静默返回空字符串）。
"""
import unittest

from cs2analyst.llm.client import LLMClient

BASE = {
    "enabled": True,
    "api_key": "sk-test",
    "base_url": "https://api.deepseek.com",
    "model": "base-model",
    "max_tokens": 100,
    "temperature": 0.7,
    "profiles": {
        "judgement": {"model": "deepseek-flash", "thinking": True, "max_tokens": 6000},
        "chat": {"model": "deepseek-flash", "thinking": False, "max_tokens": 4000},
        "review": {"model": "deepseek-v4-pro", "thinking": True},
    },
}


def resp(content, finish="stop", reasoning=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(json)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

        class R:
            status_code = 200

            @staticmethod
            def json():
                return item

        return R()


def mk(responses, profile=None, cfg=None):
    c = LLMClient({**BASE, **(cfg or {})}, profile=profile)
    c._session = FakeSession(responses)
    return c, c._session


class TestLLMProfiles(unittest.TestCase):
    def test_profile_overrides_base(self):
        c = LLMClient(BASE, profile="judgement")
        self.assertEqual(c.model, "deepseek-flash")
        self.assertTrue(c.thinking)
        self.assertEqual(c.max_tokens, 6000)
        # 未命中 profile 的字段继承基础配置
        self.assertEqual(c.temperature, 0.7)
        self.assertEqual(c.profile, "judgement")

    def test_review_profile_uses_pro_model(self):
        c = LLMClient(BASE, profile="review")
        self.assertEqual(c.model, "deepseek-v4-pro")
        self.assertTrue(c.thinking)
        self.assertEqual(c.max_tokens, 100)      # profile 未写 → 继承基础值

    def test_unknown_profile_falls_back_to_base(self):
        c = LLMClient(BASE, profile="not-exist")
        self.assertEqual(c.model, "base-model")
        self.assertIsNone(c.thinking)

    def test_base_config_used_without_profile(self):
        c = LLMClient(BASE)
        self.assertEqual(c.model, "base-model")
        self.assertIsNone(c.thinking)


class TestPayload(unittest.TestCase):
    def test_thinking_enabled_and_temperature_dropped(self):
        c, s = mk([resp("hi")], profile="judgement")
        c.chat([{"role": "user", "content": "x"}])
        payload = s.calls[0]
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", payload)      # 思考模式下温度无效，不假装能控制
        self.assertEqual(payload["max_tokens"], 6000)

    def test_thinking_disabled_keeps_temperature(self):
        c, s = mk([resp("hi")], profile="chat")
        c.chat([{"role": "user", "content": "x"}])
        payload = s.calls[0]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["temperature"], 0.7)

    def test_no_thinking_key_when_unset(self):
        c, s = mk([resp("hi")])
        c.chat([{"role": "user", "content": "x"}])
        self.assertNotIn("thinking", s.calls[0])      # 不传 → 用服务端默认

    def test_json_mode_sets_response_format(self):
        c, s = mk([resp('{"a": 1}')])
        self.assertEqual(c.chat_json([{"role": "user", "content": "x"}]), {"a": 1})
        self.assertEqual(s.calls[0]["response_format"], {"type": "json_object"})


class TestEmptyContentGuard(unittest.TestCase):
    """思考 token 吃光预算 → finish_reason=length 且正文为空，必须自动补救。"""

    def test_empty_content_retries_with_bigger_budget(self):
        c, s = mk([resp("", finish="length"), resp("补救成功")], profile="judgement")
        self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "补救成功")
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(s.calls[0]["max_tokens"], 6000)
        self.assertEqual(s.calls[1]["max_tokens"], 12000)     # 加倍

    def test_empty_content_twice_raises_clear_error(self):
        c, _ = mk([resp("", finish="length")], profile="judgement")
        with self.assertRaises(RuntimeError) as ctx:
            c.chat([{"role": "user", "content": "x"}])
        self.assertIn("空正文", str(ctx.exception))

    def test_truncated_answer_retries(self):
        c, s = mk([resp("被截断的半句", finish="length"), resp("完整答案")], profile="judgement")
        self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "完整答案")
        self.assertEqual(len(s.calls), 2)

    def test_truncated_answer_returned_when_no_retry_left(self):
        c, _ = mk([resp("被截断的半句", finish="length")], profile="judgement")
        self.assertEqual(c.chat([{"role": "user", "content": "x"}], retry=1), "被截断的半句")

    def test_budget_growth_capped(self):
        c, s = mk([resp("", finish="length"), resp("", finish="length"), resp("ok")],
                  profile="judgement", cfg={"max_output_cap": 8000})
        self.assertEqual(c.chat([{"role": "user", "content": "x"}], retry=3), "ok")
        self.assertEqual(s.calls[1]["max_tokens"], 8000)      # 被 cap 截住
        self.assertEqual(s.calls[2]["max_tokens"], 8000)      # 预算封顶后同额重试

    def test_tool_call_round_with_empty_content_is_ok(self):
        """工具调用轮次正文为空是正常的，不能被空正文防护误判为故障。"""
        payload = resp("", finish="tool_calls")
        payload["choices"][0]["message"]["tool_calls"] = [
            {"id": "1", "type": "function", "function": {"name": "get", "arguments": "{}"}}]
        c, s = mk([payload], profile="chat")
        msg = c.chat_with_tools([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        self.assertTrue(msg["tool_calls"])
        self.assertEqual(len(s.calls), 1)                     # 没有多余重试

    def test_normal_answer_no_retry(self):
        c, s = mk([resp("正常")], profile="judgement")
        self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "正常")
        self.assertEqual(len(s.calls), 1)

    def test_usage_and_finish_reason_recorded(self):
        c, _ = mk([resp("正常", usage={"prompt_tokens": 11, "completion_tokens": 22})])
        c.chat([{"role": "user", "content": "x"}])
        self.assertEqual(c.last_usage["completion_tokens"], 22)
        self.assertEqual(c.last_finish_reason, "stop")


class TestToolCalls(unittest.TestCase):
    def test_message_passed_through_with_reasoning(self):
        """带 tools 的请求必须能拿到完整 message（含 reasoning_content），
        否则下一轮回传会缺字段、被接口以 400 拒绝。"""
        payload = resp("", reasoning="我先想想")
        payload["choices"][0]["message"]["tool_calls"] = [
            {"id": "1", "type": "function", "function": {"name": "get", "arguments": "{}"}}]
        payload["choices"][0]["finish_reason"] = "tool_calls"
        c, s = mk([payload], profile="chat")
        msg = c.chat_with_tools([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        self.assertEqual(msg["reasoning_content"], "我先想想")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get")
        self.assertEqual(s.calls[0]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
