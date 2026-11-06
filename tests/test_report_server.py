"""报告服务的热重载守卫测试。

背景（2026-09-25 实际踩到）：网页问答是常驻进程，改完代码如果不重启服务，
它会一直用内存里的旧代码——平台费率从「2.5%」改成「卖出 1% + 提现 1%」后，
网页问答仍然回答 2.5%，因为进程是两天前启动的。

守卫做法：每次问答请求前比对被依赖文件的 mtime 指纹，变化则退出进程，
由 systemd（Restart=always）用新代码拉起。这里锁定两件事：
1. 监听的路径必须真实存在（路径写错 → 指纹永远是 "-" → 守卫永不触发，等于没写）；
2. 没有改动时指纹必须稳定（否则会无限重启）。
"""
import unittest
from pathlib import Path

from tools.report_server import _WATCHED, _fingerprint


class TestHotReloadGuard(unittest.TestCase):
    PROJECT = Path(__file__).resolve().parent.parent

    # config/config.yaml 是**运行时文件**（用户自己的密钥，已在 .gitignore 内），
    # 全新克隆时不存在是正常的，不能算路径写错。
    RUNTIME_FILES = {"config/config.yaml"}

    def test_watched_paths_exist(self):
        missing = [rel for rel in _WATCHED
                   if rel not in self.RUNTIME_FILES and not (self.PROJECT / rel).exists()]
        self.assertEqual(missing, [], f"热重载守卫监听了不存在的路径：{missing}")

    def test_runtime_files_are_watched(self):
        """运行时文件仍要在监听列表里（存在时改了要能触发重载）。"""
        self.assertIn("config/config.yaml", _WATCHED)

    def test_watched_covers_key_modules(self):
        """至少要覆盖「费率与提示词来源」的文件，否则改了它们不会触发重载。"""
        joined = " ".join(_WATCHED)
        for key in ("rules.py", "chat/assistant.py", "llm/client.py", "config/config.yaml"):
            self.assertIn(key, joined)

    def test_fingerprint_is_stable(self):
        self.assertEqual(_fingerprint(), _fingerprint())

    def test_fingerprint_includes_timestamps(self):
        """在真实项目目录下，指纹应当包含数字时间戳（而不是全部 '-'）。"""
        fp = _fingerprint()
        self.assertIn(":", fp)
        self.assertTrue(any(ch.isdigit() for ch in fp))


if __name__ == "__main__":
    unittest.main()
