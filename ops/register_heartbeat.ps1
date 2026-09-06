# Registers the "VoiceOS Heartbeat" scheduled task. Run from an ELEVATED
# PowerShell. Runs ops/heartbeat.ps1 hourly as the logged-in user (S4U, so it
# fires whether or not anyone is logged on) to ping healthchecks.io while the
# voice stack — local ASR, the Cloudflared service, AND the ASR /health route
# through the public tunnel — is healthy; pings /fail with a reason otherwise.
#
# Battery / missed-schedule hardening (SESSIONS.md 2026-09-07): the task must
# still run on battery and must catch up a slot missed while the box was
# unavailable — otherwise an outage during that window goes unalerted, which is
# exactly when the alert matters. Hence AllowStartIfOnBatteries +
# StartWhenAvailable. WakeToRun is deliberately left off: a sleeping box isn't
# serving anything, so there's nothing to monitor until it wakes.
#
# Settings are assigned as properties on the object rather than passed as
# New-ScheduledTaskSettingsSet parameters, because that cmdlet's parameter set
# varies by Windows build (this box rejects -MultipleInstancesPolicy and
# -DisallowStartIfOnBatteries); the CIM properties are stable across builds.
# HEALTHCHECKS_PING_URL is read by heartbeat.ps1 from the User env var, not
# passed here, to keep the ping URL out of the task's visible action string.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}

$name   = "VoiceOS Heartbeat"
$script = "D:\ai\voice-ecosystem\ops\heartbeat.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory "D:\ai\voice-ecosystem"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId "timlo" -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet
$settings.Hidden = $true
$settings.StartWhenAvailable = $true
$settings.ExecutionTimeLimit = "PT72H"
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
