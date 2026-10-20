# P1-08: register / unregister a Windows Scheduled Task so the workflow server
# starts at logon, plus size-based log rotation for the captured stdout/stderr.
#
# Why a Scheduled Task and not a real Windows Service:
#   a true service needs a compiled wrapper (pywin32 / NSSM) and would run in
#   session 0 with no desktop -- the browser-opening path and the Tauri shell
#   both assume an interactive session. A logon task keeps the server in the
#   user's session, which is what this product actually needs.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\service.ps1 -Install
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\service.ps1 -Install -StartNow
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\service.ps1 -Uninstall
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\service.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\service.ps1 -RotateLogs
#
# This script never touches TTS_WRITE_ENABLED.

param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$RotateLogs,
    [switch]$StartNow,
    [string]$TaskName = 'AI-Shopkeeper-Workflow',
    [int]$MaxLogMB = 10,
    [int]$KeepLogs = 5
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Initialize-ConsoleUtf8

$repoRoot = Get-RepoRoot
$logsDir = Join-Path $repoRoot 'artifacts\logs'
$stdoutLog = Join-Path $logsDir 'workflow.out.log'
$stderrLog = Join-Path $logsDir 'workflow.err.log'

function Invoke-RotateLogs {
    param([string]$Path, [int]$MaxMB, [int]$Keep)

    if (-not (Test-Path -LiteralPath $Path)) { return }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -lt ($MaxMB * 1MB)) { return }

    # Shift .N -> .N+1 from the oldest down, so we never overwrite a live file.
    #
    # ⚠️ **不删除任何日志**。超出保留份数的旧文件移进 `archive\` 而不是 `Remove-Item`：
    # 日志是排障依据，滚动策略误配（例如 MaxLogMB 填成 1）时删除会**静默丢掉证据**。
    # 移到归档目录既回收了主目录空间，又保留可追溯性。
    $archive = Join-Path (Split-Path -Parent $Path) 'archive'
    New-Item -ItemType Directory -Force -Path $archive | Out-Null

    $oldest = "$Path.$Keep"
    if (Test-Path -LiteralPath $oldest) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Move-Item -LiteralPath $oldest `
            -Destination (Join-Path $archive ((Split-Path -Leaf $oldest) + "." + $stamp)) -Force
    }
    for ($i = $Keep - 1; $i -ge 1; $i--) {
        $src = "$Path.$i"
        if (Test-Path -LiteralPath $src) {
            Move-Item -LiteralPath $src -Destination ("$Path." + ($i + 1)) -Force
        }
    }
    Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
    Write-Zh ("日志已滚动：{0}（保留 {1} 份）" -f (Split-Path -Leaf $Path), $Keep) -Level 'INFO'
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if ($RotateLogs) {
    Invoke-RotateLogs -Path $stdoutLog -MaxMB $MaxLogMB -Keep $KeepLogs
    Invoke-RotateLogs -Path $stderrLog -MaxMB $MaxLogMB -Keep $KeepLogs
    exit 0
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Zh '计划任务未注册。' -Level 'WARN'
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Zh ("计划任务已注册：{0}，状态 {1}" -f $TaskName, $task.State)
        if ($info) { Write-Zh ("上次运行：{0}，结果 {1}" -f $info.LastRunTime, $info.LastTaskResult) }
    }
    foreach ($p in @($stdoutLog, $stderrLog)) {
        if (Test-Path -LiteralPath $p) {
            Write-Zh ("日志 {0}：{1:N1} MB" -f (Split-Path -Leaf $p), ((Get-Item -LiteralPath $p).Length / 1MB))
        }
    }
    exit 0
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Zh '计划任务本就不存在，无需卸载。'
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Zh ("已卸载计划任务：{0}" -f $TaskName)
    exit 0
}

if (-not $Install) {
    Exit-WithZhError '请指定 -Install / -Uninstall / -Status / -RotateLogs 之一。'
}

$venvPython = Get-VenvPython -RepoRoot $repoRoot
if (-not (Test-Path -LiteralPath $venvPython)) {
    Exit-WithZhError '尚未安装。请先运行 install.ps1（会建 .venv）。'
}

# Run the server through a small cmd wrapper so we can redirect BOTH streams to
# files: Start-Process alone cannot append stdout+stderr to the same file pair,
# and losing stderr would hide exactly the startup failures we care about.
$runner = Join-Path $logsDir 'run-workflow.cmd'
$cmdBody = @"
@echo off
chcp 65001 >nul
set PYTHONPATH=$repoRoot\src
cd /d "$repoRoot"
"$venvPython" -m shopkeeper.workflow.serve 1>>"$stdoutLog" 2>>"$stderrLog"
"@
Set-Content -LiteralPath $runner -Value $cmdBody -Encoding ASCII

# Rotate BEFORE starting so a long-running server cannot grow the file unbounded.
Invoke-RotateLogs -Path $stdoutLog -MaxMB $MaxLogMB -Keep $KeepLogs
Invoke-RotateLogs -Path $stderrLog -MaxLogMB $MaxLogMB -Keep $KeepLogs

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$runner`"" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Interactive session is required: the server is meant to be reachable from the
# desktop shell / browser on this machine, and /healthz is loopback-only.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Zh ("已注册开机自启计划任务：{0}" -f $TaskName)
Write-Zh ("日志目录：{0}（单文件上限 {1} MB，保留 {2} 份）" -f $logsDir, $MaxLogMB, $KeepLogs)
Write-Zh 'TTS_WRITE_ENABLED 不会被本脚本修改（默认保持关闭）。'

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Zh '已立即启动该任务。'
}
exit 0
