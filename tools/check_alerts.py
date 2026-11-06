"""命令行：执行一次价格异动巡检。

用法：
    python tools/check_alerts.py            # 巡检并推送（有异动才推）
    python tools/check_alerts.py --dry-run  # 只巡检不推送
    python tools/check_alerts.py --show     # 只看监控清单与当前价，不判定
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.alerts.checker import AlertChecker  # noqa: E402
from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.log_dir())
    args = sys.argv[1:]
    checker = AlertChecker(cfg)

    if "--show" in args:
        items = checker.collect_items()
        prices = checker.fetch_prices([i["name"] for i in items])
        print(f"\n监控清单（{len(items)} 个）：")
        for it in items:
            p = prices.get(it["name"])
            price = f"¥{p['price']:.2f}" if p else "取价失败"
            print(f"  [{it.get('source', '')}] {it['name']}　{price}")
        return 0

    run = checker.run(push="--dry-run" not in args)
    print(f"\n=== 巡检完成（{run.time}）===")
    if run.expected and run.checked < run.expected:
        print(f"已检查：{run.checked}/{run.expected} 个标的（{len(run.skipped)} 个取价失败）")
    else:
        print(f"已检查：{run.checked} 个标的")
    if run.skipped:
        print("取价失败清单：" + "、".join(run.skipped[:8])
              + (f" 等 {len(run.skipped)} 个" if len(run.skipped) > 8 else ""))
    if run.error:
        print(f"错误：{run.error}")
    mode_txt = {"alert": "紧急异动（已推送）", "summary": "盘中概览（已推送）"}.get(run.mode, "未推送")
    print(f"推送类型：{mode_txt}")
    if run.urgent:
        print(f"\n🔔 紧急 {len(run.urgent)} 条（立即推送）：")
        for a in run.urgent:
            print(f"  {a.to_line()}")
    if run.mild:
        print(f"\n📋 温和 {len(run.mild)} 条（静默记录，12/22 点概览汇总）：")
        for a in run.mild[:10]:
            print(f"  {a.to_line()}")
        if len(run.mild) > 10:
            print(f"  …另有 {len(run.mild) - 10} 条")
    if not run.alerts:
        print("\n无任何异动")
    if run.ai_comment:
        print(f"\n🤖 AI 点评：{run.ai_comment}")
    if "--dry-run" in args and run.message:
        print("\n" + "=" * 60)
        print("以下为「如果不加 --dry-run 会推送的内容」：")
        print("=" * 60)
        print(run.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
