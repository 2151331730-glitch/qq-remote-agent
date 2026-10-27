# NapCat re-login helper: stop the supervisor loop, relaunch QQ+NapCat once, wait for QR.
# ASCII only. Run elevated.
$ErrorActionPreference = "SilentlyContinue"
$Root  = "C:\Users\win11\qq-remote"
$Nap   = Join-Path $Root "napcat"
$Out   = Join-Path $env:TEMP "napcat-relogin.txt"

function Say($m) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Add-Content -Path $Out -Value $line -Encoding ASCII
}

Set-Content -Path $Out -Value "" -Encoding ASCII
Say ("elevated=" + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))

schtasks /End /TN QQRemote | Out-Null
Say "task QQRemote ended"
Start-Sleep -Seconds 2

Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process NapCatWinBootMain -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
Say "old QQ killed"

Remove-Item (Join-Path $Nap "cache\qrcode.png") -Force
Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$Nap\launcher.bat`"" -WorkingDirectory $Nap -WindowStyle Minimized
Say "launcher started"

for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    if (Test-Path (Join-Path $Nap "cache\qrcode.png")) {
        Say ("qrcode ready after " + (($i + 1) * 5) + "s")
        break
    }
}
if (-not (Test-Path (Join-Path $Nap "cache\qrcode.png"))) { Say "no qrcode within 120s" }

$dst = "C:\Users\win11\Desktop\napcat-login-qr.png"
Copy-Item (Join-Path $Nap "cache\qrcode.png") $dst -Force
Say "qr copied to $dst"
Say ("qq procs=" + (Get-Process QQ -ErrorAction SilentlyContinue).Count)
