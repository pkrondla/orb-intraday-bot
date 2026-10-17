# Server-side install/update for the ORB bot on Windows EC2.
# Called from the GitHub Actions WinRM deploy. Idempotent. It deploys at any time, including during
# market hours: the bot restores open positions from SQLite on start and their broker stops stay.
param(
    [string]$RepoPath = "C:\orb-bot",
    [string]$PythonExe = "",
    [string]$ServiceName = "OrbBot",
    [string]$WatchServiceName = "OrbBotWatch",
    [string]$GitRef = "main",
    [switch]$SkipGit
)

$ErrorActionPreference = "Stop"
Write-Host "Install-OrbBot.ps1: Invoke-Native uses -Label (NSSM service-name fix)"

function Invoke-Native([scriptblock]$Command, [string]$Label) {
    # The label must not be called $Name: scriptblocks look up $Name here and would
    # pass the label to nssm instead of the service name (the first-install failure).
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Resolve-Python {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path $Preferred)) { return $Preferred }
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe",
        "C:\Python312\python.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { return $found }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python 3.12 not found. Install it system-wide or pass -PythonExe."
}

$python = Resolve-Python -Preferred $PythonExe
$logsDir = Join-Path $RepoPath "logs"
$dataDir = Join-Path $RepoPath "data"
$deployDir = Join-Path $RepoPath "deploy"
$venvPython = Join-Path $RepoPath ".venv\Scripts\python.exe"

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    throw "nssm was not found on PATH."
}

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
New-Item -ItemType Directory -Path $deployDir -Force | Out-Null

# Record previous ref for rollback.
$prevFile = Join-Path $deployDir "previous_ref.txt"
$currentFile = Join-Path $deployDir "current_ref.txt"
if (Test-Path $currentFile) {
    Copy-Item $currentFile $prevFile -Force
}

# Git Credential Manager on this host has no Windows credential store in a WinRM
# session. The clone/pull still works; this only stops the fatal-looking warning.
$env:GCM_CREDENTIAL_STORE = "none"
$env:GCM_INTERACTIVE = "never"

if (-not $SkipGit) {
    if (Test-Path (Join-Path $RepoPath ".git")) {
        Invoke-Native { git -C $RepoPath fetch --all --tags } "git fetch"
        Invoke-Native { git -C $RepoPath checkout $GitRef } "git checkout"
        Invoke-Native { git -C $RepoPath pull --ff-only origin $GitRef } "git pull"
    } else {
        throw "Repo not found at $RepoPath. Clone it first (the CD workflow does this)."
    }
}

$sha = (git -C $RepoPath rev-parse HEAD).Trim()
Set-Content -Path $currentFile -Value $sha -Encoding utf8

# venv
if (-not (Test-Path $venvPython)) {
    Invoke-Native { & $python -m venv (Join-Path $RepoPath ".venv") } "create venv"
}
Invoke-Native { & $venvPython -m pip install --upgrade pip } "pip upgrade"
Invoke-Native { & $venvPython -m pip install -r (Join-Path $RepoPath "requirements.lock") } "pip install lock"
Invoke-Native { & $venvPython -m pip install --no-deps -e $RepoPath } "pip install package"

# Launchers
$serveLauncher = Join-Path $RepoPath "start-orb-bot.ps1"
$watchLauncher = Join-Path $RepoPath "start-orb-watch.ps1"
@(
    '$ErrorActionPreference = "Stop"'
    "`$repo = `"$RepoPath`""
    'Get-Content (Join-Path $repo ".env") -ErrorAction SilentlyContinue | ForEach-Object {'
    '  if ($_ -match "^\s*#" -or [string]::IsNullOrWhiteSpace($_)) { return }'
    '  $parts = $_.Split("=", 2)'
    '  if ($parts.Count -eq 2) { [System.Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process") }'
    '}'
    "Set-Location `$repo"
    "& `"$venvPython`" -m orb_intraday_bot serve"
) | Set-Content -Path $serveLauncher -Encoding utf8

@(
    '$ErrorActionPreference = "Stop"'
    "`$repo = `"$RepoPath`""
    'Get-Content (Join-Path $repo ".env") -ErrorAction SilentlyContinue | ForEach-Object {'
    '  if ($_ -match "^\s*#" -or [string]::IsNullOrWhiteSpace($_)) { return }'
    '  $parts = $_.Split("=", 2)'
    '  if ($parts.Count -eq 2) { [System.Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process") }'
    '}'
    "Set-Location `$repo"
    "& `"$venvPython`" -m orb_intraday_bot watchdog"
) | Set-Content -Path $watchLauncher -Encoding utf8

function Install-NssmService {
    param([string]$ServiceName, [string]$Launcher, [string]$Stdout, [string]$Stderr)
    $powershell = (Get-Command powershell.exe).Source
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Invoke-Native { nssm stop $ServiceName confirm } "nssm stop $ServiceName" | Out-Null
        Start-Sleep -Seconds 2
        Invoke-Native { nssm remove $ServiceName confirm } "nssm remove $ServiceName" | Out-Null
        Start-Sleep -Seconds 1
    }
    Invoke-Native { nssm install $ServiceName $powershell } "nssm install $ServiceName" | Out-Null
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        throw "nssm install $ServiceName returned success but the service was not created."
    }
    Invoke-Native {
        nssm set $ServiceName AppParameters "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Launcher`""
    } "nssm set AppParameters $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppDirectory $RepoPath } "nssm set AppDirectory $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppStdout $Stdout } "nssm set AppStdout $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppStderr $Stderr } "nssm set AppStderr $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppRotateFiles 1 } "nssm set AppRotateFiles $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppRotateOnline 1 } "nssm set AppRotateOnline $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName Start SERVICE_AUTO_START } "nssm set Start $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppExit Default Restart } "nssm set AppExit $ServiceName" | Out-Null
    Invoke-Native { nssm set $ServiceName AppThrottle 15000 } "nssm set AppThrottle $ServiceName" | Out-Null
    Invoke-Native { nssm start $ServiceName } "nssm start $ServiceName" | Out-Null
    Start-Sleep -Seconds 3
    $status = (nssm status $ServiceName).Trim()
    if ($status -ne "SERVICE_RUNNING") { throw "Service $ServiceName failed to start: $status" }
    Write-Host "$ServiceName is running."
}

Install-NssmService -ServiceName $ServiceName -Launcher $serveLauncher `
    -Stdout (Join-Path $logsDir "orb-bot.log") -Stderr (Join-Path $logsDir "orb-bot-error.log")
Install-NssmService -ServiceName $WatchServiceName -Launcher $watchLauncher `
    -Stdout (Join-Path $logsDir "orb-watch.log") -Stderr (Join-Path $logsDir "orb-watch-error.log")

# Smoke check (read-only)
Invoke-Native { & $venvPython -m orb_intraday_bot check } "orb-bot check"
Write-Host "Deploy complete: $sha"
