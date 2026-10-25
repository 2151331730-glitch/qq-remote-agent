# 唯一正确的重启入口：改完 bridge.py 后跑这个
# 用法：powershell -ExecutionPolicy Bypass -File restart-bridge.ps1 [-Force]
# 默认会拒绝在任务执行中重启；-Force 跳过该检查（仅供确认任务已结束时使用）。
#
# 2026-10-25 修复：原来靠 Win32_Process 的 CommandLine 匹配 bridge.py，
# 但管理员进程的命令行普通会话读不到，导致永远误判"没有在跑的 bridge"，
# 于是旧进程一直占着 3099，改了代码也不生效。改为用 3099 端口找进程。
param([switch]$Force)
$ErrorActionPreference = "Stop"
$Root  = "C:\Users\win11\qq-remote"
$State = Join-Path $Root "bridge\state.json"
$Log   = Join-Path $Root "logs\restart.txt"

function Write-Restart($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-BridgePids {
    $c = Get-NetTCPConnection -LocalPort 3099 -State Listen -ErrorAction SilentlyContinue
    if (-not $c) { return @() }
    return @($c | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique)
}

# 1) 有任务在飞就拒绝
$cur = ""
if (Test-Path $State) {
    try { $cur = (Get-Content $State -Raw -Encoding UTF8 | ConvertFrom-Json).current_task } catch {}
}
if ($cur -and -not $Force) {
    Write-Restart "拒绝重启：任务在飞（$cur）。等它结束，或加 -Force"
    exit 1
}

# 2) 用端口找 bridge
$pids = Get-BridgePids
if ($pids.Count -eq 0) {
    Write-Restart "没有在跑的 bridge（3099 无监听），直接拉起计划任务"
    schtasks /Run /TN QQRemote | Out-Null
    exit 0
}

# 3) 先杀 claude 子进程，再杀 bridge；权限不够就提权杀
foreach ($b in $pids) {
    $kids = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $b })
    foreach ($k in $kids) {
        try { Stop-Process -Id $k.ProcessId -Force -ErrorAction Stop; Write-Restart "杀掉子进程 $($k.Name) $($k.ProcessId)" } catch {}
    }
    try {
        Stop-Process -Id $b -Force -ErrorAction Stop
        Write-Restart "杀掉 bridge $b"
    } catch {
        Write-Restart "直接杀失败（$($_.Exception.Message)），改用管理员权限"
        Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
            '-NoProfile','-WindowStyle','Hidden','-Command',"Stop-Process -Id $b -Force"
        )
        Write-Restart "已通过提权杀掉 bridge $b"
    }
}

# 4) 等新进程：端口重新监听且 PID 变化
$newPid = $null
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    $now = Get-BridgePids
    if ($now.Count -gt 0 -and ($now | Where-Object { $pids -notcontains $_ })) {
        $newPid = ($now | Where-Object { $pids -notcontains $_ })[0]; break
    }
}
if (-not $newPid) {
    Write-Restart "45 秒内没有新 bridge，手动拉起计划任务"
    schtasks /Run /TN QQRemote | Out-Null
    Start-Sleep -Seconds 10
    $now = Get-BridgePids
    if ($now.Count -gt 0) { $newPid = $now[0] }
}

# 5) 验证：进程启动时间必须晚于 bridge.py 修改时间
if ($newPid) {
    $p = Get-Process -Id $newPid -ErrorAction SilentlyContinue
    $fileTime = (Get-Item (Join-Path $Root "bridge\bridge.py")).LastWriteTime
    Write-Restart "新 bridge PID $newPid 启动于 $($p.StartTime)"
    if ($p.StartTime -gt $fileTime) { Write-Restart "OK：进程启动时间晚于 bridge.py 修改时间" }
    else { Write-Restart "警告：进程启动时间早于 bridge.py，可能还是旧代码" }
} else {
    Write-Restart "启动失败，请跑 自检.ps1"
    exit 1
}
