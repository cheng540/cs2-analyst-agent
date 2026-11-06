#!/bin/bash
# ============================================================
#  安装「每周复盘推送」cron 任务（每周日 20:00）
#  用法： bash tools/install_weekly_cron.sh
# ============================================================
set -eu

CRON_LINE="0 20 * * 0 /root/cs2-analyst/tools/weekly_report.sh"
TMP="$(mktemp)"

crontab -l 2>/dev/null | grep -v 'weekly_report.sh' > "$TMP" || true
printf '%s\n' "$CRON_LINE" >> "$TMP"

if ! crontab "$TMP" 2>/tmp/cron_err; then
  echo "❌ 安装失败："
  cat /tmp/cron_err
  echo "--- 待安装内容 ---"
  cat -A "$TMP"
  rm -f "$TMP" /tmp/cron_err
  exit 1
fi
rm -f "$TMP" /tmp/cron_err

echo "✅ 已安装每周复盘推送任务"
echo "--- 当前 crontab（cs2 相关）---"
crontab -l | grep -E 'cs2-analyst' || true
echo "--- 推送时间：每周日 20:00 ---"
echo "--- 日志：/root/cs2-analyst/logs/weekly.log ---"
