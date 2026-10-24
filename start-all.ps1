# QQ 远程任务 - 总控
# 作用：守着 NapCat（QQ）和 bridge.py，谁挂了就拉起来。
# 用法：powershell -ExecutionPolicy Bypass -File start-all.ps1
#      加 -Once 只检查一次不循环（调试用）

param([switch]$Once)

$ErrorActionPreference = "SilentlyContinue"
$Root      = "C:\Users\win11\qq-remote"
$NapCatDir = Join-Path $Root "napcat"
$BridgeDir = Join-Path $Root "bridge"
$LogDir    = Join-Path $Root "logs"
$Log       = Join-Path $LogDir "supervisor.log"
$Python    = "C:\Users\win11\AppData\Local\Programs\Python\Python313\python.exe"

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Test-ProcessRunning($pattern) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
             Where-Object { $_.CommandLine -like "*$pattern*" }
    return [bool]$procs
}

function Start-Bridge {
    Write-Log "启动 bridge"
    $args = @($Python, (Join-Path $BridgeDir "bridge.py"))
    Start-Process -FilePath $Python -ArgumentList (Join-Path $BridgeDir "bridge.py") `
        -WorkingDirectory $BridgeDir -WindowStyle Hidden
}

function Start-NapCat {
    Write-Log "启动 NapCat（QQ）"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$NapCatDir\launcher.bat`"" `
        -WorkingDirectory $NapCatDir -WindowStyle Minimized
}

while ($true) {
    # NapCat / QQ 是否活着
    if (-not (Get-Process QQ -ErrorAction SilentlyContinue)) {
        Write-Log "QQ 未运行"
        Start-NapCat
        Start-Sleep -Seconds 20
    }

    # bridge 是否活着
    if (-not (Test-ProcessRunning "bridge.py")) {
        Start-Bridge
        Start-Sleep -Seconds 3
    }

    if ($Once) { break }
    Start-Sleep -Seconds 30
}
