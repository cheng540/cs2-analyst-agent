# ============================================================
#  Sync cloud data to local (reports / ledger / judgements / intel)
#  Usage:
#      $env:CS2_SERVER_HOST = "your.server.ip"
#      powershell -ExecutionPolicy Bypass -File tools\sync_from_server.ps1
#  No secrets in this file: host and key path come from environment variables.
#  (ASCII only: PowerShell 5.1 reads .ps1 as GBK and fails on Chinese comments.)
# ============================================================
param(
    [string]$ServerHost = $env:CS2_SERVER_HOST,
    [string]$User = "root",
    [int]$Port = 22,
    [string]$KeyPath = $env:CS2_SSH_KEY,
    [string]$RemoteRoot = "/root/cs2-analyst",
    [string]$LocalRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

if (-not $ServerHost) {
    Write-Host "CS2_SERVER_HOST is not set." -ForegroundColor Red
    Write-Host "  set it first, e.g.  `$env:CS2_SERVER_HOST = '1.2.3.4'" -ForegroundColor Yellow
    exit 1
}
if (-not $KeyPath) { $KeyPath = Join-Path $env:USERPROFILE ".ssh\cs2_analyst_deploy" }

Write-Host "=== Sync from cloud ===" -ForegroundColor Cyan
Write-Host "  host : $ServerHost"
Write-Host "  key  : $KeyPath"
Write-Host "  local: $LocalRoot"

$items = @(
    @{ remote = "$RemoteRoot/reports/";               local = "$LocalRoot\reports_from_cloud\" },
    @{ remote = "$RemoteRoot/data/sim/";              local = "$LocalRoot\data\sim_from_cloud\" },
    @{ remote = "$RemoteRoot/data/llm/";              local = "$LocalRoot\data\llm_from_cloud\" },
    @{ remote = "$RemoteRoot/data/intel/";            local = "$LocalRoot\data\intel_from_cloud\" },
    @{ remote = "$RemoteRoot/data/holdings_history/"; local = "$LocalRoot\data\holdings_history_from_cloud\" },
    @{ remote = "$RemoteRoot/data/weekly/";           local = "$LocalRoot\data\weekly_from_cloud\" },
    @{ remote = "$RemoteRoot/data/alerts/";           local = "$LocalRoot\data\alerts_from_cloud\" },
    @{ remote = "$RemoteRoot/data/";                  local = "$LocalRoot\data\cloud_root\" }
)

foreach ($it in $items) {
    New-Item -ItemType Directory -Force -Path $it.local | Out-Null
    Write-Host "  pull $($it.remote) ..." -NoNewline
    scp -i $KeyPath -P $Port -o BatchMode=yes -o ConnectTimeout=20 -r "$($User)@$($ServerHost):$($it.remote)*" $it.local 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host " ok" -ForegroundColor Green }
    else { Write-Host " skipped (empty or missing)" -ForegroundColor Yellow }
}

# keep cloud_root tidy: only briefs / name map / base items
Get-ChildItem "$LocalRoot\data\cloud_root" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike "brief_*" -and $_.Name -notlike "name_map.json" -and $_.Name -notlike "base_items.json" } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "  pull briefs ..." -NoNewline
scp -i $KeyPath -P $Port -o BatchMode=yes "$($User)@$($ServerHost):$RemoteRoot/data/brief_*.md" "$LocalRoot\data\cloud_root\" 2>$null | Out-Null
Write-Host " ok" -ForegroundColor Green

Write-Host ""
Write-Host "=== Done. Local copies ===" -ForegroundColor Cyan
Write-Host "  reports   : $LocalRoot\reports_from_cloud\"
Write-Host "  paper sim : $LocalRoot\data\sim_from_cloud\  (state_a.json / state_b.json)"
Write-Host "  llm       : $LocalRoot\data\llm_from_cloud\"
Write-Host "  intel     : $LocalRoot\data\intel_from_cloud\"
Write-Host "  holdings  : $LocalRoot\data\holdings_history_from_cloud\"
Write-Host "  weekly    : $LocalRoot\data\weekly_from_cloud\"
Write-Host "  alerts    : $LocalRoot\data\alerts_from_cloud\"
Write-Host "  briefs    : $LocalRoot\data\cloud_root\brief_*.md"
Write-Host ""
Write-Host "After syncing, open the console:  python tools/report_server.py  ->  http://127.0.0.1:8899/app/" -ForegroundColor Cyan
