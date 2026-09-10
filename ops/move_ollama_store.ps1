# Point the Ollama service's model store at a new path (default: D:\ai\ollama\models).
#
# Why: the store defaulted to C:\Users\timlo\.ollama\models and C: was 93% full.
# Ollama runs as LocalSystem, so OLLAMA_MODELS lives in the NSSM service's
# AppEnvironmentExtra (REG_MULTI_SZ) rather than a user env var. Set it with
# Set-ItemProperty, not `nssm set` — nssm's quoting mangles values.
#
# Run elevated. Copy the store to $NewPath FIRST; this script only switches and
# restarts, it does not move data and does not delete the old store.

param(
    [string]$NewPath = "D:\ai\ollama\models"
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this in an elevated PowerShell (Run as Administrator)."
    exit 1
}

if (-not (Test-Path (Join-Path $NewPath "manifests"))) {
    Write-Error "$NewPath does not look like an Ollama store (no manifests\ inside). Copy the store there first."
    exit 1
}

$key = "HKLM:\SYSTEM\CurrentControlSet\Services\Ollama\Parameters"
$current = @((Get-ItemProperty -Path $key -Name AppEnvironmentExtra).AppEnvironmentExtra)
$oldLine = $current | Where-Object { $_ -like "OLLAMA_MODELS=*" }
Write-Output "current: $oldLine"

# VoiceASR depends on Ollama, so it stops first and starts last.
Write-Output "stopping VoiceASR, Ollama..."
Stop-Service VoiceASR -Force
Stop-Service Ollama -Force

$updated = @($current | Where-Object { $_ -and $_ -notlike "OLLAMA_MODELS=*" })
$updated += "OLLAMA_MODELS=$NewPath"
Set-ItemProperty -Path $key -Name AppEnvironmentExtra -Value ([string[]]$updated) -Type MultiString
Write-Output "set:     OLLAMA_MODELS=$NewPath"

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
    Write-Error "Ollama did not answer on 11434 after 60s. Check D:\ai\voice-ecosystem\logs\ollama.log. To roll back: set AppEnvironmentExtra back to '$oldLine' and restart Ollama."
    exit 1
}

Write-Output "starting VoiceASR..."
Start-Service VoiceASR

Write-Output ""
Write-Output "--- verify ---"
(Get-ItemProperty -Path $key -Name AppEnvironmentExtra).AppEnvironmentExtra
Get-Service Ollama, VoiceASR | Format-Table Name, Status -AutoSize
(Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10).models | Select-Object name
Write-Output "old store left in place; delete it only after checking the models above."
