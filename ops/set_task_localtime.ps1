# Pin every daily "VoiceOS *" trigger to its current NZ wall-clock time, so a
# DST change no longer moves it.
#
# Why: New-ScheduledTaskTrigger -At writes StartBoundary with a UTC offset
# ("...T11:10:00+13:00"), which turns on "Synchronize across time zones" --
# the task then fires at a fixed UTC instant, and on 2026-09-27 (NZDT start)
# 14 tasks moved an hour later on the clock. Milk Watch / NVIDIA Daily had no
# offset and stayed put. Stripping the offset makes a trigger local-time.
#
# Locks the times they run at NOW (NZDT), not the pre-DST ones: the stock
# reports key off the US close, which lands at 08:00-10:00 NZ time depending
# on both countries' DST. Technicals at 10:30 / dashboard 11:10 / day range
# 11:25 stay after the close all year; the old 09:30 would run before it
# Nov-Mar. These are also the times on the home page.
#
# Only daily triggers are touched -- the repeating ones (heartbeat, sync,
# every-minute tasks) fire on an interval where the offset doesn't matter.
# Idempotent: a trigger without an offset is skipped. Rerunning
# ops/set_task_stagger.ps1 reintroduces offsets; run this again after it.
#
# Self-elevates. Run: powershell -File ops\set_task_localtime.ps1

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated -- relaunching as Administrator..."
    Start-Process powershell -Verb RunAs -ArgumentList @(
        "-NoProfile", "-NoExit", "-File", $PSCommandPath
    )
    return
}

$offsetNow = [TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now)

foreach ($t in Get-ScheduledTask -TaskName "VoiceOS *") {
    $changed = $false
    foreach ($trig in $t.Triggers) {
        if ($trig.CimClass.CimClassName -ne "MSFT_TaskDailyTrigger") { continue }
        if ($trig.StartBoundary -notmatch '(Z|[+-]\d\d:\d\d)$') { continue }
        # The wall-clock time it fires at today, with the offset dropped.
        $local = [DateTimeOffset]::Parse($trig.StartBoundary).ToOffset($offsetNow)
        $new = $local.ToString("yyyy-MM-ddTHH:mm:ss")
        Write-Host ("{0,-30} {1} -> {2}" -f $t.TaskName, $trig.StartBoundary, $new)
        $trig.StartBoundary = $new
        $changed = $true
    }
    if ($changed) { Set-ScheduledTask -InputObject $t | Out-Null }
}

Write-Host ""
Get-ScheduledTask -TaskName "VoiceOS *" | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    foreach ($trig in $_.Triggers) {
        if ($trig.CimClass.CimClassName -eq "MSFT_TaskDailyTrigger") {
            Write-Host ("{0,-30} {1,-26} next {2:yyyy-MM-dd HH:mm}" -f $_.TaskName, $trig.StartBoundary, $i.NextRunTime)
        }
    }
}
