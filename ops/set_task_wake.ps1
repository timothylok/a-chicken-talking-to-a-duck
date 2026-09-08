# Turn on WakeToRun for the three daily stock report tasks, so they fire at
# their scheduled time instead of waiting for the next manual wake.
#
# Why: StartWhenAvailable (set 2026-09-08) stopped sleep from *losing* a run,
# but a run is still late by however long the machine stays asleep -- these
# three are anchored to the US close, so a report that lands hours late is
# stale. The reactive overnight watches (earnings/valuation/risk, 01:00-02:00)
# are deliberately left alone: they only poll for new SEC filings, catch-up
# serves them fine, and waking the machine nightly for them is not worth it.
#
# Caveat: this only bites on AC power. "Allow wake timers" is Enable on AC but
# Disable on DC in the current power scheme, and these tasks also carry
# DisallowStartIfOnBatteries -- so on battery the machine will not wake and the
# task would not run even if it did. Both are deliberate laptop defaults; change
# them only if you want reports off-mains too.
#
# Self-elevates. Run: powershell -File ops\set_task_wake.ps1

$ErrorActionPreference = "Stop"

$tasks = @(
    "VoiceOS Technicals Daily"
    "VoiceOS Risk Dashboard Daily"
    "VoiceOS Stock Day Range"
)

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated -- relaunching as Administrator..."
    Start-Process powershell -Verb RunAs -ArgumentList @(
        "-NoProfile", "-NoExit", "-File", $PSCommandPath
    )
    return
}

foreach ($name in $tasks) {
    $t = Get-ScheduledTask -TaskName $name
    $t.Settings.WakeToRun = $true
    Set-ScheduledTask -InputObject $t | Out-Null
    Write-Host "$name : set"
}

Write-Host ""
Get-ScheduledTask | Where-Object { $_.TaskName -like "VoiceOS*" } |
    ForEach-Object { [PSCustomObject]@{ Name = $_.TaskName; WakeToRun = $_.Settings.WakeToRun } } |
    Sort-Object Name | Format-Table -AutoSize
