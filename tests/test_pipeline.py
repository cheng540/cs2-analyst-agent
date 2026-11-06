"""端到端流水线冒烟测试：离线演示数据全链路（含报告生成）。"""
import tempfile
import unittest
from pathlib import Path

from docx import Document

from cs2analyst.config import load_config
from cs2analyst.pipeline import run_pipeline
from demo.generate_demo_data import generate_demo_data


class TestPipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.demo_dir = self.tmp_path / "demo"
        self.inv = self.tmp_path / "inventory_demo.json"
        generate_demo_data(self.demo_dir, self.inv)
        self.cfg = load_config()
        # 重定向所有输出目录到临时目录，避免污染项目目录
        self.cfg.paths.update({
            "data_dir": str(self.tmp_path / "data"),
            "report_dir": str(self.tmp_path / "reports"),
            "log_dir": str(self.tmp_path / "logs"),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_pipeline(self):
        result = run_pipeline(self.cfg, offline_dir=self.demo_dir,
                              inventory_override=self.inv)
        self.assertEqual(result["mode"], "offline")
        # 报告文件生成且可打开
        report = Path(result["report_path"])
        self.assertTrue(report.exists())
        doc = Document(str(report))
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        joined = "\n".join(texts)
        for sec in ("一、大盘环境综述", "二、持仓整体评估", "三、持仓单品详细分析",
                    "四、操作建议汇总表", "五、风险提示"):
            self.assertIn(sec, joined)
        # 决策覆盖：清仓/减仓/持有/加仓 + 数据缺失条目
        actions = {d["action"] for d in result["decisions"]}
        for act in ("CLEAR", "REDUCE", "HOLD", "ADD"):
            self.assertIn(act, actions)
        self.assertIn("★ 蝴蝶刀 | 多普勒 (Factory New)", result["missing"])
        # 组合核算一致性
        p = result["portfolio"]
        self.assertGreater(p["total_value"], 0)
        self.assertAlmostEqual(p["total_pnl"], p["total_value"] - p["total_cost"], places=2)


if __name__ == "__main__":
    unittest.main()
