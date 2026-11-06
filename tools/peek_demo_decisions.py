"""打印离线演示数据的每件决策（排查端到端测试为何少了某个动作）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config
from cs2analyst.pipeline import run_pipeline
from demo.generate_demo_data import generate_demo_data


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        demo_dir = tmp_path / "demo"
        inv = tmp_path / "inventory_demo.json"
        generate_demo_data(demo_dir, inv)
        cfg = load_config()
        cfg.paths.update({
            "data_dir": str(tmp_path / "data"),
            "report_dir": str(tmp_path / "reports"),
            "log_dir": str(tmp_path / "logs"),
        })
        result = run_pipeline(cfg, offline_dir=demo_dir, inventory_override=inv)
        print(f"mode={result['mode']} 大盘={result['market']['trend']}/{result['market']['risk']}"
              f" 市场近5日={result['market'].get('ret_5d')}")
        for d in result["decisions"]:
            print(f"  {d['action']:<7} score={d.get('score')}  {d.get('name_cn') or d['name']}")
            for r in (d.get("reasons") or [])[:4]:
                print(f"        · {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
