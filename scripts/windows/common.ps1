# Shared helpers for Windows one-click install/start (PowerShell 5.1 + pwsh 7).
# Dot-source only. Do not print secrets. ASCII identifiers; user-facing text may be Chinese.
# IMPORTANT: keep this file UTF-8 with BOM so Windows PowerShell 5.1 parses Chinese correctly.

$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}

function Initialize-ConsoleUtf8 {
    # Chinese Windows consoles are often GBK; prefer UTF-8 so Chinese messages render.
    try {
        chcp 65001 | Out-Null
    } catch { }
    try {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $script:OutputEncoding = [Console]::OutputEncoding
    } catch { }
}

function Write-Zh {
    param([Parameter(Mandatory = $true)][string]$Message, [string]$Level = 'INFO')
    $prefix = switch ($Level) {
        'ERROR' { '[错误]' }
        'WARN' { '[警告]' }
        default { '[信息]' }
    }
    Write-Host ("{0} {1}" -f $prefix, $Message)
}

function Exit-WithZhError {
    param([Parameter(Mandatory = $true)][string]$Message, [int]$ExitCode = 1)
    Write-Zh -Message $Message -Level 'ERROR'
    exit $ExitCode
}

function Get-EnvFilePath {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return (Join-Path $RepoRoot '.env')
}

function Read-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trim = $line.Trim()
        if (-not $trim -or $trim.StartsWith('#')) { continue }
        $eq = $trim.IndexOf('=')
        if ($eq -lt 1) { continue }
        $k = $trim.Substring(0, $eq).Trim()
        if ($k -ne $Key) { continue }
        $v = $trim.Substring($eq + 1).Trim().Trim('"').Trim("'")
        return $v
    }
    return $null
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )
    # Only rewrite the named key. Never invent secrets. Never touch TTS_WRITE_ENABLED here.
    $lines = @()
    $found = $false
    if (Test-Path -LiteralPath $Path) {
        $lines = Get-Content -LiteralPath $Path -Encoding UTF8
    }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            [void]$out.Add(('{0}={1}' -f $Key, $Value))
            $found = $true
        } else {
            [void]$out.Add($line)
        }
    }
    if (-not $found) {
        [void]$out.Add(('{0}={1}' -f $Key, $Value))
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($Path, $out.ToArray(), $utf8NoBom)
}

function Test-PortFree {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [string]$HostAddress = '127.0.0.1'
    )
    $ip = [System.Net.IPAddress]::Parse($HostAddress)
    $listener = New-Object System.Net.Sockets.TcpListener $ip, $Port
    try {
        $listener.Start()
        $listener.Stop()
        return $true
    } catch {
        try { $listener.Stop() } catch { }
        return $false
    }
}

function Find-FreeWorkflowPort {
    param(
        [int]$StartPort = 8010,
        [int]$MaxTries = 50,
        [string]$HostAddress = '127.0.0.1'
    )
    if ($StartPort -lt 1 -or $StartPort -gt 65535) {
        Exit-WithZhError "端口号无效：$StartPort"
    }
    for ($i = 0; $i -lt $MaxTries; $i++) {
        $candidate = $StartPort + $i
        if ($candidate -gt 65535) { break }
        if (Test-PortFree -Port $candidate -HostAddress $HostAddress) {
            return $candidate
        }
    }
    Exit-WithZhError ("从 {0} 起连续 {1} 个端口都被占用，请关闭占用程序或在 .env 中手动设置 WORKFLOW_PORT。" -f $StartPort, $MaxTries)
}

function Resolve-Python312 {
    # Prefer the Windows py launcher, then python on PATH. Require 3.12.x.
    $candidates = @()
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $exe = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
        } catch { }
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $candidates += $pythonCmd.Source }
    $python3Cmd = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3Cmd) { $candidates += $python3Cmd.Source }

    foreach ($exe in $candidates) {
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        try {
            $ver = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            if ($ver.Trim() -eq '3.12') { return $exe }
        } catch { }
    }
    return $null
}

function Get-VenvPython {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return (Join-Path $RepoRoot '.venv\Scripts\python.exe')
}

function New-DesktopShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$StartBatPath
    )
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop) {
        Write-Zh -Message '无法定位桌面目录，跳过快捷方式。' -Level 'WARN'
        return
    }
    $lnkPath = Join-Path $desktop 'AI店长工作台.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnkPath)
    $sc.TargetPath = $StartBatPath
    $sc.WorkingDirectory = $RepoRoot
    $sc.WindowStyle = 1
    $sc.Description = '启动 AI 店长泰国站工作台'
    $sc.Save()
    Write-Zh ("已创建桌面快捷方式：{0}" -f $lnkPath)
}
