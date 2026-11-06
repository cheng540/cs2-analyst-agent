#!/bin/bash
# ============================================================
#  安装「价格异动巡检」cron 任务（9:00–24:00 每小时，共 16 次）
#  用法： bash tools/install_alerts_cron.sh
# ============================================================
set -eu

CRON_LINE="0 0,9-23 * * * /root/cs2-analyst/tools/run_alerts.sh"
TMP="$(mktemp)"

# 保留原有 crontab（去掉旧的同名任务，避免重复）
crontab -l 2>/dev/null | grep -v 'run_alerts.sh' > "$TMP" || true
printf '%s\n' "$CRON_LINE" >> "$TMP"

# 安装前先自检格式
if ! crontab "$TMP" 2>/tmp/cron_err; then
  echo "❌ 安装失败："
  cat /tmp/cron_err
  echo "--- 待安装内容 ---"
  cat -A "$TMP"
  rm -f "$TMP" /tmp/cron_err
  exit 1
fi
rm -f "$TMP" /tmp/cron_err

echo "✅ 已安装价格异动巡检任务"
echo "--- 当前 crontab（cs2 相关）---"
crontab -l | grep -E 'cs2-analyst' || true
echo "--- 巡检时间：每天 9:00–24:00 每小时（0 点那条属于次日），共 16 次 ---"
echo "--- 推送策略：紧急信号即时推送；温和波动静默记录，12:00 / 22:00 汇总 ---"
echo "--- 日志：/root/cs2-analyst/logs/alerts.log ---"
