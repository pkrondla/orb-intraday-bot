# Registers a Windows scheduled task that starts the paper session every weekday morning.
#
#   powershell -ExecutionPolicy Bypass -File scripts\forward_test\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\forward_test\register_tasks.ps1 -Remove
#
# Task Scheduler uses this PC's local time, the market runs on IST, so the start time is given in
# IST and converted. The bot waits for the market itself, so starting early is harmless. If your
# local time zone changes its clock (daylight saving), run this script again.
param(
    [string]$IstStart = "09:00",
    [string]$TaskName = "ORB Intraday Bot (paper)",
    [switch]$Remove
)

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$TaskName'."
    exit 0
}

$ist = [TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$local = [TimeZoneInfo]::Local
$hour, $minute = $IstStart.Split(":") | ForEach-Object { [int]$_ }

# A Monday in IST, converted to local time: gives the local clock time and the day shift.
$monday = [DateTime]::SpecifyKind((Get-Date "2026-10-19").Date.AddHours($hour).AddMinutes($minute), "Unspecified")
$localStart = [TimeZoneInfo]::ConvertTime($monday, $ist, $local)
$shift = ($localStart.Date - $monday.Date).Days
$days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday") | ForEach-Object {
    [DayOfWeek](([int][DayOfWeek]$_ + $shift + 7) % 7)
}

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "run_day.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Python `"$python`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $localStart.ToString("HH:mm")
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 9) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Paper-trading session for the ORB Intraday Bot. Never places orders." -Force | Out-Null

Write-Host ("Registered '$TaskName': weekdays at {0} local time ({1} IST). The PC must be on " +
    "and signed in; the task wakes it from sleep." -f $localStart.ToString("HH:mm"), $IstStart)
Write-Host "Logs: $repo\data\logs"
