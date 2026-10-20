# Windows one-click install for Chinese novice sellers.
# Idempotent: safe to re-run. Never writes API secrets. Never changes TTS_WRITE_ENABLED.
#
# Usage (from repo root or anywhere):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/install.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/install.ps1 -NonInteractive
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/install.ps1 -CreateShortcut
#
# Default WORKFLOW_PORT=8010 (Windows 8000 is often taken). Occupied ports bump upward.

param(
    [switch]$NonInteractive,
    [switch]$CreateShortcut,
    [int]$PreferredPort = 8010
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Initialize-ConsoleUtf8

$repoRoot = Get-RepoRoot
Set-Location -LiteralPath $repoRoot
Write-Zh ("仓库目录：{0}" -f $repoRoot)

# ---- Python 3.12 ----
$python = Resolve-Python312
if (-not $python) {
    Exit-WithZhError @'
未找到 Python 3.12。请先安装 Python 3.12（安装时勾选 “Add python.exe to PATH”），然后重新打开命令行再运行本脚本。
下载：https://www.python.org/downloads/release/python-3120/
'@
}
Write-Zh ("使用 Python：{0}" -f $python)

# ---- venv ----
$venvDir = Join-Path $repoRoot '.venv'
$venvPython = Get-VenvPython -RepoRoot $repoRoot
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Zh '正在创建虚拟环境 .venv …'
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Exit-WithZhError '创建虚拟环境失败。请确认已安装 Python 3.12 且有写入本目录的权限。'
    }
} else {
    Write-Zh '已存在 .venv，跳过创建。'
}

# ---- pip install (runtime deps; sellers do not need playwright) ----
Write-Zh '正在安装依赖（pip install -e .）…'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Exit-WithZhError '升级 pip 失败。请检查网络后重试。'
}
& $venvPython -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Exit-WithZhError '安装 shopkeeper 失败。请检查网络与 pyproject.toml 后重试。'
}
Write-Zh '依赖安装完成。'

# ---- .env from example (placeholders only; no secrets invented) ----
$envPath = Get-EnvFilePath -RepoRoot $repoRoot
$examplePath = Join-Path $repoRoot '.env.example'
$createdEnv = $false
if (-not (Test-Path -LiteralPath $envPath)) {
    if (-not (Test-Path -LiteralPath $examplePath)) {
        Exit-WithZhError '缺少 .env.example，无法生成 .env。请确认仓库完整。'
    }
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    $createdEnv = $true
    Write-Zh '已从 .env.example 复制生成 .env（仅占位，未写入任何密钥）。'
} else {
    Write-Zh '已存在 .env，跳过复制。'
}

# Guard: never flip TTS_WRITE_ENABLED. If missing after copy, leave as-is from example (0).
$tts = Read-DotEnvValue -Path $envPath -Key 'TTS_WRITE_ENABLED'
if ($null -ne $tts -and $tts.Trim() -ne '0') {
    Write-Zh -Message ("检测到 TTS_WRITE_ENABLED={0}；安装脚本不会修改该值（保持你的设置）。" -f $tts) -Level 'WARN'
} else {
    Write-Zh 'TTS_WRITE_ENABLED 保持为 0（安装脚本不修改该开关）。'
}

# ---- WORKFLOW_PORT: default 8010, probe and bump if busy ----
$hostAddr = Read-DotEnvValue -Path $envPath -Key 'WORKFLOW_HOST'
if (-not $hostAddr) { $hostAddr = '127.0.0.1' }

$currentPortRaw = Read-DotEnvValue -Path $envPath -Key 'WORKFLOW_PORT'
$startPort = $PreferredPort
if ($currentPortRaw -match '^\d+$') {
    $currentPort = [int]$currentPortRaw
    # Fresh copy from .env.example still says 8000; Windows default is 8010.
    if ($createdEnv -and $currentPort -eq 8000) {
        $startPort = $PreferredPort
    } else {
        $startPort = $currentPort
    }
}

$resolvedPort = Find-FreeWorkflowPort -StartPort $startPort -HostAddress $hostAddr
if ($currentPortRaw -ne [string]$resolvedPort) {
    Set-DotEnvValue -Path $envPath -Key 'WORKFLOW_PORT' -Value ([string]$resolvedPort)
    if ($currentPortRaw -and $currentPortRaw -ne [string]$resolvedPort) {
        Write-Zh ("端口 {0} 不可用，已顺延为 WORKFLOW_PORT={1} 并写入 .env。" -f $currentPortRaw, $resolvedPort)
    } else {
        Write-Zh ("已设置 WORKFLOW_PORT={0}（写入 .env）。" -f $resolvedPort)
    }
} else {
    Write-Zh ("WORKFLOW_PORT={0} 可用，保持不变。" -f $resolvedPort)
}

# Optional desktop shortcut
if ($CreateShortcut) {
    $startBat = Join-Path $PSScriptRoot 'start.bat'
    if (-not (Test-Path -LiteralPath $startBat)) {
        Write-Zh -Message '未找到 start.bat，跳过快捷方式。' -Level 'WARN'
    } else {
        New-DesktopShortcut -RepoRoot $repoRoot -StartBatPath $startBat
    }
} elseif (-not $NonInteractive) {
    Write-Zh '提示：需要桌面快捷方式时，请加参数 -CreateShortcut。'
}

Write-Zh '安装完成。下一步：双击 scripts\windows\start.bat，或在本目录运行：'
Write-Zh '  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\start.ps1'
Write-Zh ("浏览器将打开 http://{0}:{1}/workflow" -f $hostAddr, $resolvedPort)

if ($NonInteractive) {
    # Emit a machine-readable line for CI without printing secrets.
    Write-Output ("INSTALL_OK port={0}" -f $resolvedPort)
}

exit 0
