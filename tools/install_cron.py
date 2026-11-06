#!/usr/bin/env python3
"""按 config.yaml 生成定时任务（cron），让「改间隔/改时间/关报告」真正生效。

用法（服务器 / Linux）：
    python tools/install_cron.py                 # 只打印将要写入的 cron 行（默认不修改系统）
    python tools/install_cron.py --install       # 真正写入 crontab（会先删掉本项目的旧行）
    python tools/install_cron.py --uninstall     # 移除本项目写入的所有行

Windows（本地）：
    不写 cron，用「任务计划程序」或直接常驻 `python main.py schedule`。
    `--install` 在 Windows 上会拒绝执行并打印提示，避免半成品。

设计要点：
    · 所有任务都调用 tools/run_scheduled.py，开关由配置决定，删行不是必须的；
    · 旧的三行（daily_run.sh / run_alerts.sh / weekly_report.sh）会被识别并清理；
    · 写 crontab 前先备份到 logs/crontab.bak.<时间戳>，出问题可以 cp 回去。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.policy import (  # noqa: E402
    KIND_LABELS, alerts_interval_minutes, report,
)

ROOT = Path(__file__).resolve().parent.parent
MARKER = "tools/run_scheduled.py"
# 旧版脚本：装新 cron 时一并清掉，避免一天跑两遍
LEGACY_MARKERS = ("tools/daily_run.sh", "tools/run_alerts.sh", "tools/weekly_report.sh",
                  "tools/check_alerts.py")


def _python() -> str:
    """优先用当前解释器（服务器上是 venv/bin/python，本地是系统 Python）。"""
    return sys.executable or "python3"


def build_lines(cfg) -> list[str]:
    py = _python()
    root = ROOT.as_posix()
    lines: list[str] = []

    for kind in ("daily", "noon", "weekly"):
        conf = report(cfg, kind)
        if not conf["enabled"]:
            continue
        if kind == "weekly":
            # config 用 0=周一，cron 用 0=周日
            dow = (conf["weekday"] + 1) % 7
            when = f"{conf['minute']} {conf['hour']} * * {dow}"
        else:
            when = f"{conf['minute']} {conf['hour']} * * *"
        lines.append(f"{when} cd {root} && {py} {MARKER} --task {kind}")

    if (cfg.alerts or {}).get("enabled", True):
        interval = alerts_interval_minutes(cfg)
        # 只在白天到午夜之间巡检：夜里没有成交量，白跑还费接口额度
        hours = "9-23"
        when = f"*/{interval} {hours} * * *" if interval < 60 else f"0 {hours} * * *"
        if interval < 60:
            lines.append(f"{when} cd {root} && {py} {MARKER} --task alerts")
        else:
            # 间隔 >= 60 时用整点更整齐（60→每小时，120→每2小时）
            step = max(1, interval // 60)
            when = f"0 {hours}/{step} * * *" if step > 1 else f"0 {hours} * * *"
            lines.append(f"{when} cd {root} && {py} {MARKER} --task alerts")
    return lines


def _read_crontab() -> str:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_ours(line: str) -> bool:
    return MARKER in line or any(m in line for m in LEGACY_MARKERS)


def _install(lines: list[str]) -> int:
    current = _read_crontab()
    kept = [ln for ln in current.splitlines() if not _is_ours(ln)]
    backup = ROOT / "logs" / f"crontab.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(current, encoding="utf-8")

    body = "\n".join(kept + [""] + ["# ---- CS2 持仓助手（由 tools/install_cron.py 生成）----"]
                     + lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=body, text=True,
                          capture_output=True, timeout=20)
    if proc.returncode != 0:
        print(f"写入 crontab 失败：{proc.stderr.strip()}")
        print(f"原 crontab 已备份到 {backup}")
        return 1
    print(f"✓ 已写入 {len(lines)} 条定时任务（原 crontab 备份：{backup}）")
    return 0


def _uninstall() -> int:
    current = _read_crontab()
    kept = [ln for ln in current.splitlines() if not _is_ours(ln)]
    proc = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                          text=True, capture_output=True, timeout=20)
    if proc.returncode != 0:
        print(f"移除失败：{proc.stderr.strip()}")
        return 1
    print("✓ 已移除本项目写入的 cron 行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/安装定时任务")
    ap.add_argument("--install", action="store_true", help="写入 crontab（Linux）")
    ap.add_argument("--uninstall", action="store_true", help="移除本项目的 cron 行")
    args = ap.parse_args()

    cfg = load_config()
    lines = build_lines(cfg)

    print("当前配置对应的定时任务：")
    if not lines:
        print("  （全关了：没有任何任务）")
    for line in lines:
        print("  " + line)

    windows = os.name == "nt"
    if args.uninstall:
        if windows:
            print("\nWindows 上不管理 cron；请到「任务计划程序」里删除 CS2 相关任务。")
            return 1
        return _uninstall()
    if args.install:
        if windows:
            print("\nWindows 不支持 crontab。两种做法：")
            print("  1) 常驻：python main.py schedule   （按配置自动跑日报/午报/周报/巡检）")
            print("  2) 任务计划程序：把上面每条命令做成一个触发器")
            return 1
        if not shutil.which("crontab"):
            print("\n找不到 crontab 命令，确认这台机器上装了 cron。")
            return 1
        return _install(lines)

    print("\n以上只是预览。确认无误后加 --install 写入（会备份原 crontab）。")
    print("提示：也可以不装 cron，直接 `python main.py schedule` 常驻运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
