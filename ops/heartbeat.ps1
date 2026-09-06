# Heartbeat: pings healthchecks.io every run if the voice stack is healthy.
# Healthy = local ASR /health responds ok, the Cloudflared service is running,
# AND the ASR /health route is reachable through the public Cloudflare tunnel
# (the process being up doesn't prove the tunnel's edge connections are alive -
# a ~5h silent tunnel outage on 2026-08-01 and an 11-min one on 2026-09-07 both
# passed the old process-only check).
# On failure, pings the /fail endpoint with a reason for an immediate alert.
# Registered as a scheduled task (see SESSIONS.md 2026-07-12).
param([string]$PingUrl = $env:HEALTHCHECKS_PING_URL)

if (-not $PingUrl) {
    Write-Error "PingUrl not set (pass -PingUrl or set HEALTHCHECKS_PING_URL env var)"
    exit 1
}

# Full public-path check: DNS -> Cloudflare edge -> tunnel -> cloudflared -> local ASR.
# CF Access service token is read from gateway/.env (its canonical home) so the
# secret isn't duplicated into a second location.
function Test-Tunnel {
    param([hashtable]$Headers)
    try {
        $r = Invoke-WebRequest -Uri "https://voice.fittertrack.com/health" -Headers $Headers `
            -TimeoutSec 15 -UseBasicParsing
        $body = $null
        try { $body = $r.Content | ConvertFrom-Json } catch {}
        if ($null -eq $body) { return "tunnel returned non-JSON (CF Access challenge?)" }
        if ($body.status -ne "ok") { return "tunnel reached, asr status: $($body.status)" }
        return $null
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $code = [int]$resp.StatusCode
            if ($code -eq 401 -or $code -eq 403) { return "tunnel access denied ($code) - CF token expired?" }
            if ($code -ge 502 -and $code -le 504) { return "tunnel up, ASR upstream $code" }
            return "tunnel HTTP $code"
        }
        return "tunnel unreachable: $($_.Exception.Message)"
    }
}

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

$cfId = $null; $cfSecret = $null
$envPath = Join-Path $PSScriptRoot "..\gateway\.env"
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*CF_ACCESS_CLIENT_ID\s*=\s*(.+?)\s*$')     { $cfId = $Matches[1] }
        if ($line -match '^\s*CF_ACCESS_CLIENT_SECRET\s*=\s*(.+?)\s*$') { $cfSecret = $Matches[1] }
    }
}
if ($cfId -and $cfSecret) {
    $headers = @{ "CF-Access-Client-Id" = $cfId; "CF-Access-Client-Secret" = $cfSecret }
    $t = Test-Tunnel -Headers $headers
    if ($t) {
        Start-Sleep -Seconds 5
        $t = Test-Tunnel -Headers $headers  # one retry to ride out brief edge reconnects
        if ($t) { $reason += $t }
    }
} else {
    $reason += "tunnel check skipped: CF Access creds not found in gateway/.env"
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
