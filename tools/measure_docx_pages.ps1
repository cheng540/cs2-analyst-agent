# Measure REAL page count of a docx via local Word / WPS COM automation.
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\measure_docx_pages.ps1 "path\to\file.docx"
#
# Why: estimation can only guess layout. The user's export environment (WPS/Word + doc grid)
# stretches 9pt body lines to ~15.5pt instead of the font-natural 11.88pt - a 30% difference
# that decides "1 page or 2 pages". This script measures the truth.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as GBK unless it has a
# UTF-8 BOM, so non-ASCII comments break parsing with confusing "Unexpected token" errors.
param(
    [Parameter(Mandatory = $true)][string]$Docx,
    [string]$PdfOut = ""
)

$ErrorActionPreference = "Stop"
$full = (Resolve-Path $Docx).Path
if (-not $PdfOut) {
    $PdfOut = Join-Path $env:TEMP ("measure_" + [IO.Path]::GetFileNameWithoutExtension($full) + ".pdf")
}

# Record pre-existing Word/WPS pids so we can clean up ONLY the headless instances this script
# creates. Word's COM Quit() sometimes leaves an invisible WINWORD.EXE behind (observed: 6
# leftovers after 4 runs, ~100MB each). Killing a process with a window would lose user data,
# so the cleanup below only touches pid's we spawned AND that have no main window.
$prevWord = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$prevWps = @(Get-Process wps -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)

function Stop-HeadlessOrphans($name, $prev) {
    $now = @(Get-Process $name -ErrorAction SilentlyContinue)
    $mine = @($now | Where-Object { $prev -notcontains $_.Id -and $_.MainWindowHandle -eq 0 })
    if ($mine.Count -gt 0) {
        Write-Host ("[{0}] cleaning {1} headless leftover process(es): {2}" -f $name, $mine.Count, (($mine.Id) -join ','))
        $mine | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

function Measure-With($progId, $label) {
    $app = $null
    try {
        $app = New-Object -ComObject $progId
        $app.Visible = $false
        try { $app.DisplayAlerts = 0 } catch {}
        $doc = $app.Documents.Open($full, $false, $true)   # ConfirmConversions=false, ReadOnly=true
        Start-Sleep -Milliseconds 400
        $pages = $doc.ComputeStatistics(2)                 # wdStatisticPages
        $words = $doc.ComputeStatistics(0)                 # wdStatisticWords
        $chars = $doc.ComputeStatistics(3)                 # wdStatisticCharacters
        Write-Host ("[{0}] pages={1}  words={2}  chars={3}" -f $label, $pages, $words, $chars)
        if ($label -eq "Word") {
            $doc.SaveAs2($PdfOut, 17)                      # wdFormatPDF
            Write-Host ("[Word] exported PDF -> {0}" -f $PdfOut)
        }
        $doc.Close(0)
    } catch {
        Write-Host ("[{0}] unavailable/error: {1}" -f $label, $_.Exception.Message)
    } finally {
        if ($app) { try { $app.Quit(0) } catch {} ; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) }
    }
}

Measure-With "Word.Application" "Word"
Stop-HeadlessOrphans "WINWORD" $prevWord
Measure-With "KWps.Application" "WPS"
Stop-HeadlessOrphans "wps" $prevWps
