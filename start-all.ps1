# QQ 远程任务 - 总控
# 守着 NapCat（QQ）和 bridge.py：谁挂了拉起来，谁"活着但不干活"也拉起来。
# 健康判据不是"进程存在"，而是：
#   1) OneBot 的 3001 端口在监听（说明 QQ 已登录且 NapCat 起来了）
#   2) bridge 与 3001 之间有 ESTABLISHED 连接（说明桥真的连上了）
# 用法：powershell -ExecutionPolicy Bypass -File start-all.ps1 [-Once]

param([switch]$Once)

$ErrorActionPreference = "SilentlyContinue"
$Root      = "C:\Users\win11\qq-remote"
$NapCatDir = Join-Path $Root "napcat"
$BridgeDir = Join-Path $Root "bridge"
$LogDir    = Join-Path $Root "logs"
$Log       = Join-Path $LogDir "supervisor.log"
$Python    = "C:\Users\win11\AppData\Local\Programs\Python\Python313\python.exe"

New-Item -ItemType Directory -Force $LogDir | Out-Null
$portFails = 0
$connFails = 0

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Get-BridgePid {
    # bridge 自己的看板端口 3099。监听者的 PID 跨权限可读，是最干净的"桥在不在"信号。
    $c = Get-NetTCPConnection -LocalPort 3099 -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) { return $c.OwningProcess }
    return 0
}

function Test-BridgeRunning {
    # 注意：不能只看"有没有人连着 3001"——tools\qq_mcp.py 也连着 3001，
    # 桥死了它那条连接还在，会把这个判断骗成"桥活着"，于是永远不重启（2026-10-27 踩过）。
    if (Get-BridgePid) { return $true }
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
             Where-Object { $_.CommandLine -like "*bridge.py*" }
    return [bool]$procs
}

function Test-OneBotListening {
    return [bool](Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue)
}

function Test-BridgeConnected {
    # 必须限定"是 bridge 这个进程"连的：qq_mcp 也连着 3001
    $bp = Get-BridgePid
    if (-not $bp) { return $false }
    return [bool](Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
                  Where-Object { $_.RemotePort -eq 3001 -and $_.OwningProcess -eq $bp })
}

function Start-Bridge {
    Write-Log "启动 bridge"
    Start-Process -FilePath $Python -ArgumentList (Join-Path $BridgeDir "bridge.py") `
        -WorkingDirectory $BridgeDir -WindowStyle Hidden
}

function Start-NapCat {
    Write-Log "启动 NapCat（QQ）"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$NapCatDir\launcher.bat`"" `
        -WorkingDirectory $NapCatDir -WindowStyle Minimized
}

while ($true) {
    # ---- 1. QQ 客户端本身 ----
    if (-not (Get-Process QQ -ErrorAction SilentlyContinue)) {
        Write-Log "QQ 未运行"
        Start-NapCat
        Start-Sleep -Seconds 20
        if ($Once) { break }
        continue
    }

    # ---- 2. QQ 在跑，但 OneBot 端口没起来（掉线/未登录/注入失败） ----
    if (-not (Test-OneBotListening)) {
        $portFails++
        Write-Log "3001 未监听（连续 $portFails 次）"
        if ($portFails -ge 3) {
            Write-Log "判定 NapCat 掉线，重启 QQ"
            Get-Process QQ -ErrorAction SilentlyContinue | Stop-Process -Force
            Start-Sleep -Seconds 5
            Start-NapCat
            $portFails = 0
            Start-Sleep -Seconds 25
            if ($Once) { break }
            continue
        }
    } else {
        $portFails = 0
    }

    # ---- 3. 桥进程 ----
    if (-not (Test-BridgeRunning)) {
        Write-Log "bridge 进程不存在"
        Start-Bridge
        Start-Sleep -Seconds 5
    } elseif ((Test-OneBotListening) -and -not (Test-BridgeConnected)) {
        # 桥在跑但没连上：可能是卡死的旧连接
        $connFails++
        Write-Log "bridge 未连上 OneBot（连续 $connFails 次）"
        if ($connFails -ge 3) {
            Write-Log "重启 bridge"
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.CommandLine -like "*bridge.py*" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Start-Sleep -Seconds 3
            Start-Bridge
            $connFails = 0
            Start-Sleep -Seconds 10
        }
    } else {
        $connFails = 0
    }

    if ($Once) { break }
    Start-Sleep -Seconds 30
}
