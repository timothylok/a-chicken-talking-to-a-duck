# Remove the drive-inherited "Authenticated Users: Modify" from the voice-OS
# trees, so the VoiceASR service account (whose token includes Authenticated
# Users) can read but not modify code or the ops scripts run elevated.
# See SECURITY.md (residual risks). Idempotent.
#
# Run from an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File D:\ai\voice-ecosystem\ops\tighten_acls.ps1
#
# Result per tree: timlo Full (explicit), Administrators/SYSTEM Full (copied),
# BUILTIN\Users RX (copied) -> service reads code but cannot write it.
# Explicit ACEs are untouched: asr\logs + asr\cache stay Modify for the
# service, and the deny ACEs on the secret files stay.

$ErrorActionPreference = "Stop"
$dirs = "D:\ai\voice-ecosystem", "D:\ai\thecolab-skills"

foreach ($dir in $dirs) {
    # freeze the current effective ACEs as explicit, then edit them
    icacls $dir /inheritance:d | Out-Null
    # the user keeps full control (git, scheduled tasks running as timlo)
    icacls $dir /grant "timlo:(OI)(CI)F" | Out-Null
    # drop the blanket Modify everywhere in the tree (/c: skip locked files)
    icacls $dir /remove:g "NT AUTHORITY\Authenticated Users" /t /c | Out-Null
    Write-Host "tightened: $dir"
}

Write-Host "`nrepo root ACL now:"
icacls "D:\ai\voice-ecosystem" | Select-Object -First 8
