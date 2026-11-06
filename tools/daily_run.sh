#!/bin/bash
# ============================================================
#  CS2 饰品持仓智能分析 - 服务器每日运行脚本（由 cron 调用）
#  手动运行： bash tools/daily_run.sh
# ============================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
mkdir -p logs

LOG=$ROOT/logs/daily_run.log
{
  echo ""
  echo "================ $(date '+%Y-%m-%d %H:%M:%S') START ================"
} >> "$LOG"

PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY=python3
"$PY" tools/run_scheduled.py --task daily >> "$LOG" 2>&1
CODE=$?

echo "---------------- $(date '+%Y-%m-%d %H:%M:%S') END (exit=$CODE) ----------------" >> "$LOG"

# 日志按月清理（保留 90 天）
find "$ROOT/logs" -name '*.log*' -mtime +90 -delete 2>/dev/null
exit $CODE
