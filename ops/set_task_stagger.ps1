# Move the Category 6 dashboard and Stock Day Range off the 10:00 slot, so the
# morning briefing is not competing with them for Ollama's single runner.
#
# Why: the iPhone morning-briefing automation posts 早晨 at 10:00:04, and
# MORNING_BRIEFING's news translation loads gemma3:4b. "VoiceOS Risk Dashboard
# Daily" fired at the same minute needing qwen3:8b. With
# OLLAMA_MAX_LOADED_MODELS=1 and OLLAMA_NUM_PARALLEL=1 there is one runner
# slot, so on 2026-09-19 gemma3:4b took it at 10:00:25, never released, and
# Cat 6 lost 9/9 summaries while Day Range lost 9/9 narrations -- both on a
# 120 s client timeout per call.
#
# Why LATER and not earlier: the dashboard takes ~6 min (09-16/17/18 samples:
# 5m29s, 5m52s, 5m20s) and technicals only finishes ~09:46-09:48, so an
# earlier slot is both tight and backwards -- if the dashboard overran it
# would be holding qwen3:8b when the briefing fires, and the starved job
# becomes the user-facing briefing on the phone rather than a background
# report. Background work should yield to the briefing, not the other way
# round.
#
# Why Day Range moves too: it is not independent. ops/stock_day_range.py reads
# dashboard/data/latest.json (DASHBOARD_JSON) and grounds its narrative in
# that day's Cat 6 output, so the chain technicals -> dashboard -> day range
# has to stay in order. Leaving Day Range at 10:15 while the dashboard ran to
# ~10:16 would have it reading YESTERDAY's snapshot.
#
# Resulting morning chain (all NZT):
#   09:30  Technicals        ends ~09:48
#   10:00  briefing (phone)  ends ~10:00:36; gemma3:4b keep-alive 5m, slot free ~10:05:36
#   10:10  Dashboard         4.5 min after the slot frees, ends ~10:16
#   10:25  Day Range         9 min after the dashboard ends, ends ~10:28
#
# Self-elevates. Run: powershell -File ops\set_task_stagger.ps1

$ErrorActionPreference = "Stop"

$schedule = @{
    "VoiceOS Risk Dashboard Daily" = "10:10"
    "VoiceOS Stock Day Range"      = "10:25"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated -- relaunching as Administrator..."
    Start-Process powershell -Verb RunAs -ArgumentList @(
        "-NoProfile", "-NoExit", "-File", $PSCommandPath
    )
    return
}

foreach ($name in $schedule.Keys) {
    $at = $schedule[$name]
    # Rebuild the trigger rather than editing StartBoundary in place: these
    # tasks carry a single daily trigger, and New-ScheduledTaskTrigger keeps
    # the task's own Settings (StartWhenAvailable, WakeToRun) untouched.
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    Set-ScheduledTask -TaskName $name -Trigger $trigger | Out-Null
    Write-Host "$name : $at"
}

Write-Host ""
Write-Host "Morning chain after this change:"
Get-ScheduledTask | Where-Object {
    $_.TaskName -in @("VoiceOS Technicals Daily", "VoiceOS Risk Dashboard Daily", "VoiceOS Stock Day Range")
} | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    [PSCustomObject]@{
        Name             = $_.TaskName
        Start            = ($_.Triggers | Select-Object -First 1).StartBoundary
        NextRun          = $info.NextRunTime
        StartWhenAvail   = $_.Settings.StartWhenAvailable
        WakeToRun        = $_.Settings.WakeToRun
    }
} | Sort-Object NextRun | Format-Table -AutoSize
