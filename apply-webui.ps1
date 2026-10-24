# 把 WebUI 监听到本机 + 设置开机自动登录账号（必须在 NapCat 停止时执行，否则会被覆盖）
# 注意：必须写成 UTF-8 无 BOM —— NapCat 解析带 BOM 的 JSON 会静默退回默认配置。
param([string]$ConfigPath = "C:\Users\win11\qq-remote\bridge\config.json")

$p = "C:\Users\win11\qq-remote\napcat\config\webui.json"
$botUin = (Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).bot_uin
$raw = Get-Content $p -Raw -Encoding UTF8
$j = $raw | ConvertFrom-Json
$j.host = "127.0.0.1"
$j.autoLoginAccount = [string]$botUin
$json = $j | ConvertTo-Json -Depth 30
$enc = New-Object System.Text.UTF8Encoding $false   # $false = 不写 BOM
[IO.File]::WriteAllText($p, $json, $enc)
Write-Host "webui.json 已更新: host=$($j.host) autoLoginAccount=$($j.autoLoginAccount)（无 BOM）"
