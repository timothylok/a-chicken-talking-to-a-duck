# Turn on StartWhenAvailable ("run as soon as possible after a missed start")
# for the VoiceOS tasks that lacked it.
#
# Why: the machine slept 05:45-10:11 on 2026-09-07 and six runs were skipped
# outright rather than caught up on resume -- Category 4 technicals, the
# Category 6 dashboard, the earnings notify, NVIDIA daily, milk and price
# watch. The three already-fixed stock tasks got this in the 2026-09-07 sweep.
#
# Self-elevates. Run: powershell -File ops\set_task_catchup.ps1

$ErrorActionPreference = "Stop"

$tasks = @(
    "VoiceOS Earnings Watch"
    "VoiceOS Earnings Watch Notify"
    "VoiceOS Risk Watch"
    "VoiceOS Valuation Watch"
    "VoiceOS Milk Watch"
    "VoiceOS NVIDIA Daily"
    "VoiceOS Price Watch"
    "VoiceOS Log Prune"
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
    $t.Settings.StartWhenAvailable = $true
    Set-ScheduledTask -InputObject $t | Out-Null
    Write-Host "$name : set"
}

Write-Host ""
Get-ScheduledTask | Where-Object { $_.TaskName -like "VoiceOS*" } |
    ForEach-Object { [PSCustomObject]@{ Name = $_.TaskName; StartWhenAvailable = $_.Settings.StartWhenAvailable } } |
    Sort-Object Name | Format-Table -AutoSize
