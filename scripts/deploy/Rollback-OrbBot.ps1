# Roll back to the previously recorded git ref and reinstall services.
param(
    [string]$RepoPath = "C:\orb-bot"
)

$ErrorActionPreference = "Stop"
$prevFile = Join-Path $RepoPath "deploy\previous_ref.txt"
if (-not (Test-Path $prevFile)) {
    throw "No previous_ref.txt at $prevFile. Cannot roll back."
}
$ref = (Get-Content $prevFile -Raw).Trim()
if (-not $ref) { throw "previous_ref.txt is empty." }
Write-Host "Rolling back to $ref"
& (Join-Path $PSScriptRoot "Install-OrbBot.ps1") -RepoPath $RepoPath -GitRef $ref
