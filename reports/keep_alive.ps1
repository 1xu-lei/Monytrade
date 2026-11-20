# MoneyTrail 守护脚本：服务掉线即拉起
# 背景：本会话里的后台任务会被回收（表现为进程 exit_code 0 静默退出、无 traceback），
#       所以用一个独立循环盯着 /health，发现连不上就按原参数重启 app.py。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File reports\keep_alive.ps1 -Port 8090 -IntervalSeconds 60

param(
    [int]$Port = 8090,
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
$HealthUrl = "http://127.0.0.1:$Port/health"
$LogPath = Join-Path $PSScriptRoot 'keepalive.log'
$ServerLog = Join-Path $PSScriptRoot 'server.log'

function Write-Log($message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 4
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-PortBusy {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Write-Log "守护启动：端口 $Port，间隔 $IntervalSeconds 秒，解释器 $Python"

while ($true) {
    if (Test-Health) {
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    if (Test-PortBusy) {
        Write-Log "健康检查失败但端口仍被占用，可能有半死进程，本轮不重启（避免端口冲突）"
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    Write-Log "健康检查失败且端口空闲 → 重启 app.py --port $Port"
    try {
        Start-Process -FilePath $Python `
            -ArgumentList @('app.py', '--port', "$Port") `
            -WorkingDirectory $Root `
            -WindowStyle Hidden `
            -RedirectStandardOutput $ServerLog `
            -RedirectStandardError ($ServerLog + '.err') | Out-Null
    } catch {
        Write-Log ("启动失败：" + $_.Exception.Message)
    }

    Start-Sleep -Seconds 5
    if (Test-Health) {
        Write-Log "重启成功，/health 已恢复"
    } else {
        Write-Log "重启后仍未就绪，下一轮再试"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
