# Start the workflow server and open the browser to /workflow.
# Prefer running after install.ps1. Re-probes WORKFLOW_PORT if occupied.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/start.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/start.ps1 -NonInteractive
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/start.ps1 -CreateShortcut

param(
    [switch]$NonInteractive,
    [switch]$CreateShortcut,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Initialize-ConsoleUtf8

$repoRoot = Get-RepoRoot
Set-Location -LiteralPath $repoRoot

$venvPython = Get-VenvPython -RepoRoot $repoRoot
if (-not (Test-Path -LiteralPath $venvPython)) {
    Exit-WithZhError '尚未安装。请先运行：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install.ps1'
}

$envPath = Get-EnvFilePath -RepoRoot $repoRoot
if (-not (Test-Path -LiteralPath $envPath)) {
    Exit-WithZhError '缺少 .env。请先运行 install.ps1（会从 .env.example 复制占位文件）。'
}

$hostAddr = Read-DotEnvValue -Path $envPath -Key 'WORKFLOW_HOST'
if (-not $hostAddr) { $hostAddr = '127.0.0.1' }

$portRaw = Read-DotEnvValue -Path $envPath -Key 'WORKFLOW_PORT'
$startPort = 8010
if ($portRaw -match '^\d+$') { $startPort = [int]$portRaw }

# Re-probe: if the saved port is busy, bump and persist (do not touch TTS_WRITE_ENABLED).
if (-not (Test-PortFree -Port $startPort -HostAddress $hostAddr)) {
    $resolvedPort = Find-FreeWorkflowPort -StartPort ($startPort + 1) -HostAddress $hostAddr
    Set-DotEnvValue -Path $envPath -Key 'WORKFLOW_PORT' -Value ([string]$resolvedPort)
    Write-Zh -Message ("端口 {0} 已被占用，已顺延为 {1} 并更新 .env。" -f $startPort, $resolvedPort) -Level 'WARN'
} else {
    $resolvedPort = $startPort
}

if ($CreateShortcut) {
    $startBat = Join-Path $PSScriptRoot 'start.bat'
    New-DesktopShortcut -RepoRoot $repoRoot -StartBatPath $startBat
}

$url = "http://${hostAddr}:${resolvedPort}/workflow"
$healthUrl = "http://${hostAddr}:${resolvedPort}/healthz"
Write-Zh ("即将启动工作台：{0}" -f $url)
Write-Zh 'TTS_WRITE_ENABLED 不会被本脚本修改（默认保持关闭）。'

$env:PYTHONPATH = (Join-Path $repoRoot 'src')
$env:WORKFLOW_PORT = [string]$resolvedPort

# Open browser from a side process after /healthz is up (keeps serve in the foreground).
if (-not $NonInteractive -and -not $SkipBrowser) {
    $opener = @"
`$ErrorActionPreference = 'SilentlyContinue'
for (`$i = 0; `$i -lt 60; `$i++) {
    Start-Sleep -Seconds 1
    try {
        `$resp = Invoke-WebRequest -Uri '$healthUrl' -UseBasicParsing -TimeoutSec 2
        if (`$resp.StatusCode -eq 200) {
            Start-Process '$url'
            exit 0
        }
    } catch { }
}
"@
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $opener) `
        -WindowStyle Hidden | Out-Null
    Write-Zh ("探活成功后将打开浏览器：{0}" -f $url)
} else {
    Write-Zh ("NonInteractive/SkipBrowser：服务起来后请访问 {0}" -f $url)
}

Write-Zh '服务运行中。按 Ctrl+C 结束。'
& $venvPython -m shopkeeper.workflow.serve
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Exit-WithZhError ("服务退出码 {0}。若提示端口占用，请重新运行 install.ps1 或修改 .env 中的 WORKFLOW_PORT。" -f $exitCode)
}
exit $exitCode
