# MoneyTrail 进度报备（PowerShell 兜底）
# 用途：Python 解释器不可用时仍能按 30 分钟节奏落盘，保持「进度可核查」。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File reports\progress_reporter.ps1 -IntervalSeconds 1800
#       powershell -NoProfile -ExecutionPolicy Bypass -File reports\progress_reporter.ps1 -Once
#
# 两条原则（对应 NIGHT_LOG 表三 19:24 那次环境故障）：
# 1) 「Python 能不能用」只能由「真正跑一次 progress_reporter.py」来判定，
#    不能只看 -V 或只看文件在不在 —— 该故障下 -V 正常、跑脚本却挂住。
# 2) 兜底快照不许编数字。Python 真的跑不起来时，台账写「未取数」，
#    并明确引用上一次成功读数，而不是硬编码一份「基线」。

param(
    [int]$IntervalSeconds = 1800,
    [switch]$Once
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$LogPath = Join-Path $PSScriptRoot '进度报备.log'
$LatestPath = Join-Path $PSScriptRoot 'latest.md'
$SnapPath = Join-Path $PSScriptRoot '.snapshot_ps.json'
$PySnapPath = Join-Path $PSScriptRoot '.snapshot.json'
$HealthUrl = 'http://127.0.0.1:8000/health'
$PyProbeSeconds = 45

function Get-Health {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        return "OK $($r.StatusCode)"
    } catch {
        return "不可达（$($_.Exception.GetType().Name)）"
    }
}

function Test-PythonRuns($exe) {
    # PATH 里的 python.exe 很可能是应用商店占位符（跑起来退出码 9009），
    # 所以候选解释器一律先实测一次 --version，能打印版本号才算数（与 run.bat 同规矩）。
    $probe = Join-Path $env:TEMP 'mt_pyver.out'
    Remove-Item $probe -ErrorAction SilentlyContinue
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c `"`"$exe`" -V > `"$probe`" 2>&1`""
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try {
        $p = [Diagnostics.Process]::Start($psi)
    } catch {
        return $false
    }
    if (-not $p.WaitForExit(15000)) {
        try { $p.Kill() } catch { }
        return $false
    }
    if ($p.ExitCode -ne 0) { return $false }
    $text = Get-Content $probe -Raw -ErrorAction SilentlyContinue
    return ($text -match 'Python 3\.')
}

function Find-Python {
    # 候选顺序与 run.bat 保持一致：先 PATH 里的 py/python，再常见安装路径。
    $candidates = @()
    foreach ($cmd in @('py', 'python')) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found -and $found.Source) { $candidates += $found.Source }
    }
    foreach ($version in 314, 313, 312, 311, 310) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe")
    }
    $candidates += 'C:\Python314\python.exe'
    foreach ($c in ($candidates | Select-Object -Unique)) {
        if (-not $c) { continue }
        if (-not (Test-Path $c)) { continue }
        if (Test-PythonRuns $c) { return $c }
    }
    return $null
}

function Invoke-PyReporter {
    <#
      真正跑一次 Python 版报备器。成功（退出码 0）说明解释器可用，
      快照也已由 Python 侧写好（含台账与进度），本脚本直接收工。

      两个本机实测的坑，代码里都已绕开：
      1) Start-Process -WindowStyle Hidden 加 -RedirectStandardOutput 的组合会卡住；
         Wait-Process -Timeout 又不刷新 HasExited，于是「能跑」被误判成「挂死」。
      2) 解释器初始化时 stdout 若由父进程用管道接管，本机会一直卡住；
         交给 cmd 重定向到文件则正常（见 NIGHT_LOG 表七）。
    #>
    $exe = Find-Python
    if (-not $exe) { return @{ ok = $false; reason = '未找到 python.exe' } }
    $reporter = Join-Path $PSScriptRoot 'progress_reporter.py'
    if (-not (Test-Path $reporter)) { return @{ ok = $false; reason = 'progress_reporter.py 缺失' } }

    $out = Join-Path $env:TEMP 'mt_pyreport.out'
    Remove-Item $out -ErrorAction SilentlyContinue
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c `"`"$exe`" -u `"$reporter`" > `"$out`" 2>&1`""
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try {
        $p = [Diagnostics.Process]::Start($psi)
    } catch {
        return @{ ok = $false; reason = "启动失败：$($_.Exception.Message)" }
    }
    if (-not $p.WaitForExit($PyProbeSeconds * 1000)) {
        try { $p.Kill() } catch { }
        return @{ ok = $false; reason = "python 运行 $PyProbeSeconds 秒未返回（环境级挂起）" }
    }
    if ($p.ExitCode -ne 0) {
        $tail = (Get-Content $out -ErrorAction SilentlyContinue | Select-Object -Last 1)
        return @{ ok = $false; reason = "python 退出码 $($p.ExitCode)：$tail" }
    }
    return @{ ok = $true; output = (Get-Content $out -Raw -Encoding UTF8 -ErrorAction SilentlyContinue) }
}

function Get-PreviousReadings {
    # 最近一次带数字的快照（可能是 Python 版写的），用于兜底时说明「上次已知读数」。
    if (-not (Test-Path $LatestPath)) { return @() }
    $lines = Get-Content $LatestPath -ErrorAction SilentlyContinue
    $wanted = $lines | Where-Object { $_ -match '^- (台账|工单状态|进度)：' }
    return @($wanted | Select-Object -First 3)
}

function Get-FileSignatures {
    $watch = @('app.py', 'config.py', 'gen_data.py', 'check.py', 'run.bat', 'README.md',
               'DEMO.md', 'NIGHT_LOG.md', 'core', 'web', 'tests', 'reports')
    $result = @{}
    foreach ($item in $watch) {
        $path = Join-Path $Root $item
        if (-not (Test-Path $path)) { continue }
        if ((Get-Item $path).PSIsContainer) {
            $files = Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '__pycache__' }
            if (-not $files) { continue }
            $result[$item] = [pscustomobject]@{
                count = @($files).Count
                size  = ($files | Measure-Object -Property Length -Sum).Sum
                mtime = ($files | Measure-Object -Property LastWriteTime -Maximum).Maximum.Ticks
            }
        } else {
            $f = Get-Item $path
            $result[$item] = [pscustomobject]@{ count = 1; size = $f.Length; mtime = $f.LastWriteTime.Ticks }
        }
    }
    return $result
}

function Get-NextSeq {
    # Python 版与 PowerShell 版共用同一份追加日志，序号必须共用一套计数。
    $seq = 0
    foreach ($path in @($SnapPath, $PySnapPath)) {
        if (-not (Test-Path $path)) { continue }
        try {
            $value = [int](Get-Content $path -Raw | ConvertFrom-Json).seq
            if ($value -gt $seq) { $seq = $value }
        } catch { }
    }
    return $seq + 1
}

function Write-Snapshot {
    $py = Invoke-PyReporter
    if ($py.ok) {
        # Python 版已经写好了 latest.md 与日志，这里只补一句「走的是哪条路径」。
        Write-Output "Python 报备成功（解释器可用，兜底脚本不重复落盘）"
        return
    }

    $now = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $seq = Get-NextSeq
    $previous = Get-PreviousReadings

    $sig = Get-FileSignatures
    $prev = @{}
    if (Test-Path $SnapPath) {
        try {
            $obj = Get-Content $SnapPath -Raw | ConvertFrom-Json
            foreach ($p in $obj.files.PSObject.Properties) { $prev[$p.Name] = $p.Value }
        } catch { }
    }
    $added = @(); $changed = @(); $removed = @()
    foreach ($k in $sig.Keys) {
        if (-not $prev.ContainsKey($k)) { $added += $k; continue }
        if ($prev[$k].mtime -ne $sig[$k].mtime -or $prev[$k].size -ne $sig[$k].size) { $changed += $k }
    }
    foreach ($k in $prev.Keys) { if (-not $sig.ContainsKey($k)) { $removed += $k } }

    if ($added.Count -or $changed.Count -or $removed.Count) {
        $seg = @()
        if ($added.Count) { $seg += "新增 " + ($added -join '、') }
        if ($changed.Count) { $seg += "变更 " + ($changed -join '、') }
        if ($removed.Count) { $seg += "消失 " + ($removed -join '、') }
        $diff = "- 距上次：" + ($seg -join '；')
    } else {
        $diff = "- 距上次：无文件变化"
    }

    $blockLines = @(
        "## 第 $seq 次报备 · $now（PowerShell 兜底：Python 不可用）",
        "- 服务：127.0.0.1:8000 → $(Get-Health)",
        "- Python：不可用 —— $($py.reason)",
        "- 台账：未取数（需要 Python 读库；本段不填任何推算值）"
    )
    if ($previous.Count) {
        $blockLines += '- 上次成功读数（仅作参照，非本次实测）：'
        $blockLines += @($previous | ForEach-Object { "  $_" })
    }
    $blockLines += $diff
    $blockLines += ''
    $block = $blockLines -join "`n"

    Add-Content -Path $LogPath -Value $block -Encoding UTF8
    Set-Content -Path $LatestPath -Value ("# MoneyTrail 最新进度快照`n`n" + $block) -Encoding UTF8

    $snap = [pscustomobject]@{ at = $now; seq = $seq; files = $sig }
    $snap | ConvertTo-Json -Depth 5 | Set-Content -Path $SnapPath -Encoding UTF8
    Write-Output $block
}

Write-Snapshot
if ($Once) { exit 0 }
while ($true) {
    Start-Sleep -Seconds ([Math]::Max(60, $IntervalSeconds))
    Write-Snapshot
}
