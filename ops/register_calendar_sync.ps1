# Registers the "VoiceOS Calendar Sync" scheduled task. Run from an ELEVATED
# PowerShell. Fetches the secret Google iCal feed every 15 min as the
# logged-in user and writes asr/cache/calendar.json for the SCHEDULE_TODAY
# voice command. Same principal/settings convention as "VoiceOS Notion Sync"
# (S4U, Limited, Hidden, StartWhenAvailable) — see SESSIONS.md 2026-08-31.
#
# Settings are assigned as properties on the object rather than passed as
# New-ScheduledTaskSettingsSet parameters, because that cmdlet's parameter set
# varies by Windows build (this box rejects -MultipleInstancesPolicy and
# -DisallowStartIfOnBatteries); the CIM properties are stable across builds.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$name = "VoiceOS Calendar Sync"
$pyw  = "D:\ai\voice-ecosystem\asr\.venv\Scripts\pythonw.exe"
$script = "D:\ai\voice-ecosystem\ops\calendar_sync.py"

$action = New-ScheduledTaskAction -Execute $pyw -Argument $script -WorkingDirectory "D:\ai\voice-ecosystem"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
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

Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 8
$t = Get-ScheduledTask -TaskName $name
$i = $t | Get-ScheduledTaskInfo
"{0}: logon={1} state={2} lastResult=0x{3:X}" -f $t.TaskName, $t.Principal.LogonType, $t.State, $i.LastTaskResult
