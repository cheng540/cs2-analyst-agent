#!/bin/bash
# ============================================================
#  CS2 饰品价格异动巡检（由 cron 在 10/12/15/19/22 点调用）
#  手动运行： bash tools/run_alerts.sh
# ============================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
mkdir -p logs

LOG=$ROOT/logs/alerts.log
{
  echo ""
  echo "=============== $(date '+%Y-%m-%d %H:%M:%S') 巡检开始 ==============="
} >> "$LOG"

PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY=python3
"$PY" tools/run_scheduled.py --task alerts >> "$LOG" 2>&1
CODE=$?

echo "--------------- $(date '+%Y-%m-%d %H:%M:%S') 结束(exit=$CODE) ---------------" >> "$LOG"

# 日志保留 90 天
find "$ROOT/logs" -name 'alerts.log*' -mtime +90 -delete 2>/dev/null
exit $CODE
