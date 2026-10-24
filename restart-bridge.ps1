# 唯一正确的重启入口：改完 bridge.py 后跑这个
# 用法：powershell -ExecutionPolicy Bypass -File restart-bridge.ps1 [-Force]
# 默认会拒绝在任务执行中重启；-Force 跳过该检查（仅供确认任务已结束时使用）。
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

# 1) 有任务在飞就拒绝
$cur = ""
if (Test-Path $State) {
    try { $cur = (Get-Content $State -Raw -Encoding UTF8 | ConvertFrom-Json).current_task } catch {}
}
if ($cur -and -not $Force) {
    Write-Restart "拒绝重启：任务在飞（$cur）。等它结束，或加 -Force"
    exit 1
}

# 2) 找 bridge 进程
$bridges = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*bridge\bridge.py*" })
if ($bridges.Count -eq 0) {
    Write-Restart "没有在跑的 bridge，直接拉起计划任务"
    schtasks /Run /TN QQRemote | Out-Null
    exit 0
}

# 3) 先杀 claude 子进程，再杀 bridge（避免孤儿任务）
foreach ($b in $bridges) {
    $kids = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $b.ProcessId })
    foreach ($k in $kids) {
        try { Stop-Process -Id $k.ProcessId -Force -ErrorAction Stop; Write-Restart "杀掉子进程 $($k.Name) $($k.ProcessId)" } catch {}
    }
    try { Stop-Process -Id $b.ProcessId -Force -ErrorAction Stop; Write-Restart "杀掉 bridge $($b.ProcessId)" }
    catch { Write-Restart "杀 bridge 失败：$($_.Exception.Message)" }
}

# 4) 等守护拉起新进程（最多 45 秒）
$newPid = $null
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    $p = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*bridge\bridge.py*" })
    if ($p.Count -gt 0) { $newPid = $p[0].ProcessId; break }
}
if (-not $newPid) {
    Write-Restart "45 秒内没有新 bridge，手动拉起计划任务"
    schtasks /Run /TN QQRemote | Out-Null
    Start-Sleep -Seconds 8
    $p = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*bridge\bridge.py*" })
    if ($p.Count -gt 0) { $newPid = $p[0].ProcessId }
}

# 5) 验证新代码真的生效：进程启动时间必须晚于 bridge.py 修改时间
if ($newPid) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$newPid"
    $fileTime = (Get-Item (Join-Path $Root "bridge\bridge.py")).LastWriteTime
    Write-Restart "新 bridge PID $newPid 启动于 $($proc.CreationDate)"
    if ($proc.CreationDate -gt $fileTime) { Write-Restart "OK：进程启动时间晚于 bridge.py 修改时间" }
    else { Write-Restart "警告：进程可能还是旧代码" }
} else {
    Write-Restart "启动失败，请跑 自检.ps1"
    exit 1
}
