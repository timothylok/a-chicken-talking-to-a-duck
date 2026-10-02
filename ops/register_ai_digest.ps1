# Registers the "VoiceOS AI Digest" scheduled task. Run from an ELEVATED
# PowerShell. Builds the daily AI news / hiccups digest at 07:30 NZT
# as the logged-in user (ops/ai_digest.py -- holds the Workers AI
# credentials from the repo-root .env, so never the VoiceASR service) and
# republishes the dashboard app with it. Same principal/settings convention
# as ops/register_flightwatch.ps1 (S4U, Limited, Hidden, StartWhenAvailable).

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$name = "VoiceOS AI Digest"
$pyw  = "D:\ai\voice-ecosystem\asr\.venv\Scripts\pythonw.exe"
$script = "D:\ai\voice-ecosystem\ops\ai_digest.py"

$action = New-ScheduledTaskAction -Execute $pyw -Argument $script -WorkingDirectory "D:\ai\voice-ecosystem"
$trigger = New-ScheduledTaskTrigger -Daily -At "07:30"
# -At writes StartBoundary with a fixed UTC offset, which shifts the run an hour
# at each NZ DST change; offset-free means local wall-clock time (see
# ops/set_task_localtime.ps1).
$trigger.StartBoundary = (Get-Date -Hour 7 -Minute 30 -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")
$principal = New-ScheduledTaskPrincipal -UserId "timlo" -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet
$settings.Hidden = $true
$settings.StartWhenAvailable = $true
$settings.ExecutionTimeLimit = "PT1H"
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
try { $settings.MultipleInstances = "IgnoreNew" } catch {}

Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null

$t = Get-ScheduledTask -TaskName $name
$i = $t | Get-ScheduledTaskInfo
"{0}: logon={1} state={2} next={3}" -f $t.TaskName, $t.Principal.LogonType, $t.State, $i.NextRunTime
