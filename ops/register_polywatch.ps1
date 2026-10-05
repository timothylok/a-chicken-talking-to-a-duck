# Registers the "VoiceOS Polymarket Watch" scheduled task. Run from an ELEVATED
# PowerShell. Checks Mag 7 Polymarket odds once a day at 11:45 NZT as the
# logged-in user (ops/polywatch.py, ~15 s, no LLM) -- after the US close in
# both DST regimes and clear of the 10:00-11:30 stock-report window. Same
# principal/settings convention as ops/register_flightwatch.ps1 (S4U, Limited,
# Hidden, StartWhenAvailable so a sleeping laptop catches up on wake).

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$name = "VoiceOS Polymarket Watch"
$pyw  = "D:\ai\voice-ecosystem\asr\.venv\Scripts\pythonw.exe"
$script = "D:\ai\voice-ecosystem\ops\polywatch.py"

$action = New-ScheduledTaskAction -Execute $pyw -Argument $script -WorkingDirectory "D:\ai\voice-ecosystem"
$trigger = New-ScheduledTaskTrigger -Daily -At "11:45"
# -At writes StartBoundary with a fixed UTC offset, which shifts the run an hour
# at each NZ DST change; offset-free means local wall-clock time (see
# ops/set_task_localtime.ps1).
$trigger.StartBoundary = (Get-Date -Hour 11 -Minute 45 -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")
$principal = New-ScheduledTaskPrincipal -UserId "timlo" -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet
$settings.Hidden = $true
$settings.StartWhenAvailable = $true
$settings.ExecutionTimeLimit = "PT15M"
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
try { $settings.MultipleInstances = "IgnoreNew" } catch {}

Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null

$t = Get-ScheduledTask -TaskName $name
$i = $t | Get-ScheduledTaskInfo
"{0}: logon={1} state={2} next={3}" -f $t.TaskName, $t.Principal.LogonType, $t.State, $i.NextRunTime
