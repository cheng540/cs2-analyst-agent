"""`python main.py demo` 必须与真实数据隔离。

2026-10-29 实际踩到：demo 走的是同一条 pipeline，会把合成指数（~1000）
追加进真实的 `data/market/broad_kline_2.json`，在真实图表上留下一个假尖刺，
同时覆盖 `data/market/broad_index.json` 与 `data/holdings_history/`。

这个测试把 run_pipeline 换成立即返回的假函数，只检查它拿到的配置：
数据目录、报告目录必须指向 data/demo_run，不能再是 data/ 与 reports/。
"""
import unittest
from pathlib import Path

import cs2analyst.pipeline as pipeline_mod
import demo.generate_demo_data as demo_data_mod
import main
from cs2analyst.config import AppConfig


class TestDemoIsolation(unittest.TestCase):
    def setUp(self):
        self._orig_pipeline = pipeline_mod.run_pipeline
        self._orig_generate = demo_data_mod.generate_demo_data
        self.captured = {}

        def fake_generate(out_dir, inventory_path):
            self.captured["fixture_dir"] = Path(out_dir)
            self.captured["inventory"] = Path(inventory_path)
            Path(inventory_path).parent.mkdir(parents=True, exist_ok=True)
            Path(inventory_path).write_text("[]", encoding="utf-8")

        def fake_pipeline(cfg, offline_dir=None, inventory_override=None):
            self.captured["cfg"] = cfg
            self.captured["offline_dir"] = offline_dir
            self.captured["inventory_override"] = inventory_override
            return {
                "market": {"trend": "上升趋势", "risk": "中风险"},
                "portfolio": {"total_value": 1.0, "total_pnl": 0.0, "pnl_pct": 0.0},
                "decisions": [],
                "missing": [],
                "report_path": "data/demo_run/reports/x.docx",
            }

        pipeline_mod.run_pipeline = fake_pipeline
        demo_data_mod.generate_demo_data = fake_generate

        self.cfg = AppConfig(
            paths={"data_dir": "data", "report_dir": "reports",
                   "log_dir": "logs", "inventory_file": "config/inventory.json"},
        )

    def tearDown(self):
        pipeline_mod.run_pipeline = self._orig_pipeline
        demo_data_mod.generate_demo_data = self._orig_generate

    def test_demo_writes_into_sandbox(self):
        code = main.cmd_demo(self.cfg)
        self.assertEqual(code, 0)

        cfg = self.captured["cfg"]
        self.assertEqual(cfg.paths["data_dir"], "data/demo_run")
        self.assertEqual(cfg.paths["report_dir"], "data/demo_run/reports")
        # 真实数据目录不能被当成写入目标
        self.assertNotEqual(cfg.paths["data_dir"], "data")
        self.assertNotEqual(cfg.paths["report_dir"], "reports")

    def test_demo_inventory_stays_out_of_config(self):
        main.cmd_demo(self.cfg)
        inventory = self.captured["inventory"]
        self.assertEqual(inventory.name, "inventory_demo.json")
        self.assertIn("demo_run", inventory.as_posix())
        self.assertNotIn("/config/", inventory.as_posix())

    def test_original_cfg_not_mutated(self):
        main.cmd_demo(self.cfg)
        self.assertEqual(self.cfg.paths["data_dir"], "data", "演示不应改掉调用方的配置")


if __name__ == "__main__":
    unittest.main()
