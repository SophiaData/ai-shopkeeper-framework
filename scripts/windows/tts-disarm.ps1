# One-click "disarm" for real TikTok writes (HANDOFF_PLAN P0-10).
#
# Why this exists: the only risky state in this repo is TTS_WRITE_ENABLED=1.
# After a real listing is done the owner must turn it back off immediately --
# by hand, under time pressure, possibly late at night. A single command that
# only ever writes 0 removes both the effort and the chance of a typo.
#
# Safety properties (do not weaken):
#   1. This script can ONLY write TTS_WRITE_ENABLED=0. It never writes 1.
#   2. It never prints or rewrites secrets (API keys/tokens).
#   3. Without -Apply it is a read-only report (dry run by default).
#
# Usage:
#   powershell -File scripts\windows\tts-disarm.ps1            # report only
#   powershell -File scripts\windows\tts-disarm.ps1 -Apply     # actually write 0

param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Initialize-ConsoleUtf8

$repoRoot = Get-RepoRoot
$envFile = Get-EnvFilePath -RepoRoot $repoRoot

if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Zh ("没有找到 .env：{0}" -f $envFile)
    Write-Zh "真写开关本来就处于默认关闭状态，无需处理。"
    exit 0
}

$current = Read-DotEnvValue -Path $envFile -Key 'TTS_WRITE_ENABLED'
$allowlist = Read-DotEnvValue -Path $envFile -Key 'TTS_WRITE_SHOP_ALLOWLIST'
$daily = Read-DotEnvValue -Path $envFile -Key 'TTS_WRITE_DAILY_LIMIT'

Write-Zh "=== TikTok 真写开关现状 ==="
Write-Zh ("  .env            : {0}" -f $envFile)
Write-Zh ("  TTS_WRITE_ENABLED: {0}" -f ($(if ($null -eq $current -or $current -eq '') { '(未设置=0)' } else { $current })))
Write-Zh ("  白名单店铺数     : {0}" -f ($(if ($null -eq $allowlist -or $allowlist -eq '') { '0' } else { ($allowlist -split ',').Count })))
Write-Zh ("  每日上限         : {0}" -f ($(if ($null -eq $daily -or $daily -eq '') { '(未设置=默认 1)' } else { $daily })))

$isOn = ($current -eq '1')
if (-not $isOn) {
    Write-Zh ""
    Write-Zh "结论：真写已是关闭状态（TTS_WRITE_ENABLED 不是 1）。无需处理。"
    exit 0
}

Write-Zh ""
Write-Zh "结论：真写【处于开启状态】。"

if (-not $Apply) {
    Write-Zh "当前是只读报告模式（未改动任何文件）。"
    Write-Zh "要立即关闭真写，请运行："
    Write-Zh "  powershell -File scripts\windows\tts-disarm.ps1 -Apply"
    exit 0
}

Set-DotEnvValue -Path $envFile -Key 'TTS_WRITE_ENABLED' -Value '0'
$after = Read-DotEnvValue -Path $envFile -Key 'TTS_WRITE_ENABLED'

if ($after -ne '0') {
    Exit-WithZhError ("关写失败：TTS_WRITE_ENABLED 仍为 {0}" -f $after)
}

Write-Zh ""
Write-Zh "已关闭真写：TTS_WRITE_ENABLED=0"
Write-Zh "注意：.env 在下次重启服务后生效。若服务正在运行，请一并重启。"
Write-Zh "提示：新开的上架请求会被 TTS_WRITE_BLOCKED 拦下，这是预期行为。"
