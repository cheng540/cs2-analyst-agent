@echo off
REM ============================================================
REM  CS2 skin portfolio analyst - daily auto run (called by Task Scheduler)
REM  You can also double-click this file to run it manually.
REM  Output is appended to logs\daily_run.log
REM ============================================================
chcp 65001 >nul
cd /d E:\deepseek
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist logs mkdir logs

echo. >> logs\daily_run.log
echo ================ %date% %time% START ================ >> logs\daily_run.log
"E:\develop\Python\python.exe" main.py run >> logs\daily_run.log 2>&1
echo ---------------- %date% %time% END (exit code %ERRORLEVEL%) ---------------- >> logs\daily_run.log
