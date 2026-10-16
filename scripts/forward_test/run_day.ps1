# Runs one paper session, then compares it with a replay of the same candles.
# Started by the scheduled task (see register_tasks.ps1) or by hand. Paper only: no orders.
param(
    [string]$Python = "python"
)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$env:PYTHONPATH = Join-Path $repo "src"
$env:PYTHONIOENCODING = "utf-8"

$logs = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = (Get-Date).ToString("yyyy-MM-dd")
$console = Join-Path $logs "task-$stamp.log"

"=== run $(Get-Date -Format s)" | Out-File -Append -Encoding utf8 $console
& $Python -m orb_intraday_bot run 2>&1 | Out-File -Append -Encoding utf8 $console
$code = $LASTEXITCODE
"=== run ended with exit code $code at $(Get-Date -Format s)" | Out-File -Append -Encoding utf8 $console

# Exit code 3 = another run is alive; 0 or 130 = normal. Reconcile only after a normal run.
if ($code -eq 0) {
    "=== reconcile" | Out-File -Append -Encoding utf8 $console
    & $Python -m orb_intraday_bot reconcile 2>&1 | Out-File -Append -Encoding utf8 $console
    & $Python -m orb_intraday_bot summary 2>&1 | Out-File -Append -Encoding utf8 $console
}
exit $code
