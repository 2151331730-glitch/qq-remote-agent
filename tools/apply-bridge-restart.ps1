# 一次性助手：等本次任务结果发完（日志出现「任务结束」）后重启 bridge，
# 让新代码生效；重启由 start-all.ps1 守护自动拉起。
$log = "C:\Users\win11\qq-remote\logs\bridge.log"
$markerTime = Get-Date "2026-10-24 16:41:00"
$deadline = (Get-Date).AddMinutes(15)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    $lines = @(Get-Content $log -Tail 8 -Encoding UTF8)
    $idx = -1
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        if ($lines[$i] -match "^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO 任务结束") {
            $t = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)
            if ($t -gt $markerTime) { $idx = $i; break }
        }
    }
    if ($idx -ge 0) {
        $after = @()
        if ($idx -lt $lines.Count - 1) { $after = $lines[($idx+1)..($lines.Count-1)] }
        if (@($after | Where-Object { $_ -match "任务开始" }).Count -gt 0) { continue }
        Start-Sleep -Seconds 10
        $lines2 = @(Get-Content $log -Tail 4 -Encoding UTF8)
        if (@($lines2 | Where-Object { $_ -match "任务开始" }).Count -gt 0) { continue }
        $proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.CommandLine -like "*bridge\bridge.py*" }
        if ($proc) {
            Add-Content -Path "$env:TEMP\bridge_restart.log" -Value ("{0} restart pid {1}" -f (Get-Date -Format s), ($proc.ProcessId -join ',')) -Encoding UTF8
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
        break
    }
}
