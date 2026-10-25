# 出门前跑一下：powershell -ExecutionPolicy Bypass -File 自检.ps1
$Root = "C:\Users\win11\qq-remote"

Write-Host "=== QQ 远程任务自检 ===" -ForegroundColor Cyan

$qq = Get-Process QQ -ErrorAction SilentlyContinue
if ($qq) { Write-Host "[OK] QQ 进程在跑 (PID $($qq.Id -join ','))" } else { Write-Host "[X] QQ 没运行 —— NapCat 没起来" -ForegroundColor Red }

$port = Get-NetTCPConnection -LocalPort 3001 -State Listen -ErrorAction SilentlyContinue
if ($port) { Write-Host "[OK] OneBot WS 在监听 127.0.0.1:3001" } else { Write-Host "[X] 3001 没监听 —— 可能还没登录" -ForegroundColor Red }

# 桥是管理员进程，非管理员读不到它的命令行，所以改看"有没有人连上 3001"
$conn = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemotePort -eq 3001 }
if ($conn) { Write-Host "[OK] 桥已连上 OneBot (PID $($conn.OwningProcess -join ','))" } else { Write-Host "[X] 桥没连上 —— 可能没启动或刚断线" -ForegroundColor Red }

$task = Get-ScheduledTask -TaskName QQRemote -ErrorAction SilentlyContinue
if ($task) { Write-Host "[OK] 计划任务已注册（$($task.State)）" } else { Write-Host "[!] 计划任务未注册：跑 install-task.ps1" -ForegroundColor Yellow }

$power = powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null | Select-String "当前交流"
if ($power -match "0x00000000") { Write-Host "[OK] 插电时不会睡眠" } else { Write-Host "[!] 插电仍会睡眠：$power" -ForegroundColor Yellow }

$battery = (Get-CimInstance Win32_Battery).BatteryStatus
if ($battery -eq 2) { Write-Host "[OK] 正在用交流电" } else { Write-Host "[!] 当前是电池供电，睡眠定时 3 分钟" -ForegroundColor Yellow }

# 语音识别服务（127.0.0.1:18791）：起不来只是"语音转不了文字"，不影响打字
try {
    $asr = Invoke-RestMethod -Uri "http://127.0.0.1:18791/health" -TimeoutSec 3
    if ($asr.loaded) { Write-Host "[OK] 语音识别服务就绪（模型 $($asr.model) 已加载）" }
    else { Write-Host "[!] 语音识别服务在跑，模型还没加载完" -ForegroundColor Yellow }
} catch {
    Write-Host "[!] 语音识别服务没起 —— 发语音不会转文字，打字照常" -ForegroundColor Yellow
}

$stickers = @(Get-ChildItem "$Root\bridge\stickers" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -match '^\.(gif|png|jpe?g|webp)$' })
if ($stickers.Count -gt 0) { Write-Host "[OK] 表情库 $($stickers.Count) 张" } else { Write-Host "[!] 表情库是空的 —— 想让它发表情，往 bridge\stickers 丢图" -ForegroundColor Yellow }

Write-Host "`n=== 最近 6 行日志 ===" -ForegroundColor Cyan
Get-Content "$Root\logs\bridge.log" -Tail 6 -Encoding UTF8 -ErrorAction SilentlyContinue
