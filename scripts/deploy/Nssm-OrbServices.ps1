# Shared NSSM stop/install for OrbBot and OrbBotWatch.
# Used by Install-OrbBot.ps1 and the CD WinRM steps so they cannot drift.
#
#   powershell -File Nssm-OrbServices.ps1 -Action Stop
#   powershell -File Nssm-OrbServices.ps1 -Action Install
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Stop", "Install")]
    [string]$Action,
    [string]$RepoPath = "C:\orb-bot",
    [string]$ServiceName = "OrbBot",
    [string]$WatchServiceName = "OrbBotWatch"
)

$ErrorActionPreference = "Stop"

function Invoke-Nssm {
    param([string]$Label, [string[]]$NssmArgs)
    Write-Host $Label
    & nssm @NssmArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

function Get-NssmStatus {
    param([string]$Name)
    if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) { return "MISSING" }
    return ((& nssm status $Name 2>&1) | Out-String).Trim()
}

function Wait-NssmStatus {
    param([string]$Name, [string[]]$Wanted, [int]$Seconds = 40)
    $deadline = (Get-Date).AddSeconds($Seconds)
    $status = Get-NssmStatus $Name
    while ((Get-Date) -lt $deadline) {
        if ($Wanted -contains $status) { return $status }
        Start-Sleep -Milliseconds 500
        $status = Get-NssmStatus $Name
    }
    return $status
}

function Stop-NssmService {
    param([string]$Name)
    if ((Get-NssmStatus $Name) -eq "MISSING") { return }
    Write-Host "nssm stop $Name"
    & nssm stop $Name | Out-Null
    $status = Wait-NssmStatus -Name $Name -Wanted @("SERVICE_STOPPED", "MISSING") -Seconds 40
    if (@("SERVICE_STOPPED", "MISSING") -notcontains $status) {
        Write-Host "force-stopping $Name (was $status)"
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        $status = Wait-NssmStatus -Name $Name -Wanted @("SERVICE_STOPPED", "MISSING") -Seconds 20
    }
    if (@("SERVICE_STOPPED", "MISSING") -notcontains $status) {
        throw "Could not stop $Name (status $status)"
    }
}

function Remove-NssmService {
    param([string]$Name)
    if ((Get-NssmStatus $Name) -eq "MISSING") { return }
    Stop-NssmService -Name $Name
    Write-Host "nssm remove $Name"
    & nssm remove $Name confirm | Out-Null
    $status = Wait-NssmStatus -Name $Name -Wanted @("MISSING") -Seconds 20
    if ($status -ne "MISSING") {
        throw "Could not remove $Name (status $status)"
    }
}

function Start-NssmService {
    param([string]$Name)
    $status = Get-NssmStatus $Name
    Write-Host "nssm start $Name (current $status)"
    # nssm start exits 1 if the service is already START_PENDING or RUNNING.
    if ($status -ne "SERVICE_RUNNING" -and $status -ne "SERVICE_START_PENDING") {
        & nssm start $Name | Out-Null
    }
    $status = Wait-NssmStatus -Name $Name -Wanted @("SERVICE_RUNNING") -Seconds 45
    if ($status -ne "SERVICE_RUNNING") {
        throw "Service $Name failed to start: $status"
    }
    Write-Host "$Name is running."
}

function Install-NssmService {
    param(
        [string]$Name,
        [string]$Launcher,
        [string]$Stdout,
        [string]$Stderr,
        [string]$Repo
    )
    $powershell = (Get-Command powershell.exe).Source
    Remove-NssmService -Name $Name
    Invoke-Nssm "nssm install $Name" @("install", $Name, $powershell)
    if ((Get-NssmStatus $Name) -eq "MISSING") {
        throw "nssm install $Name returned success but the service was not created."
    }
    $params = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Launcher`""
    Invoke-Nssm "nssm set AppParameters $Name" @("set", $Name, "AppParameters", $params)
    Invoke-Nssm "nssm set AppDirectory $Name" @("set", $Name, "AppDirectory", $Repo)
    Invoke-Nssm "nssm set AppStdout $Name" @("set", $Name, "AppStdout", $Stdout)
    Invoke-Nssm "nssm set AppStderr $Name" @("set", $Name, "AppStderr", $Stderr)
    Invoke-Nssm "nssm set AppRotateFiles $Name" @("set", $Name, "AppRotateFiles", "1")
    Invoke-Nssm "nssm set AppRotateOnline $Name" @("set", $Name, "AppRotateOnline", "1")
    Invoke-Nssm "nssm set AppExit $Name" @("set", $Name, "AppExit", "Default", "Restart")
    Invoke-Nssm "nssm set AppThrottle $Name" @("set", $Name, "AppThrottle", "15000")
    # Demand-start for the first start so SCM does not race nssm (START_PENDING).
    Invoke-Nssm "nssm set Start $Name" @("set", $Name, "Start", "SERVICE_DEMAND_START")
    Start-NssmService -Name $Name
    Invoke-Nssm "nssm set Start $Name" @("set", $Name, "Start", "SERVICE_AUTO_START")
}

switch ($Action) {
    "Stop" {
        Stop-NssmService -Name $ServiceName
        Stop-NssmService -Name $WatchServiceName
        Write-Host "services stopped"
    }
    "Install" {
        $logs = Join-Path $RepoPath "logs"
        New-Item -ItemType Directory -Path $logs -Force | Out-Null
        Install-NssmService -Name $ServiceName `
            -Launcher (Join-Path $RepoPath "start-orb-bot.ps1") `
            -Stdout (Join-Path $logs "orb-bot.log") `
            -Stderr (Join-Path $logs "orb-bot-error.log") `
            -Repo $RepoPath
        Install-NssmService -Name $WatchServiceName `
            -Launcher (Join-Path $RepoPath "start-orb-watch.ps1") `
            -Stdout (Join-Path $logs "orb-watch.log") `
            -Stderr (Join-Path $logs "orb-watch-error.log") `
            -Repo $RepoPath
    }
}
