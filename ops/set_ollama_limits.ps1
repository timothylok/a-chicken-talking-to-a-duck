# Cap Ollama at one loaded model at a time.
#
# Why: the GPU has 4 GiB. On 2026-09-02 10:00:25 llama-server logged
# "model predicted to exceed available memory, evicting" (predicted 5.4 GiB,
# available 2.0 GiB -- a second runner already held the rest) and then wedged:
# it served no request until the machine's 2026-09-07 sleep/resume restarted
# the service. Five days of stock-report narration, and nvidia_daily's digest,
# silently timed out. asr/logs alerting now catches that within a day
# (see sf.alert_if_narration_dead), but this makes the eviction path itself
# unreachable -- a second model can never be co-loaded on a card too small
# for it. Cost: model swaps serialize, so a stock job running while you're
# talking to the voice assistant queues instead of thrashing.
#
# Only MAX_LOADED_MODELS is set. OLLAMA_KEEP_ALIVE was considered and left
# alone: keep-alive resets on every request, so it was never implicated in
# the wedge, and shortening it would only add reloads between jobs.
#
# Ollama runs as a service, so this lives in the NSSM AppEnvironmentExtra
# (REG_MULTI_SZ) -- written with Set-ItemProperty, not `nssm set`, whose
# quoting mangles values. Self-elevates. Run:
#   powershell -File ops\set_ollama_limits.ps1

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated -- relaunching as Administrator..."
    Start-Process powershell -Verb RunAs -ArgumentList @("-NoProfile", "-NoExit", "-File", $PSCommandPath)
    return
}

# Transcript, so a failure in the elevated window survives the window closing.
$logPath = Join-Path (Split-Path $PSScriptRoot -Parent) "asr\logs\set_ollama_limits.log"
Start-Transcript -Path $logPath -Append | Out-Null
trap { Write-Output "FAILED: $_"; Stop-Transcript | Out-Null; break }

$key = "HKLM:\SYSTEM\CurrentControlSet\Services\Ollama\Parameters"
$current = @((Get-ItemProperty -Path $key -Name AppEnvironmentExtra).AppEnvironmentExtra)
Write-Output "current:"
$current | ForEach-Object { Write-Output "  $_" }

# VoiceASR depends on Ollama, so it stops first and starts last.
Write-Output "stopping VoiceASR, Ollama..."
Stop-Service VoiceASR -Force
Stop-Service Ollama -Force

$updated = @($current | Where-Object { $_ -and $_ -notlike "OLLAMA_MAX_LOADED_MODELS=*" })
$updated += "OLLAMA_MAX_LOADED_MODELS=1"
Set-ItemProperty -Path $key -Name AppEnvironmentExtra -Value ([string[]]$updated) -Type MultiString

Write-Output "starting Ollama..."
Start-Service Ollama

# VoiceASR warms up Ollama at startup, so wait for the API before starting it.
$ready = $false
foreach ($i in 1..30) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) {
    Write-Error "Ollama did not answer on 11434 after 60s. Check D:\ai\voice-ecosystem\logs\ollama.log. To roll back: drop the OLLAMA_MAX_LOADED_MODELS line from AppEnvironmentExtra and restart Ollama."
    exit 1
}

Write-Output "starting VoiceASR..."
Start-Service VoiceASR

Write-Output ""
Write-Output "--- verify ---"
(Get-ItemProperty -Path $key -Name AppEnvironmentExtra).AppEnvironmentExtra
Get-Service Ollama, VoiceASR | Format-Table Name, Status -AutoSize
Stop-Transcript | Out-Null
