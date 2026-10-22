# Shared NSSM stop/install for OrbBot and OrbBotWatch.
# Used by Install-OrbBot.ps1 and the CD WinRM steps so they cannot drift.
#
#   powershell -File Nssm-OrbServices.ps1 -Action Stop
#   powershell -File Nssm-OrbServices.ps1 -Action Install
#
# Never call `nssm stop`/`nssm start` and wait unbounded: NSSM blocks until the
# app exits, WinRM keep-alives that hang, and GitHub cancels the job at 30 min.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Stop", "Install")]
    [string]$Action,
    [string]$RepoPath = "C:\orb-bot",
    [string]$ServiceName = "OrbBot",
    [string]$WatchServiceName = "OrbBotWatch"
)

$ErrorActionPreference = "Stop"

function Get-ServiceState {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc) { return "MISSING" }
    return [string]$svc.Status
}

function Wait-ServiceState {
    param([string]$Name, [string[]]$Wanted, [int]$Seconds = 15)
    $deadline = (Get-Date).AddSeconds($Seconds)
    $status = Get-ServiceState $Name
    while ((Get-Date) -lt $deadline) {
        if ($Wanted -contains $status) { return $status }
        Start-Sleep -Milliseconds 400
        $status = Get-ServiceState $Name
    }
    return $status
}

function Invoke-NssmTimed {
    param(
        [string]$Label,
        [string[]]$NssmArgs,
        [int]$TimeoutMs = 20000,
        [switch]$IgnoreExit
    )
    Write-Host $Label
    $nssm = (Get-Command nssm).Source
    $p = Start-Process -FilePath $nssm -ArgumentList $NssmArgs -PassThru -WindowStyle Hidden
    if (-not $p.WaitForExit($TimeoutMs)) {
        Write-Host "$Label timed out after ${TimeoutMs}ms; killing nssm pid $($p.Id)"
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        if (-not $IgnoreExit) { throw "$Label timed out" }
        return
    }
    if (-not $IgnoreExit -and $p.ExitCode -ne 0) {
        throw "$Label failed with exit code $($p.ExitCode)"
    }
}

function Stop-NssmService {
    param([string]$Name)
    $status = Get-ServiceState $Name
    if ($status -eq "MISSING") { return }
    Write-Host "sc.exe stop $Name (was $status)"
    # sc.exe returns immediately. nssm stop can block until the process exits.
    & sc.exe stop $Name | Out-Null
    $status = Wait-ServiceState -Name $Name -Wanted @("Stopped", "MISSING") -Seconds 12
    if (@("Stopped", "MISSING") -contains $status) { return }
    Write-Host "force-killing $Name (was $status)"
    $cim = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
    if ($cim -and $cim.ProcessId -and $cim.ProcessId -ne 0) {
        & taskkill.exe /F /T /PID $cim.ProcessId | Out-Null
    }
    Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
    $status = Wait-ServiceState -Name $Name -Wanted @("Stopped", "MISSING") -Seconds 8
    if (@("Stopped", "MISSING") -notcontains $status) {
        throw "Could not stop $Name (status $status)"
    }
}

function Remove-NssmService {
    param([string]$Name)
    if ((Get-ServiceState $Name) -eq "MISSING") { return }
    Stop-NssmService -Name $Name
    Invoke-NssmTimed "nssm remove $Name" @("remove", $Name, "confirm") -TimeoutMs 15000
    $status = Wait-ServiceState -Name $Name -Wanted @("MISSING") -Seconds 10
    if ($status -ne "MISSING") {
        throw "Could not remove $Name (status $status)"
    }
}

function Start-NssmService {
    param([string]$Name)
    $status = Get-ServiceState $Name
    Write-Host "nssm start $Name (current $status)"
    if ($status -ne "Running" -and $status -ne "StartPending") {
        # Ignore exit: nssm returns 1 when already StartPending/Running.
        Invoke-NssmTimed "nssm start $Name" @("start", $Name) -TimeoutMs 15000 -IgnoreExit
    }
    $status = Wait-ServiceState -Name $Name -Wanted @("Running") -Seconds 20
    if ($status -ne "Running") {
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
    Invoke-NssmTimed "nssm install $Name" @("install", $Name, $powershell)
    if ((Get-ServiceState $Name) -eq "MISSING") {
        throw "nssm install $Name returned success but the service was not created."
    }
    $params = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Launcher`""
    Invoke-NssmTimed "nssm set AppParameters $Name" @("set", $Name, "AppParameters", $params)
    Invoke-NssmTimed "nssm set AppDirectory $Name" @("set", $Name, "AppDirectory", $Repo)
    Invoke-NssmTimed "nssm set AppStdout $Name" @("set", $Name, "AppStdout", $Stdout)
    Invoke-NssmTimed "nssm set AppStderr $Name" @("set", $Name, "AppStderr", $Stderr)
    Invoke-NssmTimed "nssm set AppRotateFiles $Name" @("set", $Name, "AppRotateFiles", "1")
    Invoke-NssmTimed "nssm set AppRotateOnline $Name" @("set", $Name, "AppRotateOnline", "1")
    Invoke-NssmTimed "nssm set AppExit $Name" @("set", $Name, "AppExit", "Default", "Restart")
    Invoke-NssmTimed "nssm set AppThrottle $Name" @("set", $Name, "AppThrottle", "15000")
    Invoke-NssmTimed "nssm set Start $Name" @("set", $Name, "Start", "SERVICE_DEMAND_START")
    Start-NssmService -Name $Name
    Invoke-NssmTimed "nssm set Start $Name" @("set", $Name, "Start", "SERVICE_AUTO_START")
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
