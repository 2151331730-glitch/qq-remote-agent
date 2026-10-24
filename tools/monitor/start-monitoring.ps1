# 启动 Prometheus + Grafana，看图：http://127.0.0.1:3000/d/qq-remote
# 前置：bridge 的看板在 127.0.0.1:3099（bridge 启动时自动带 /metrics）
$ErrorActionPreference = "Continue"
$m = "C:\Users\win11\qq-remote\tools\monitor"

if (-not (Get-Process prometheus -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$m\prometheus-3.15.1.windows-amd64\prometheus.exe" `
        -ArgumentList "--config.file=$m\prometheus.yml", "--storage.tsdb.path=$m\data",
                      "--web.listen-address=127.0.0.1:9090", "--web.enable-lifecycle" `
        -WorkingDirectory "$m\prometheus-3.15.1.windows-amd64" -WindowStyle Hidden
    Write-Host "prometheus 已启动 http://127.0.0.1:9090"
}

if (-not (Get-Process grafana -ErrorAction SilentlyContinue)) {
    $g = "$m\grafana-13.2.4"
    Start-Process -FilePath "$g\bin\grafana.exe" -ArgumentList "server", "--homepath", "$g" `
        -WorkingDirectory $g -WindowStyle Hidden -Environment @{
            GF_SECURITY_ADMIN_PASSWORD = "admin"
            GF_AUTH_ANONYMOUS_ENABLED  = "true"
            GF_AUTH_ANONYMOUS_ORG_ROLE = "Admin"
            GF_SERVER_HTTP_PORT        = "3000"
        }
    Write-Host "grafana 已启动 http://127.0.0.1:3000/d/qq-remote"
}

Write-Host "`n停止：Get-Process prometheus,grafana | Stop-Process -Force"
