# Wait for the OneBot port (3001) to come up after a QR login, then hand control
# back to the normal supervisor (scheduled task QQRemote -> start-all.ps1).
#
# Why this exists: start-all.ps1 restarts QQ whenever 3001 is missing for 3 checks
# (~90s). While someone is trying to scan a login QR that is exactly the wrong thing
# to do -- it kills the QR window every minute and a half. So during login we keep
# the supervisor OFF and run only this dumb wait loop, which never touches QQ.
#
# ASCII only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as GBK and chokes on
# Chinese comments (MissingEndCurlyBrace).

param(
    [int]$TimeoutMinutes = 240,
    [int]$Port = 3001
)

$ErrorActionPreference = "SilentlyContinue"
$Log = "C:\Users\win11\qq-remote\logs\restore-on-login.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Test-Port($p) {
    return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

Write-Log "watching port $Port (timeout ${TimeoutMinutes}min)"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)

while ((Get-Date) -lt $deadline) {
    if (Test-Port $Port) {
        Write-Log "port $Port is up - login detected"
        Start-Sleep -Seconds 8      # let NapCat settle before the supervisor probes
        $out = schtasks /Run /TN QQRemote 2>&1
        Write-Log ("schtasks /Run QQRemote -> " + ($out -join " "))
        Start-Sleep -Seconds 20
        $bridge = Test-Port 3099
        Write-Log ("bridge dashboard 3099 listening = " + $bridge)
        if (-not $bridge) {
            Write-Log "supervisor did not bring bridge up; leaving it to the next check"
        }
        Write-Log "done"
        exit 0
    }
    Start-Sleep -Seconds 5
}

Write-Log "timeout: port $Port never came up in ${TimeoutMinutes} min"
exit 1
