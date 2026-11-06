"""发布前脱敏自检的测试。

这个测试的作用不是"证明代码正确"，而是**防止以后有人手滑把密钥写回代码里**：
把 tools/check_secrets.py 当成仓库守卫，每次跑测试都会扫一遍。
"""
import unittest

from tools.check_secrets import INTERNAL_DOCS, SKIP_DIRS, scan


class TestNoSecretsInRepo(unittest.TestCase):
    def test_repo_is_clean(self):
        hits, _notes = scan()
        detail = "\n".join(f"{rel}:{line} [{name}] {snippet}"
                           for rel, line, name, snippet in hits)
        self.assertEqual(hits, [], f"仓库里发现疑似密钥/服务器信息：\n{detail}")

    def test_internal_docs_are_flagged(self):
        """内部文档要么被打上「发布前处理」的提醒，要么根本不在包里。"""
        from pathlib import Path

        _hits, notes = scan()
        flagged = {note.split("：")[0] for note in notes}
        for name in INTERNAL_DOCS:
            exists = Path(__file__).resolve().parent.parent.joinpath(name).exists()
            if exists:
                self.assertIn(name, flagged, f"{name} 还在仓库里，但没被标记为发布前处理")
            else:
                self.assertNotIn(name, flagged, f"{name} 已删除，不该再出现在提醒里")

    def test_skip_dirs_cover_personal_data(self):
        """个人数据目录必须在跳过清单里，否则扫描会读到真实持仓。"""
        for name in ("data", "logs", "reports", "求职材料"):
            self.assertIn(name, SKIP_DIRS)

    def test_detects_synthetic_leak(self):
        """自检脚本本身要能抓到东西，否则它就是个摆设。"""
        import re

        from tools.check_secrets import PATTERNS

        sample = 'api_key: "sk-' + "a1b2c3d4e5f6g7h8" + '"'
        matched = [name for name, pattern, _ in PATTERNS if pattern.search(sample)]
        self.assertTrue(matched, "样例密钥没有被任何规则命中")

        webhook = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef12-"  # check-secrets:ignore
                   "3456")
        matched = [name for name, pattern, _ in PATTERNS if pattern.search(webhook)]
        self.assertTrue(matched, "样例 webhook 没有被命中")
        del re

    def test_pragma_allows_fake_keys(self):
        """带 check-secrets:ignore 的行要放过（测试里用的假 Key）。"""
        from pathlib import Path
        text = Path(__file__).read_text(encoding="utf-8")
        self.assertIn("check-secrets:ignore", text)


if __name__ == "__main__":
    unittest.main()
