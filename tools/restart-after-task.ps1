param(
    [Parameter(Mandatory = $true)][int]$WaitPid,
    [int]$QuietSeconds = 120,
    [int]$TimeoutMinutes = 60
)

# Restart the bridge once it is quiet, so freshly edited code goes live.
#
# Trigger: the given claude.exe task process exits.
# Condition: no task has been running under the bridge for $QuietSeconds, so a
#            burst of quick follow-up messages does not keep pushing the restart
#            back forever and we never kill a running task.
# Fallback: if the supervisor does not relaunch within 45 s, start it here.
#
# Log: logs\restart-after-task.txt
# ASCII only: this file is stored without a BOM, Chinese would be read as GBK.

$Root   = Split-Path -Parent $PSScriptRoot
$Log    = Join-Path $Root "logs\restart-after-task.txt"
$Python = "C:\Users\win11\AppData\Local\Programs\Python\Python313\python.exe"
$Bridge = Join-Path $Root "bridge\bridge.py"

function Say([string]$msg) {
    Add-Content -Path $Log -Encoding UTF8 -Value (
        "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg)
}

function Get-Bridge {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like "*bridge\bridge.py*" })
}

function Get-Busy($bridges) {
    if ($bridges.Count -eq 0) { return @() }
    @(Get-CimInstance Win32_Process -Filter "Name='claude.exe'" |
      Where-Object { $bridges.ProcessId -contains $_.ParentProcessId })
}

Say ("waiting for pid {0}" -f $WaitPid)
$deadline = (Get-Date).AddMinutes(15)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 3
}
Say ("task process gone, waiting for {0}s of quiet" -f $QuietSeconds)

$giveUp  = (Get-Date).AddMinutes($TimeoutMinutes)
$lastBusy = Get-Date
$restarted = $false
while ((Get-Date) -lt $giveUp) {
    $bridges = Get-Bridge
    if ($bridges.Count -eq 0) {
        Say "no bridge running, nothing to restart"
        exit 0
    }
    $busy = Get-Busy $bridges
    if ($busy.Count -gt 0) {
        $lastBusy = Get-Date
        Start-Sleep -Seconds 5
        continue
    }
    if (((Get-Date) - $lastBusy).TotalSeconds -lt $QuietSeconds) {
        Start-Sleep -Seconds 5
        continue
    }
    # Last look right before killing: a new task may have just arrived.
    $bridges = Get-Bridge
    if ((Get-Busy $bridges).Count -gt 0) { $lastBusy = Get-Date; continue }
    Say ("stopping bridge pid {0}" -f ($bridges.ProcessId -join ","))
    $bridges | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $restarted = $true
    break
}

if (-not $restarted) {
    Say "gave up: bridge never quiet within timeout"
    exit 0
}

# The supervisor polls every 30 s, so give it a bit. WMI sometimes misses the
# new process right after start, so also treat "3099 is listening" as alive.
Start-Sleep -Seconds 45
$again = Get-Bridge
if ($again.Count -eq 0) {
    $alive = @(Get-NetTCPConnection -State Listen -LocalPort 3099 -ErrorAction SilentlyContinue)
    if ($alive.Count -gt 0) {
        Say "bridge is listening on 3099 (process query missed it), ok"
        exit 0
    }
    Say "supervisor did not relaunch, starting bridge manually"
    Start-Process -FilePath $Python -ArgumentList $Bridge `
        -WorkingDirectory (Join-Path $Root "bridge") -WindowStyle Hidden
    Start-Sleep -Seconds 6
    $again = Get-Bridge
}
if ($again.Count -gt 0) {
    Say ("bridge back, pid {0}" -f ($again.ProcessId -join ","))
} else {
    Say "RESTART FAILED: no bridge process"
}
