"""文件锁 / 原子写 / 保留注释的 YAML 改写 —— 并发安全测试。

这组测试对应交接文档里的 P3 硬约束：
    一边 17:30 跑 pipeline 一边改持仓，数据不丢不坏。
所以这里不是"跑一次没报错就算过"，而是真的开多线程并发写，
断言最终计数一条不少。
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from cs2analyst.web.store import (
    LockTimeout,
    atomic_write_text,
    backup_file,
    file_lock,
    mask_secret,
    patch_yaml_scalars,
    read_json,
    read_yaml,
    update_json,
)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestAtomicWrite(StoreTestBase):
    def test_write_and_read_back(self):
        p = self.dir / "a.txt"
        atomic_write_text(p, "你好\nworld")
        self.assertEqual(p.read_text(encoding="utf-8"), "你好\nworld")

    def test_no_temp_files_left(self):
        p = self.dir / "a.txt"
        atomic_write_text(p, "x")
        leftovers = [f.name for f in self.dir.iterdir() if f.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_overwrite_keeps_file_valid_for_readers(self):
        p = self.dir / "a.txt"
        atomic_write_text(p, "1" * 1000)
        atomic_write_text(p, "2")
        self.assertEqual(p.read_text(encoding="utf-8"), "2")


class TestFileLock(StoreTestBase):
    def test_lock_is_exclusive_across_threads(self):
        target = self.dir / "data.json"
        order = []
        started = threading.Event()

        def holder():
            with file_lock(target):
                order.append("holder-in")
                started.set()
                threading.Event().wait(0.3)
                order.append("holder-out")

        t = threading.Thread(target=holder)
        t.start()
        started.wait(1.0)
        with file_lock(target, timeout=5):
            order.append("waiter-in")
        t.join(5)
        self.assertEqual(order, ["holder-in", "holder-out", "waiter-in"])

    def test_timeout_raises(self):
        target = self.dir / "data.json"
        started = threading.Event()
        release = threading.Event()

        def holder():
            with file_lock(target):
                started.set()
                release.wait(2)

        t = threading.Thread(target=holder)
        t.start()
        started.wait(1.0)
        try:
            with self.assertRaises(LockTimeout):
                with file_lock(target, timeout=0.15):
                    pass
        finally:
            release.set()
            t.join(3)

    def test_lock_file_created_next_to_target(self):
        target = self.dir / "sub" / "data.json"
        with file_lock(target):
            pass
        self.assertTrue((target.parent / (target.name + ".lock")).exists())


class TestConcurrentUpdate(StoreTestBase):
    """8 个线程各自增 50 次，最终必须是 400 —— 一次覆盖写就会少。"""

    def test_concurrent_increment_lost_update_free(self):
        p = self.dir / "counter.json"
        p.write_text(json.dumps({"count": 0}), encoding="utf-8")
        threads, per_thread = 8, 50

        def worker():
            for _ in range(per_thread):
                update_json(p, lambda d: {"count": d["count"] + 1}, default={"count": 0})

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(read_json(p)["count"], threads * per_thread)

    def test_update_json_rereads_inside_lock(self):
        """内存里拿到的旧副本不能覆盖磁盘上的新内容。"""
        p = self.dir / "list.json"
        p.write_text(json.dumps({"items": ["a"]}), encoding="utf-8")
        seen = {}

        def mutator(data):
            seen["fresh"] = list(data["items"])
            data["items"].append("b")

        update_json(p, mutator)
        self.assertEqual(seen["fresh"], ["a"])
        self.assertEqual(read_json(p)["items"], ["a", "b"])

    def test_corrupt_json_does_not_crash(self):
        p = self.dir / "bad.json"
        p.write_text("{不是 json", encoding="utf-8")
        self.assertEqual(read_json(p, default={"ok": 1}), {"ok": 1})


class TestYamlPatch(StoreTestBase):
    SAMPLE = (
        "# 顶部注释\n"
        "steamdt:\n"
        "  api_key: ''            # SteamDT 开放平台 Key\n"
        "  timeout: 20\n"
        "llm:\n"
        "  api_key: 'old'\n"
        "  enabled: true\n"
    )

    def _file(self, text=None):
        p = self.dir / "config.yaml"
        p.write_text(text if text is not None else self.SAMPLE, encoding="utf-8")
        return p

    def test_replaces_value_and_keeps_comment(self):
        p = self._file()
        hit = patch_yaml_scalars(p, {"steamdt.api_key": "sk-new-123"})
        text = p.read_text(encoding="utf-8")
        self.assertTrue(hit["steamdt.api_key"])
        self.assertIn("api_key: 'sk-new-123'            # SteamDT 开放平台 Key", text)
        self.assertIn("# 顶部注释", text)
        self.assertEqual(read_yaml(p)["steamdt"]["api_key"], "sk-new-123")

    def test_only_touches_target_section(self):
        p = self._file()
        patch_yaml_scalars(p, {"steamdt.api_key": "A"})
        data = read_yaml(p)
        self.assertEqual(data["llm"]["api_key"], "old")
        self.assertTrue(data["llm"]["enabled"])

    def test_appends_missing_key_into_section(self):
        p = self._file()
        hit = patch_yaml_scalars(p, {"steamdt.retry": 5})
        self.assertFalse(hit["steamdt.retry"])
        data = read_yaml(p)
        self.assertEqual(data["steamdt"]["retry"], 5)
        self.assertEqual(data["steamdt"]["timeout"], 20)
        self.assertEqual(data["llm"]["api_key"], "old")

    def test_creates_missing_section(self):
        p = self._file()
        patch_yaml_scalars(p, {"notify.wecom_webhook": "https://example/x"})
        data = read_yaml(p)
        self.assertEqual(data["notify"]["wecom_webhook"], "https://example/x")

    def test_types_roundtrip(self):
        p = self._file()
        patch_yaml_scalars(p, {
            "llm.enabled": False,
            "alerts.times": [0, 9, 12],
            "steamdt.timeout": 45,
            "steamdt.special_style": "",
        })
        data = read_yaml(p)
        self.assertIs(data["llm"]["enabled"], False)
        self.assertEqual(data["alerts"]["times"], [0, 9, 12])
        self.assertEqual(data["steamdt"]["timeout"], 45)
        self.assertEqual(data["steamdt"]["special_style"], "")

    def test_quotes_survive_roundtrip(self):
        """带单引号的值转义后仍能被 YAML 正确读回。"""
        p = self._file()
        patch_yaml_scalars(p, {"steamdt.special_style": "it's ok"})
        self.assertEqual(read_yaml(p)["steamdt"]["special_style"], "it's ok")

    def test_comments_not_duplicated_on_repeat_writes(self):
        p = self._file()
        for i in range(3):
            patch_yaml_scalars(p, {"steamdt.api_key": f"k{i}"})
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count("# SteamDT 开放平台 Key"), 1)
        self.assertEqual(read_yaml(p)["steamdt"]["api_key"], "k2")

    def test_block_sequence_same_indent_replaced_cleanly(self):
        """回归：`times:` 下面 `- 9` 与键同缩进也是合法 YAML，替换时要一起清掉。

        早期版本只按"缩进更深"判断续行，结果留下了孤立的 `- 9`，
        整个 config.yaml 直接解析失败（测试里实际踩到过）。
        """
        p = self._file(
            "alerts:\n"
            "  enabled: true\n"
            "  times:\n"
            "  - 9\n"
            "  - 12\n"
            "decision:\n"
            "  stop_loss_pct: 0.3\n"
        )
        patch_yaml_scalars(p, {"alerts.times": [9, 12, 22]})
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("- 9\n", text)
        data = read_yaml(p)
        self.assertEqual(data["alerts"]["times"], [9, 12, 22])
        self.assertEqual(data["decision"]["stop_loss_pct"], 0.3)
        self.assertIs(data["alerts"]["enabled"], True)

    def test_deeper_block_sequence_replaced(self):
        p = self._file(
            "alerts:\n"
            "  times:\n"
            "    - 9\n"
            "    - 12\n"
            "  enabled: true\n"
        )
        patch_yaml_scalars(p, {"alerts.times": [1, 2]})
        data = read_yaml(p)
        self.assertEqual(data["alerts"]["times"], [1, 2])
        self.assertIs(data["alerts"]["enabled"], True)

    def test_nested_mapping_value_kept_intact(self):
        """只改标量，不要碰同节里的嵌套映射（profiles 这类）。"""
        p = self._file(
            "llm:\n"
            "  api_key: 'old'\n"
            "  profiles:\n"
            "    chat:\n"
            "      model: deepseek-flash\n"
        )
        patch_yaml_scalars(p, {"llm.api_key": "new"})
        data = read_yaml(p)
        self.assertEqual(data["llm"]["api_key"], "new")
        self.assertEqual(data["llm"]["profiles"]["chat"]["model"], "deepseek-flash")


class TestMaskAndBackup(StoreTestBase):
    def test_mask_secret(self):
        self.assertEqual(mask_secret("sk-abcdefghijkl1234"), "sk-****1234")  # check-secrets:ignore 假 Key
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("ab"), "**")

    def test_backup_keeps_recent_copies(self):
        p = self.dir / "config.yaml"
        p.write_text("a: 1", encoding="utf-8")
        for _ in range(6):
            backup_file(p, keep=3)
        baks = sorted(self.dir.glob("config.yaml.bak.*"))
        self.assertLessEqual(len(baks), 4)


if __name__ == "__main__":
    unittest.main()
