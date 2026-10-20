# Local CI entry for M1 (no remote CI: GitHub unreachable from this machine, see docs/M1-PLAN.md #0)
# Usage (Windows PowerShell 5.1 or pwsh 7):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1 -SkipPg   # no Docker this run
# Any step failing -> non-zero exit code.
# NOTE: keep echoed text ASCII-only; PS 5.1 mangles non-ASCII stdout.
param([switch]$SkipPg)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root
$env:PYTHONPATH = (Join-Path $root 'src')
$failed = @()

"=== [0.5] admin frontend build (Vite; no CDN; optional if node missing) ==="
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location (Join-Path $root 'apps/admin')
    try {
        npm ci 2>&1 | Select-Object -Last 8
        if ($LASTEXITCODE -ne 0) { $failed += "admin-npm-ci(exit=$LASTEXITCODE)" }
        else {
            npm run build 2>&1 | Select-Object -Last 12
            if ($LASTEXITCODE -ne 0) { $failed += "admin-build(exit=$LASTEXITCODE)" }
            else {
                npm test 2>&1 | Select-Object -Last 8
                if ($LASTEXITCODE -ne 0) { $failed += "admin-smoke(exit=$LASTEXITCODE)" }
            }
        }
    } finally { Pop-Location }

    # dist is committed (FastAPI serves src/shopkeeper/admin/dist). Editing apps/admin/src
    # without rebuilding leaves the server on stale frontend. We just built above; assert
    # the artifacts match what is committed. Same step exists in .github/workflows/ci.yml.
    $distDirty = git diff --name-only -- src/shopkeeper/admin/dist
    $distUntracked = git ls-files --others --exclude-standard -- src/shopkeeper/admin/dist
    if ($distDirty -or $distUntracked) {
        "FAIL: src/shopkeeper/admin/dist is stale vs apps/admin/src"
        $distDirty | ForEach-Object { "  modified: $_" }
        $distUntracked | ForEach-Object { "  untracked: $_" }
        "  Run 'npm run build' in apps/admin and commit the artifacts."
        $failed += "admin-dist-stale"
    } else {
        "admin dist matches source"
    }
} else {
    "SKIPPED: npm not found (committed dist still required for /admin)"
}

"=== [1/7] pytest (deterministic gate; live-network tests deselected) ==="
# -m "not real" keeps this gate reproducible offline. Live-gateway coverage runs via
# `pytest -m real` and via scripts/b4_e2e_demo.py (whose log is the M1 evidence).
python -m pytest tests -v --tb=short -p no:cacheprovider -m "not real" 2>&1 | Select-Object -Last 40
if ($LASTEXITCODE -ne 0) { $failed += "pytest(exit=$LASTEXITCODE)" }

"=== [2/7] dual-DB DDL verify (SQLite + Docker PG15) ==="
# C1: skips silently = lying. -SkipPg marks the run explicitly incomplete.
if ($SkipPg) {
    "SKIPPED by -SkipPg -> this run does NOT satisfy M1-C C1 (dual-DB)"
    python scripts/ddl_verify.py --sqlite-only 2>&1 | Select-Object -Last 8
    if ($LASTEXITCODE -ne 0) { $failed += "ddl-verify-sqlite(exit=$LASTEXITCODE)" }
} else {
    python scripts/ddl_verify.py 2>&1 | Select-Object -Last 12
    if ($LASTEXITCODE -ne 0) { $failed += "ddl-verify(exit=$LASTEXITCODE)" }
}

"=== [3/7] secret scan (no plaintext keys in repo) ==="
try {
    $leak = Get-ChildItem -Recurse -File -Include '*.py', '*.toml', '*.md', '*.ps1', '*.sql', '*.json', '*.env', '.env*' |
        Where-Object { $_.FullName -notmatch '\\\.git\\' } |
        Select-String -Pattern 'sk-[A-Za-z0-9]{20,}', 'LTAI[0-9A-Za-z]{12,}', 'AccessKey Secret\s*[:=]\s*\S', 'api_key\s*[:=]\s*["'']sk-[A-Za-z0-9]{16,}' -AllMatches
    if ($leak) { $leak | ForEach-Object { "LEAK: $($_.Path):$($_.LineNumber)" }; $failed += "secret-leak" }
    else { "no plaintext secret found" }
} catch { "secret scan error: $_"; $failed += "secret-scan-error" }

"=== [3b] secret scan (git history; whitelist only known false positives) ==="
# Separate from the working-tree scan above. Hard budget is SCAN_HISTORY_BUDGET_S
# inside the script (default 60s) plus the GitHub job timeout. Output is redacted.
python scripts/scan_history.py 2>&1 | Select-Object -Last 30
if ($LASTEXITCODE -ne 0) { $failed += "scan-history(exit=$LASTEXITCODE)" }

"=== [4/7] .env must not be git-tracked ==="
$tracked = ''
try { $tracked = (git -C $root ls-files .env 2>$null) -join '' } catch { }
if ($tracked) { ".env IS TRACKED - refuse"; $failed += ".env-tracked" } else { ".env not tracked (ok)" }

"=== [5/7] markdown link gate (relative links resolve) ==="
# Static, sub-second, no DB/Docker: catches docs drifting apart. Prior to this gate
# two broken links (spec-axis m-3, m1-demo-run) were found only by manual review.
python scripts/check_links.py 2>&1 | Select-Object -Last 20
if ($LASTEXITCODE -ne 0) { $failed += "md-links(exit=$LASTEXITCODE)" }

"=== [6/7] import smoke (adapters + config) ==="
try {
    python -c "import shopkeeper, shopkeeper.config as c; from shopkeeper.adapters import MockProductSource, MockLLM, MockImage, MockPlatform; s=c.Settings.from_env(); assert s.langs; assert MockProductSource().fetch('https://detail.1688.com/offer/1.html').ok; print('smoke ok langs=', ','.join(s.langs))" 2>&1
    if ($LASTEXITCODE -ne 0) { $failed += "import-smoke(exit=$LASTEXITCODE)" }
} catch { $failed += "import-smoke-error" }

"=== [7/7] deploy artifacts (P1-09: ports/env/safe defaults/loopback) ==="
# Static + one real probe. Catches deployment files drifting from code:
# wrong port, misspelled env var (silent no-op), write channel left on,
# binding to 0.0.0.0, root user, missing healthcheck, unpresisted DB.
# docker build itself is NOT covered here (no daemon on this machine).
python scripts/check_deploy.py 2>&1 | Select-Object -Last 20
if ($LASTEXITCODE -ne 0) { $failed += "deploy-check(exit=$LASTEXITCODE)" }

if ($failed.Count -gt 0) { "CI FAILED: " + ($failed -join ', '); exit 1 }
"CI PASSED"
exit 0
