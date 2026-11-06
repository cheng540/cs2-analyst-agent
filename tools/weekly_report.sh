#!/bin/bash
# ============================================================
#  CS2 模拟盘周报（由 cron 每周日 20:00 调用）
#  作用：汇总双轨模拟盘/持仓/大盘/巡检次数，并充当运行心跳检测
#  手动运行： bash tools/weekly_report.sh
# ============================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
mkdir -p logs

LOG=$ROOT/logs/weekly.log
{
  echo ""
  echo "=============== $(date '+%Y-%m-%d %H:%M:%S') 周报生成 ==============="
} >> "$LOG"

PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY=python3
"$PY" tools/run_scheduled.py --task weekly "$@" >> "$LOG" 2>&1
CODE=$?

echo "--------------- $(date '+%Y-%m-%d %H:%M:%S') 结束(exit=$CODE) ---------------" >> "$LOG"

find "$ROOT/logs" -name 'weekly.log*' -mtime +180 -delete 2>/dev/null
exit $CODE
