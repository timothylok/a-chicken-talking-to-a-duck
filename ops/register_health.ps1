# Registers the two weight/fasting/workout scheduled tasks. Run from an ELEVATED
# PowerShell:
#   "VoiceOS Health Alerts" -- every minute (ops/health_alerts.py): the 8h
#       stop-eating push, the 10:00 weight/workout prompt, pace nudge, weekly summary.
#   "VoiceOS Health Sync"   -- every 5 min (ops/health_sync.py): mirror to Notion.
# Both run as the logged-in user (S4U, Limited) so the ntfy topic, Discord
# webhook and Notion key stay out of the VoiceASR service. Settings follow
# the sibling register_*.ps1 scripts.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$pyw = "D:\ai\voice-ecosystem\asr\.venv\Scripts\pythonw.exe"

function Register-Health($name, $script, $minutes) {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument $script -WorkingDirectory "D:\ai\voice-ecosystem"
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes $minutes)
    # Offset-free StartBoundary = local wall-clock time (see ops/set_task_localtime.ps1).
    $trigger.StartBoundary = (Get-Date).Date.ToString("yyyy-MM-ddTHH:mm:ss")
    $principal = New-ScheduledTaskPrincipal -UserId "timlo" -LogonType S4U -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet
    $settings.Hidden = $true
    $settings.StartWhenAvailable = $true
    $settings.ExecutionTimeLimit = "PT5M"
    $settings.DisallowStartIfOnBatteries = $false
    $settings.StopIfGoingOnBatteries = $false
    try { $settings.MultipleInstances = "IgnoreNew" } catch {}
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    $t = Get-ScheduledTask -TaskName $name
    $i = $t | Get-ScheduledTaskInfo
    "{0}: logon={1} state={2} next={3}" -f $t.TaskName, $t.Principal.LogonType, $t.State, $i.NextRunTime
}

Register-Health "VoiceOS Health Alerts" "D:\ai\voice-ecosystem\ops\health_alerts.py" 1
Register-Health "VoiceOS Health Sync" "D:\ai\voice-ecosystem\ops\health_sync.py" 5
