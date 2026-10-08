# Copies GRAFANA_* from the gitignored repo-root .env into the Alloy service's
# Environment and points its run arguments at ops/alloy/config.alloy (both live under
# HKLM:\SOFTWARE\GrafanaLabs\Alloy), then restarts it. Run from an ELEVATED PowerShell.
$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as administrator)."
}
$envFile = Join-Path $PSScriptRoot "..\..\.env"
$vars = Get-Content -Encoding UTF8 $envFile | Where-Object { $_ -match '^GRAFANA_\w+=.+' }
if ($vars.Count -ne 5) { throw "Expected 5 filled GRAFANA_* lines in .env, found $($vars.Count)." }
$key = "HKLM:\SOFTWARE\GrafanaLabs\Alloy"
$config = (Resolve-Path (Join-Path $PSScriptRoot "config.alloy")).Path
Set-ItemProperty -Path $key -Name Environment -Type MultiString -Value $vars
Set-ItemProperty -Path $key -Name Arguments -Type MultiString -Value @("run", $config, "--storage.path=C:\ProgramData\GrafanaLabs\Alloy\data")
Restart-Service Alloy
