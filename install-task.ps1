# 注册开机自启（登录时自动运行总控）
# 需要以管理员身份运行一次：右键 -> 使用 PowerShell 运行，或
#   powershell -ExecutionPolicy Bypass -File install-task.ps1
# 卸载：Unregister-ScheduledTask -TaskName QQRemote -Confirm:$false

$ErrorActionPreference = "Stop"
$Root = "C:\Users\win11\qq-remote"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\start-all.ps1`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "QQRemote" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "已注册计划任务 QQRemote（登录时自动启动，管理员权限）"
Write-Host "手动启动：schtasks /Run /TN QQRemote"
Write-Host "查看状态：Get-ScheduledTask QQRemote | Get-ScheduledTaskInfo"
