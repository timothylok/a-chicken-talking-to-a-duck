# Heartbeat: pings healthchecks.io every run if the voice stack is healthy.
# Healthy = ASR /health responds ok AND the Cloudflared service is running.
# On failure, pings the /fail endpoint with a reason for an immediate alert.
# Registered as a scheduled task every 5 minutes (see SESSIONS.md 2026-07-12).
param([Parameter(Mandatory = $true)][string]$PingUrl)

$reason = @()
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:9000/health" -TimeoutSec 10
    if ($h.status -ne "ok") { $reason += "asr status: $($h.status)" }
} catch {
    $reason += "asr health unreachable"
}
$cf = Get-Service Cloudflared -ErrorAction SilentlyContinue
if (-not $cf -or $cf.Status -ne "Running") {
    $reason += "cloudflared service: $(if ($cf) { $cf.Status } else { 'missing' })"
}

try {
    if ($reason.Count -eq 0) {
        Invoke-RestMethod -Uri $PingUrl -TimeoutSec 10 | Out-Null
    } else {
        Invoke-RestMethod -Uri "$PingUrl/fail" -Method Post -Body ($reason -join "; ") -TimeoutSec 10 | Out-Null
    }
} catch {
    # healthchecks.io unreachable — nothing to do; missed pings alert by themselves
}
