"""上下文恢复简报单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.brief import build_brief, save_brief
from cs2analyst.config import load_config


class TestBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = load_config()
        # 重定向到临时目录
        self.cfg.paths.update({
            "data_dir": str(self.root / "data"),
            "report_dir": str(self.root / "reports"),
            "log_dir": str(self.root / "logs"),
            "inventory_file": str(self.root / "inventory.json"),
        })
        self.cfg.sim.update({
            "state_file": str(self.root / "data" / "sim" / "state_a.json"),
            "state_file_b": str(self.root / "data" / "sim" / "state_b.json"),
        })
        for d in (self.cfg.data_dir(), self.cfg.report_dir()):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_brief_minimal(self):
        text = build_brief(self.cfg, "2026-09-23")
        self.assertIn("上下文恢复简报", text)
        self.assertIn("模拟盘（双轨，T+7 规则）", text)
        self.assertIn("尚无状态文件", text)

    def test_brief_with_inventory_and_state(self):
        self.cfg.inventory_path().write_text(json.dumps([
            {"market_hash_name": "AWP | Black Nile (Factory New)", "quantity": 2,
             "buy_price": 100.0},
            {"market_hash_name": "Sticker | X", "quantity": 1, "buy_price": 50.0,
             "manual_price": 30.0},
        ], ensure_ascii=False), encoding="utf-8")
        state = {
            "created": "2026-09-23", "label": "A", "initial_capital": 3000.0,
            "cash": 2500.0,
            "positions": {"AWP | Black Nile (Factory New)": {
                "qty": 4, "avg_cost": 125.0, "locked_until": "2026-09-30"}},
            "journal": [{"date": "2026-09-23", "net_value": 3000.0, "return_pct": 0.0,
                         "trades": 1, "thoughts": "小仓试建"}],
        }
        self.cfg.sim_state_path().parent.mkdir(parents=True, exist_ok=True)
        self.cfg.sim_state_path().write_text(json.dumps(state, ensure_ascii=False),
                                             encoding="utf-8")
        text = build_brief(self.cfg, "2026-09-23")
        self.assertIn("条目 2 个 / 共 3 件", text)
        self.assertIn("¥250.00", text)             # 总成本 100*2+50*1
        self.assertIn("手动估值条目", text)
        self.assertIn("A 轨", text)
        self.assertIn("锁定期至 2026-09-30", text)
        self.assertIn("小仓试建", text)

    def test_save_brief(self):
        path = save_brief(self.cfg, "内容", "2026-09-23")
        self.assertTrue(path.exists())
        self.assertIn("内容", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
