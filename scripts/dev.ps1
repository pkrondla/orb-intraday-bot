# Developer tasks. One command per quality gate, plus `ci` which runs them all like CI does.
#
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 setup   # clean venv from the lock file
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 ci      # everything CI runs
#   ... lint | types | imports | test | cov | audit | lock
#
# The virtual environment lives outside the repository (OneDrive would sync thousands of files).
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "lint", "types", "imports", "test", "cov", "audit", "lock", "ci")]
    [string]$Task = "ci",
    [string]$Venv = (Join-Path $env:USERPROFILE "venvs\orb-bot")
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$py = Join-Path $Venv "Scripts\python.exe"
$bin = Join-Path $Venv "Scripts"

function Run([string]$title, [scriptblock]$cmd) {
    Write-Host "`n== $title" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $title" -ForegroundColor Red; exit $LASTEXITCODE }
}

function Need-Venv {
    if (-not (Test-Path $py)) { throw "No virtual environment at $Venv. Run: dev.ps1 setup" }
}

switch ($Task) {
    "setup" {
        if (-not (Test-Path $py)) { python -m venv $Venv }
        Run "install pinned dependencies" {
            & $py -m pip install --upgrade pip
            & $py -m pip install --no-deps -r requirements-dev.lock
            & $py -m pip install --no-deps -e .
        }
    }
    "lint"    { Need-Venv; Run "ruff" { & $py -m ruff check src tests scripts } }
    "types"   { Need-Venv; Run "mypy strict" { & $py -m mypy src } }
    "imports" { Need-Venv; Run "architecture rules" { & (Join-Path $bin "lint-imports.exe") } }
    "test"    { Need-Venv; Run "tests" { & $py -m pytest -q } }
    "cov"     { Need-Venv; Run "tests with coverage gate" { & $py -m pytest -q --cov --cov-report=term-missing } }
    "audit"   { Need-Venv; Run "dependency audit" { & (Join-Path $bin "pip-audit.exe") -r requirements.lock --disable-pip --no-deps } }
    "lock" {
        Need-Venv
        # Regenerate the lock files from a clean install of exactly what pyproject.toml allows.
        # growwapi 1.5.0 still declares certifi<2026.0.0, which blocks PYSEC-2026-4320.
        # Overlay the patched CA bundle, then install the freeze with --no-deps.
        $tmp = Join-Path $env:TEMP "orb-bot-lock"
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        python -m venv $tmp
        $tp = Join-Path $tmp "Scripts\python.exe"
        $certifiFloor = "certifi>=2026.10.19"
        Run "resolve runtime" {
            & $tp -m pip install --quiet .
            & $tp -m pip install --quiet --no-deps $certifiFloor
            & $tp -m pip freeze --exclude orb-intraday-bot | Out-File -Encoding ascii requirements.lock
        }
        Run "resolve dev" {
            & $tp -m pip install --quiet ".[dev]"
            & $tp -m pip install --quiet --no-deps $certifiFloor
            & $tp -m pip freeze --exclude orb-intraday-bot | Out-File -Encoding ascii requirements-dev.lock
        }
        Remove-Item -Recurse -Force $tmp
        Write-Host "Lock files rewritten. Review the diff, run 'dev.ps1 ci', then commit." -ForegroundColor Green
    }
    "ci" {
        Need-Venv
        Run "ruff" { & $py -m ruff check src tests scripts }
        Run "mypy strict" { & $py -m mypy src }
        Run "architecture rules" { & (Join-Path $bin "lint-imports.exe") }
        Run "tests with coverage gate" { & $py -m pytest -q --cov --cov-report=term }
        Write-Host "`nAll gates passed." -ForegroundColor Green
    }
}
