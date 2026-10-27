# Quick login attempt: NapCat -q <uin> uses the cached login token, no QR scan.
# ASCII only. Run elevated.
$ErrorActionPreference = "SilentlyContinue"
$Nap = "C:\Users\win11\qq-remote\napcat"
$Out = Join-Path $env:TEMP "napcat-quick.txt"

function Say($m) {
    Add-Content -Path $Out -Value ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m) -Encoding ASCII
}
Set-Content -Path $Out -Value "" -Encoding ASCII

Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process NapCatWinBootMain -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
Remove-Item (Join-Path $Nap "cache\qrcode.png") -Force
Say "old QQ killed"

Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$Nap\launcher.bat`" -q 3089461079" -WorkingDirectory $Nap -WindowStyle Minimized
Say "launcher started with -q"

for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    if (Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue) {
        Say ("3001 listening after " + (($i + 1) * 5) + "s -> quick login OK")
        exit 0
    }
}
Say "3001 not listening after 120s"
if (Test-Path (Join-Path $Nap "cache\qrcode.png")) {
    Say "qrcode regenerated (fallback to scan)"
} else {
    Say "no qrcode either"
}
