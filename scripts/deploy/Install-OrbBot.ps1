# Server-side install/update for the ORB bot on Windows EC2.
# Called from the GitHub Actions WinRM deploy. Idempotent. It deploys at any time, including during
# market hours: the bot restores open positions from SQLite on start and their broker stops stay.
param(
    [string]$RepoPath = "C:\orb-bot",
    [string]$PythonExe = "",
    [string]$ServiceName = "OrbBot",
    [string]$WatchServiceName = "OrbBotWatch",
    [string]$GitRef = "main"
)

$ErrorActionPreference = "Stop"

function Invoke-Native([scriptblock]$Command, [string]$Name) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
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

if (Test-Path (Join-Path $RepoPath ".git")) {
    Invoke-Native { git -C $RepoPath fetch --all --tags } "git fetch"
    Invoke-Native { git -C $RepoPath checkout $GitRef } "git checkout"
    Invoke-Native { git -C $RepoPath pull --ff-only origin $GitRef } "git pull"
} else {
    throw "Repo not found at $RepoPath. Clone it first (the CD workflow does this)."
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
    param([string]$Name, [string]$Launcher, [string]$Stdout, [string]$Stderr)
    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
        Invoke-Native { nssm stop $Name confirm } "nssm stop $Name" | Out-Null
        Start-Sleep -Seconds 2
        Invoke-Native { nssm remove $Name confirm } "nssm remove $Name" | Out-Null
    }
    Invoke-Native { nssm install $Name "powershell.exe" } "nssm install $Name" | Out-Null
    Invoke-Native {
        nssm set $Name AppParameters "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Launcher`""
    } "nssm set AppParameters $Name" | Out-Null
    Invoke-Native { nssm set $Name AppDirectory $RepoPath } "nssm set AppDirectory $Name" | Out-Null
    Invoke-Native { nssm set $Name AppStdout $Stdout } "nssm set AppStdout $Name" | Out-Null
    Invoke-Native { nssm set $Name AppStderr $Stderr } "nssm set AppStderr $Name" | Out-Null
    Invoke-Native { nssm set $Name AppRotateFiles 1 } "nssm set AppRotateFiles $Name" | Out-Null
    Invoke-Native { nssm set $Name AppRotateOnline 1 } "nssm set AppRotateOnline $Name" | Out-Null
    Invoke-Native { nssm set $Name Start SERVICE_AUTO_START } "nssm set Start $Name" | Out-Null
    Invoke-Native { nssm set $Name AppExit Default Restart } "nssm set AppExit $Name" | Out-Null
    Invoke-Native { nssm set $Name AppThrottle 15000 } "nssm set AppThrottle $Name" | Out-Null
    Invoke-Native { nssm start $Name } "nssm start $Name" | Out-Null
    Start-Sleep -Seconds 3
    $status = (nssm status $Name).Trim()
    if ($status -ne "SERVICE_RUNNING") { throw "Service $Name failed to start: $status" }
    Write-Host "$Name is running."
}

Install-NssmService -Name $ServiceName -Launcher $serveLauncher `
    -Stdout (Join-Path $logsDir "orb-bot.log") -Stderr (Join-Path $logsDir "orb-bot-error.log")
Install-NssmService -Name $WatchServiceName -Launcher $watchLauncher `
    -Stdout (Join-Path $logsDir "orb-watch.log") -Stderr (Join-Path $logsDir "orb-watch-error.log")

# Smoke check (read-only)
Invoke-Native { & $venvPython -m orb_intraday_bot check } "orb-bot check"
Write-Host "Deploy complete: $sha"
