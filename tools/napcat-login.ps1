# NapCat login ladder. ASCII only. Run elevated.
# 1) try cached-token quick login for each candidate uin (no QR needed)
# 2) if all fail, leave a fresh QR on disk for the user to scan
param(
    [string[]]$Uin = @("2813436988", "3089461079"),
    [int]$WaitPerTry = 90
)
$ErrorActionPreference = "SilentlyContinue"
$Nap = "C:\Users\win11\qq-remote\napcat"
$Out = Join-Path $env:TEMP "napcat-login.txt"

function Say($m) {
    Add-Content -Path $Out -Value ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -Encoding ASCII
}
function KillQQ {
    Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-Process NapCatWinBootMain -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 3
}
function Up { (Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue) -ne $null }
function Launch([string]$arg) {
    Remove-Item (Join-Path $Nap "cache\qrcode.png") -Force
    $line = "/c `"$Nap\launcher.bat`""
    if ($arg) { $line = $line + " " + $arg }
    Start-Process -FilePath "cmd.exe" -ArgumentList $line -WorkingDirectory $Nap -WindowStyle Minimized
}

Set-Content -Path $Out -Value "" -Encoding ASCII
Say ("elevated=" + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))

foreach ($u in $Uin) {
    KillQQ
    Launch ("-q " + $u)
    Say ("try -q $u")
    for ($i = 0; $i -lt [int]($WaitPerTry / 5); $i++) {
        Start-Sleep -Seconds 5
        if (Up) {
            Say ("3001 UP after " + (($i + 1) * 5) + "s -> account $u cached login OK")
            exit 0
        }
        if (Test-Path (Join-Path $Nap "cache\qrcode.png")) {
            Say ("no cached token for $u (QR appeared)")
            break
        }
    }
}

# last resort: plain launch, keep the QR alive for scanning
KillQQ
Launch ""
Say "plain launch, waiting for qrcode"
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    if (Up) { Say "3001 UP without scan (unexpected but good)"; exit 0 }
    if (Test-Path (Join-Path $Nap "cache\qrcode.png")) {
        Say ("qrcode ready after " + (($i + 1) * 5) + "s")
        break
    }
}
$qr = Join-Path $Nap "cache\qrcode.png"
if (Test-Path $qr) {
    Copy-Item $qr "C:\Users\win11\Desktop\napcat-login-qr.png" -Force
    Say "qr copied to Desktop\napcat-login-qr.png"
} else {
    Say "no qrcode within 120s"
}
Say ("qq procs=" + ((Get-Process QQ -ErrorAction SilentlyContinue) | Measure-Object).Count)
