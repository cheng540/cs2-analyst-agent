"""LLM 研判层与情报采集单元测试（全部离线，不调用真实接口）。"""
import unittest

from cs2analyst.engine.decision import Action, Decision
from cs2analyst.engine.decision import Decision as D
from cs2analyst.intel.news import IntelCollector, summarise_anomaly
from cs2analyst.llm.analyst import LLMJudgement, apply_overlay
from cs2analyst.llm.client import LLMClient, parse_json_block


class TestLLMClient(unittest.TestCase):
    def test_disabled_without_key(self):
        c = LLMClient({"enabled": True, "api_key": ""})
        self.assertFalse(c.enabled)
        with self.assertRaises(RuntimeError):
            c.chat([{"role": "user", "content": "hi"}])

    def test_disabled_by_flag(self):
        c = LLMClient({"enabled": False, "api_key": "sk-x"})
        self.assertFalse(c.enabled)

    def test_parse_plain_json(self):
        self.assertEqual(parse_json_block('{"a": 1}'), {"a": 1})

    def test_parse_fenced_json(self):
        text = '```json\n{"market_view": "测试", "items": []}\n```'
        self.assertEqual(parse_json_block(text)["market_view"], "测试")

    def test_parse_with_surrounding_text(self):
        text = '好的，以下是结果：{"a": {"b": 2}} 希望有帮助'
        self.assertEqual(parse_json_block(text)["a"]["b"], 2)

    def test_parse_invalid(self):
        with self.assertRaises(Exception):
            parse_json_block("完全不是 JSON")


def mk(action, score=0.0, conviction=3):
    return Decision(action=action, conviction=conviction, score=score,
                    target_weight_pct=12.0, reasons=["规则理由"], action_text="t")


def mk_judgement(items: dict) -> LLMJudgement:
    j = LLMJudgement(date="2026-09-23", model="deepseek-chat")
    j.market_view = "大盘走弱"
    j.items = items
    return j


class TestOverlay(unittest.TestCase):
    def test_ai_vetoes_rule_buy(self):
        decisions = {"X": mk(Action.BUY)}
        j = mk_judgement({"X": {"stance": "avoid", "confidence": 5, "comment": "有重大利空"}})
        out, notes = apply_overlay(decisions, j, {})
        self.assertEqual(out["X"].action, Action.HOLD)
        self.assertTrue(any("AI 否决" in r for r in out["X"].reasons))
        self.assertEqual(len(notes), 1)

    def test_ai_vetoes_on_low_confidence(self):
        decisions = {"X": mk(Action.ADD)}
        j = mk_judgement({"X": {"stance": "buy", "confidence": 1, "comment": "不确定"}})
        out, _ = apply_overlay(decisions, j, {})
        self.assertEqual(out["X"].action, Action.HOLD)

    def test_ai_proposes_probe_buy(self):
        decisions = {"X": mk(Action.HOLD)}
        j = mk_judgement({"X": {"stance": "buy", "confidence": 5, "comment": "低位机会"}})
        out, notes = apply_overlay(decisions, j, {})
        self.assertEqual(out["X"].action, Action.BUY)
        self.assertEqual(out["X"].target_weight_pct, 6.0)
        self.assertTrue(notes)

    def test_no_change_when_aligned(self):
        decisions = {"X": mk(Action.HOLD), "Y": mk(Action.BUY)}
        j = mk_judgement({"X": {"stance": "hold", "confidence": 3, "comment": ""},
                          "Y": {"stance": "buy", "confidence": 4, "comment": "支持"}})
        out, notes = apply_overlay(decisions, j, {})
        self.assertEqual(out["X"].action, Action.HOLD)
        self.assertEqual(out["Y"].action, Action.BUY)
        self.assertEqual(notes, [])

    def test_missing_item_in_judgement_keeps_rule(self):
        decisions = {"X": mk(Action.BUY)}
        out, _ = apply_overlay(decisions, mk_judgement({}), {})
        self.assertEqual(out["X"].action, Action.BUY)

    def test_stance_parsing(self):
        j = mk_judgement({"X": {"stance": "BUY", "confidence": "4"}})
        self.assertEqual(j.item_stance("X"), ("buy", 4))
        j2 = mk_judgement({"X": {"stance": "unknown", "confidence": 99}})
        self.assertEqual(j2.item_stance("X"), ("", 5))


class TestIntel(unittest.TestCase):
    def test_summarise_anomaly(self):
        row = {"beforeSellPrice": 100.0, "sellPrice": 112.0,
               "beforeSellCount": 200, "sellCount": 180,
               "beforePurchasePrice": 95.0, "purchasePrice": 98.0}
        txt = summarise_anomaly(row)
        self.assertIn("+12.0%", txt)
        self.assertIn("在售量 200→180", txt)
        self.assertIn("求购价", txt)

    def test_summarise_anomaly_tolerates_garbage(self):
        txt = summarise_anomaly({"beforeSellPrice": None, "sellPrice": "abc"})
        self.assertIn("在售价", txt)

    def test_collector_disabled(self):
        c = IntelCollector({"enabled": False})
        self.assertEqual(c.collect(), {})


if __name__ == "__main__":
    unittest.main()
