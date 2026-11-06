"""识图导入：模型调用被替换成假实现，不花钱、不联网。

这段代码的失败方式比成功方式更值得测：
    · 没有 Key 时要给出可操作提示，而不是抛一堆 requests 异常；
    · 模型输出带代码块/空行/NONE 时不能崩；
    · 模型把列写错时，解析器的警告必须冒出来（这是最后一道防线）。
"""
import unittest
from unittest import mock

from cs2analyst.config import AppConfig
from cs2analyst.vision_ingest import (
    VisionError, _rows_from_reply, build_messages, images_to_items, _to_data_url,
)

PNG_1PX = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def cfg_with_key(key: str = "sk-test-key-1234") -> AppConfig:
    return AppConfig(llm={"api_key": key, "enabled": True, "model": "deepseek-flash"})


class TestDataUrl(unittest.TestCase):
    def test_accepts_data_url(self):
        self.assertEqual(_to_data_url(PNG_1PX), PNG_1PX)

    def test_rejects_other_type(self):
        with self.assertRaises(VisionError):
            _to_data_url("data:text/plain;base64,aGVsbG8=")

    def test_rejects_empty(self):
        with self.assertRaises(VisionError):
            _to_data_url("")

    def test_rejects_broken_base64(self):
        with self.assertRaises(VisionError):
            _to_data_url("这不是base64")

    def test_wraps_plain_base64(self):
        raw = PNG_1PX.split(",", 1)[1]
        self.assertTrue(_to_data_url(raw).startswith("data:image/jpeg;base64,"))

    def test_rejects_oversized(self):
        huge = "data:image/png;base64," + "A" * (9 * 1024 * 1024)
        with self.assertRaises(VisionError):
            _to_data_url(huge)


class TestBuildMessages(unittest.TestCase):
    def test_structure(self):
        messages = build_messages([PNG_1PX])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        blocks = messages[0]["content"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png"))

    def test_too_many_images(self):
        with self.assertRaises(VisionError):
            build_messages([PNG_1PX] * 4)

    def test_no_images(self):
        with self.assertRaises(VisionError):
            build_messages([])


class TestRowsFromReply(unittest.TestCase):
    def test_tab_rows(self):
        reply = "AK-47 | Redline (Field-Tested)\t1\t169\nGlove Case\t2\t92"
        rows = _rows_from_reply(reply)
        self.assertEqual(rows[0], "AK-47 | Redline (Field-Tested) 1 169")
        self.assertEqual(len(rows), 2)

    def test_strips_code_fence(self):
        reply = "```\nAK-47 | Redline (Field-Tested)\t1\t169\n```"
        self.assertEqual(len(_rows_from_reply(reply)), 1)

    def test_none(self):
        self.assertEqual(_rows_from_reply("NONE"), [])
        self.assertEqual(_rows_from_reply(""), [])

    def test_unknown_columns_dropped(self):
        rows = _rows_from_reply("AWP | Asiimov (Field-Tested)\t?\t128.5")
        self.assertEqual(rows[0], "AWP | Asiimov (Field-Tested) 128.5")

    def test_plain_text_fallback(self):
        rows = _rows_from_reply("AK-47 | Redline (Field-Tested) 1 169")
        self.assertEqual(len(rows), 1)


class TestImagesToItems(unittest.TestCase):
    def test_happy_path_uses_shared_validator(self):
        reply = ("AK-47 | Redline (Field-Tested)\t1\t169\n"
                 "手套武器箱\t2\t92\n")
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=(reply, {"total_tokens": 900})):
            result = images_to_items(cfg_with_key(), [PNG_1PX], {})
        self.assertEqual(result["source"], "vision")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["confidence"], "high")
        self.assertEqual(result["usage"]["total_tokens"], 900)
        self.assertIn("核对", result["note"])

    def test_model_garbage_raises(self):
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=("NONE", {})):
            with self.assertRaises(VisionError):
                images_to_items(cfg_with_key(), [PNG_1PX], {})

    def test_missing_wear_gets_warning(self):
        """模型漏写磨损档时，解析器必须报警——这是最后一道防线。"""
        reply = "AK-47 | Redline\t1\t169"
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=(reply, {})):
            result = images_to_items(cfg_with_key(), [PNG_1PX], {})
        self.assertTrue(any("磨损档" in w for w in result["items"][0]["warnings"]))

    def test_no_key_gives_actionable_error(self):
        cfg = AppConfig(llm={"api_key": ""})
        with self.assertRaises(VisionError) as ctx:
            images_to_items(cfg, [PNG_1PX], {})
        self.assertIn("DeepSeek", str(ctx.exception))

    def test_lookup_used_for_chinese_names(self):
        reply = "AK-47 | 红线\t1\t169"
        lookup = {"AK-47 | 红线": "AK-47 | Redline (Field-Tested)"}
        with mock.patch("cs2analyst.vision_ingest.call_model", return_value=(reply, {})):
            result = images_to_items(cfg_with_key(), [PNG_1PX], lookup)
        self.assertEqual(result["items"][0]["market_hash_name"], "AK-47 | Redline (Field-Tested)")
        self.assertEqual(result["items"][0]["matched_by"], "中文名反查")


if __name__ == "__main__":
    unittest.main()
