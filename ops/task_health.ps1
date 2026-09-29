# Health pass over the "VoiceOS *" scheduled tasks for the last N hours
# (default 48): per task, how many runs completed, how many exited non-zero,
# and the current last result / missed-run count; then every non-zero exit and
# launch failure with its time. No elevation needed.
#
# Reads the Task Scheduler event log with wevtutil, not Get-WinEvent: the
# minute-level tasks put ~6k events in 48 h and Get-WinEvent took 150 s+ to
# materialize them on PowerShell 5.1 even with an XPath filter; wevtutil's raw
# XML takes under a second (2026-09-29).
#
# Usage: powershell -File ops\task_health.ps1 [-Hours 48]

param([int]$Hours = 48)

$since = (Get-Date).AddHours(-$Hours).ToUniversalTime().ToString('o')
# 201 = action completed (carries ResultCode); 101/103 = launch failure;
# 329 = stopped by the execution time limit.
$query = "*[System[(EventID=201 or EventID=101 or EventID=103 or EventID=329) and TimeCreated[@SystemTime>='$since']]]"
$raw = (wevtutil qe Microsoft-Windows-TaskScheduler/Operational /q:$query /f:xml) -join ''

$events = foreach ($chunk in $raw -split '</Event>') {
    if ($chunk -notmatch 'Name=.TaskName.>\\(VoiceOS [^<]+)<') { continue }
    $task = $Matches[1]
    [pscustomobject]@{
        Task   = $task
        Id     = [int]([regex]::Match($chunk, '<EventID>(\d+)</EventID>').Groups[1].Value)
        Time   = ([datetime][regex]::Match($chunk, "SystemTime='([^']+)'").Groups[1].Value)
        Result = [regex]::Match($chunk, 'Name=.ResultCode.>(\d+)<').Groups[1].Value
    }
}

$problems = @($events | Where-Object { $_.Id -ne 201 -or $_.Result -ne '0' })

"VoiceOS task health, last $Hours h (since $((Get-Date).AddHours(-$Hours).ToString('yyyy-MM-dd HH:mm')))"
# The log is a fixed-size ring (10 MB by default, ~20 h of this box's minute
# tasks as of 2026-09-29), so a long window can silently reach past its start.
$oldest = [datetime][regex]::Match(((wevtutil qe Microsoft-Windows-TaskScheduler/Operational /c:1 /f:xml) -join ''), "SystemTime='([^']+)'").Groups[1].Value
if ($oldest -gt (Get-Date).AddHours(-$Hours)) {
    "WARNING: event log only reaches back to $($oldest.ToString('yyyy-MM-dd HH:mm')) -- run counts cover less than $Hours h"
}
Get-ScheduledTask -TaskName 'VoiceOS *' | Sort-Object TaskName | ForEach-Object {
    $name = $_.TaskName
    $info = $_ | Get-ScheduledTaskInfo
    $mine = @($events | Where-Object Task -eq $name)
    [pscustomobject]@{
        Task       = $name
        Runs       = @($mine | Where-Object Id -eq 201).Count
        Failures   = @($problems | Where-Object Task -eq $name).Count
        LastRun    = $info.LastRunTime.ToString('MM-dd HH:mm')
        LastResult = '0x{0:X}' -f $info.LastTaskResult
        Missed     = $info.NumberOfMissedRuns
    }
} | Format-Table -AutoSize | Out-String -Width 200

if ($problems) {
    "Failures:"
    $problems | Sort-Object Time | ForEach-Object {
        $what = if ($_.Id -eq 201) { 'exit 0x{0:X}' -f [long]$_.Result } elseif ($_.Id -eq 329) { 'timed out' } else { "launch failed (event $($_.Id))" }
        "  $($_.Time.ToString('yyyy-MM-dd HH:mm'))  $($_.Task)  $what"
    }
} else {
    "No failures."
}
