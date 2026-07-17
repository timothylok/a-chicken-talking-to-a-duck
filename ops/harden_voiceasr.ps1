# Switch VoiceASR to the low-privilege virtual account NT SERVICE\VoiceASR.
# See SECURITY.md for the rationale and access table.
#
# Run from an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File D:\ai\voice-ecosystem\ops\harden_voiceasr.ps1

$ErrorActionPreference = "Stop"
$svc  = "VoiceASR"
$acct = "NT SERVICE\VoiceASR"
$repo = "D:\ai\voice-ecosystem"

# 1. nssm.exe to a machine-wide path (the WinGet copy is inside the user
#    profile, which the service account cannot read)
$nssmSrc = "C:\Users\timlo\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
New-Item -ItemType Directory -Force "C:\Program Files\nssm" | Out-Null
Copy-Item $nssmSrc "C:\Program Files\nssm\nssm.exe" -Force

Stop-Service $svc -Force

# 2. New binary path + virtual service account (registry write: sc.exe's
#    embedded-quote handling under PowerShell mangles the quoted binPath)
$svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$svc"
Set-ItemProperty $svcKey -Name ImagePath -Value '"C:\Program Files\nssm\nssm.exe"'
Set-ItemProperty $svcKey -Name ObjectName -Value $acct

# 3. Grant "Log on as a service" (sc.exe does not do this for virtual accounts)
$sid = (New-Object System.Security.Principal.NTAccount($acct)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$inf = Join-Path $env:TEMP "voiceasr-rights.inf"
$db  = Join-Path $env:TEMP "voiceasr-rights.sdb"
secedit /export /cfg $inf /areas USER_RIGHTS | Out-Null
$content = Get-Content $inf
$line = $content | Where-Object { $_ -like "SeServiceLogonRight*" }
if (-not $line) {
    $content = $content -replace "\[Privilege Rights\]", "[Privilege Rights]`r`nSeServiceLogonRight = *$sid"
    $changed = $true
} elseif ($line -notmatch [regex]::Escape($sid)) {
    $content = $content | ForEach-Object { if ($_ -like "SeServiceLogonRight*") { "$_,*$sid" } else { $_ } }
    $changed = $true
}
if ($changed) {
    $content | Set-Content $inf -Encoding Unicode
    secedit /configure /db $db /cfg $inf /areas USER_RIGHTS | Out-Null
}
Remove-Item $inf, $db -Force -ErrorAction SilentlyContinue

# 4. Service env: redirect everything that writes to ~ or system temp
#    (the virtual account has no usable home directory)
$envExtra = @(
    "HF_HOME=C:\Users\timlo\.cache\huggingface",
    "FUEL_LOCATION=glenfield auckland",
    "BIN_ADDRESS=12340757627",
    "ASR_MODEL=JackyHoCL/whisper-small-cantonese-yue-english-ct2",
    "GROCER_NZ_CACHE=$repo\asr\cache\grocer-nz",
    "TMP=$repo\asr\cache\tmp",
    "TEMP=$repo\asr\cache\tmp"
)
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$svc\Parameters" -Name AppEnvironmentExtra -Type MultiString -Value $envExtra

# 5. Filesystem ACLs — grants per SECURITY.md table, denies on the secret files
New-Item -ItemType Directory -Force "$repo\asr\cache\tmp" | Out-Null
icacls $repo /grant "${acct}:(OI)(CI)RX" | Out-Null
icacls "$repo\asr\logs"  /grant "${acct}:(OI)(CI)M" | Out-Null
icacls "$repo\asr\cache" /grant "${acct}:(OI)(CI)M" | Out-Null
icacls "D:\ai\thecolab-skills" /grant "${acct}:(OI)(CI)RX" | Out-Null
icacls "C:\Users\timlo\.cache\huggingface" /grant "${acct}:(OI)(CI)M" | Out-Null
icacls "$repo\ops\notion.json" /deny "${acct}:R" | Out-Null
icacls "$repo\ops\ntfy.json"   /deny "${acct}:R" | Out-Null
icacls "$repo\ops\google.json" /deny "${acct}:R" | Out-Null
icacls "$repo\gateway\.env"    /deny "${acct}:R" | Out-Null

# 6. Orphaned grocer cache from the LocalSystem era
Remove-Item -Recurse -Force "C:\Windows\system32\config\systemprofile\.cache\grocer-nz" -ErrorAction SilentlyContinue

# 7. Start and verify
Start-Service $svc
$deadline = (Get-Date).AddSeconds(120)
$h = $null
do {
    Start-Sleep 3
    try { $h = Invoke-RestMethod http://localhost:9000/health } catch { }
} until ($h -or (Get-Date) -gt $deadline)
$owner = (Get-CimInstance Win32_Service -Filter "Name='VoiceASR'").StartName
if ($h) {
    Write-Host "OK: healthy as $owner - model=$($h.model) device=$($h.device)"
} else {
    Write-Warning "not healthy after 120 s (account: $owner) - check asr\logs\service.log"
}
