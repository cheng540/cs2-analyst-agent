<#
.SYNOPSIS
    安装/卸载「CS2 持仓分析」每日自动运行任务（Windows 任务计划程序）。

.DESCRIPTION
    - 默认每天 17:30 运行 tools\daily_run.cmd（内部执行 python main.py run）
    - 勾选「错过时间后尽快补跑」（StartWhenAvailable）：当天没开机，开机后会自动补跑一次
    - 不需要管理员权限（注册在当前用户下）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1
    powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1 -Hour 21 -Minute 0
    powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1 -Remove
#>
param(
    [int]$Hour = 17,
    [int]$Minute = 30,
    [string]$TaskName = "CS2-Analyst-Daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "daily_run.cmd"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务：$TaskName"
    exit 0
}

if (-not (Test-Path $script)) {
    throw "找不到运行脚本：$script"
}

$action = New-ScheduledTaskAction -Execute $script -WorkingDirectory $projectRoot
$timeText = "{0:D2}:{1:D2}" -f $Hour, $Minute
$trigger = New-ScheduledTaskTrigger -Daily -At $timeText
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "CS2 饰品持仓智能分析：每日自动拉取行情、分析持仓、运行双轨模拟盘并生成 Word 报告" | Out-Null

Write-Host "✅ 已安装每日自动任务：$TaskName"
Write-Host "   运行时间：每天 $timeText（错过会自动补跑）"
Write-Host "   执行内容：$script"
Write-Host "   日志文件：$projectRoot\logs\daily_run.log"
Write-Host ""
Write-Host "查看任务：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "立即测试：Start-ScheduledTask -TaskName $TaskName"
Write-Host "删除任务：powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1 -Remove"
