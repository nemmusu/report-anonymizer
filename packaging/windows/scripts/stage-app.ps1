<#
.SYNOPSIS
    Assembles the staging/app/ tree from repo sources + already-staged
    runtime/tools/python/llama-variants. Implements plan §2.6.

.DESCRIPTION
    Copies the Python source trees (anonymize/, gui/, bin/, config/,
    prompts/, templates/, assets/) into staging/app/repo/, copies the
    compiled launcher executables into staging/app/launcher/, and verifies
    the result so that subsequent steps (smoke-test, ISCC) operate on a
    consistent tree.

    Expected pre-conditions (run earlier scripts first):
      - staging/app/python/                (build-python-tree.ps1)
      - staging/app/runtime/               (extract-runtime.ps1)
      - staging/app/tools/                 (extract-runtime.ps1)
      - staging/app/llama-variants/*       (extract-runtime.ps1)
      - launcher/ReportAnonymizer.exe      (compile-launchers.ps1)
      - launcher/report-anonymizer-cli.exe (compile-launchers.ps1)
      - launcher/app_icon.ico              (convert-assets.ps1)

.PARAMETER StagingRoot
.PARAMETER Lean
    Skip CUDA + Vulkan variants (mirrors prepare-payload / extract-runtime).
#>
[CmdletBinding()]
param(
    [string]$StagingRoot,
    [switch]$Lean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $WindowsRoot)
if (-not $StagingRoot) { $StagingRoot = Join-Path $WindowsRoot 'staging' }

$AppRoot      = Join-Path $StagingRoot 'app'
$RepoDir      = Join-Path $AppRoot 'repo'
$LauncherDir  = Join-Path $AppRoot 'launcher'

New-Item -ItemType Directory -Force -Path $AppRoot, $LauncherDir | Out-Null

# ---------------------------------------------------------------------------
# 1. Stage the repo tree.
# ---------------------------------------------------------------------------
Write-Host "== Step 1: stage repo source ==" -ForegroundColor Cyan
if (Test-Path -LiteralPath $RepoDir) {
    Remove-Item -Recurse -Force -LiteralPath $RepoDir
}
New-Item -ItemType Directory -Force -Path $RepoDir | Out-Null

# Subdirectories that must end up in <app>\repo\.
$RepoSubdirs = @(
    'anonymize',
    'gui',
    'bin',
    'config',
    'prompts',
    'templates',
    'assets'
)

foreach ($sd in $RepoSubdirs) {
    $src = Join-Path $RepoRoot $sd
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Source directory missing in repo: $src"
    }
    $dst = Join-Path $RepoDir $sd
    Write-Host "  -> copying $sd/ -> repo\$sd\" -ForegroundColor DarkGray

    # Use robocopy for resilience on long paths and many small files. Fall
    # back to Copy-Item when robocopy is not available.
    $robo = Get-Command robocopy.exe -ErrorAction SilentlyContinue
    if ($robo) {
        # /MIR mirror, /NFL /NDL /NJH /NJS /NP silence the chatter,
        # /XD __pycache__ skip caches, /XF *.pyc skip compiled bytecode.
        $roboArgs = @($src, $dst, '/MIR', '/XD', '__pycache__', '/XF', '*.pyc', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
        & robocopy.exe @roboArgs | Out-Null
        # robocopy exits 0-7 for success, >=8 for failures.
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy failed copying $sd (exit $LASTEXITCODE)"
        }
        # robocopy mutates $LASTEXITCODE so reset it for the next command.
        $global:LASTEXITCODE = 0
    } else {
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
        Get-ChildItem -LiteralPath $dst -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
    }
}

# Top-level docs sample used by the smoke-test pandoc compatibility check.
$sampleSrcDir = Join-Path $RepoRoot 'docs\sample_report'
if (Test-Path -LiteralPath $sampleSrcDir) {
    $sampleDst = Join-Path $RepoDir 'docs\sample_report'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $sampleDst) | Out-Null
    Copy-Item -LiteralPath $sampleSrcDir -Destination $sampleDst -Recurse -Force
}

# Standalone files that some modules expect at the repo root.
foreach ($f in @('requirements.txt', 'pyproject.toml', 'LICENSE', 'README.md')) {
    $src = Join-Path $RepoRoot $f
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $RepoDir $f) -Force
    }
}

# ---------------------------------------------------------------------------
# 1.5. Wire the staged repo into python312._pth.
#
# The Python embed bundle ignores PYTHONPATH whenever a *._pth file is
# present alongside python.exe (which we ship). So that smoke-test.ps1
# *and* the launcher executable can import anonymize/gui out of
# staging/app/repo/ without modifying sys.path from C, we append the
# relative path "..\repo" to python312._pth here. This is layout-coupled
# (requires repo/ to live one directory up from python/), which is what
# stage-app.ps1 has already enforced above.
# ---------------------------------------------------------------------------
$PthCandidates = Get-ChildItem -LiteralPath (Join-Path $AppRoot 'python') -Filter 'python*._pth' -File -ErrorAction SilentlyContinue
foreach ($pth in $PthCandidates) {
    $content = Get-Content -LiteralPath $pth.FullName -Raw
    if ($content -notmatch '(?m)^\s*\.\.\\repo\s*$') {
        $content = $content.TrimEnd() + "`r`n..\repo`r`n"
        Set-Content -LiteralPath $pth.FullName -Value $content -Encoding ASCII
        Write-Host "  -> appended ..\repo to $($pth.Name)" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# 2. Copy compiled launcher binaries + icon into app/launcher/.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 2: stage launcher executables ==" -ForegroundColor Cyan
$launcherSrcDir = Join-Path $WindowsRoot 'launcher'
$RequiredLauncherFiles = @(
    'ReportAnonymizer.exe',
    'report-anonymizer-cli.exe',
    'app_icon.ico'
)
foreach ($f in $RequiredLauncherFiles) {
    $src = Join-Path $launcherSrcDir $f
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Launcher artefact missing: $src. Run compile-launchers.ps1 (and convert-assets.ps1) first."
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $LauncherDir $f) -Force
    Write-Host "  -> staged $f" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 3. Validate.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 3: validate staging tree ==" -ForegroundColor Cyan
$ExpectedPaths = @(
    'python\python.exe',
    'runtime',
    'tools\pdftotext.exe',
    'launcher\ReportAnonymizer.exe',
    'launcher\report-anonymizer-cli.exe',
    'launcher\app_icon.ico',
    'repo\anonymize\__init__.py',
    'repo\gui\main.py',
    'repo\bin\anonymize-dossier'
)
$missing = @()
foreach ($rel in $ExpectedPaths) {
    if (-not (Test-Path -LiteralPath (Join-Path $AppRoot $rel))) {
        $missing += $rel
    }
}

# Variant validation: CPU is mandatory, CUDA/Vulkan only when not Lean.
$variantsDir = Join-Path $AppRoot 'llama-variants'
$expectedVariants = @('cpu')
if (-not $Lean) { $expectedVariants += @('cuda', 'vulkan') }
foreach ($v in $expectedVariants) {
    $exp = Join-Path $variantsDir "$v\llama-server.exe"
    if (-not (Test-Path -LiteralPath $exp)) {
        $missing += "llama-variants\$v\llama-server.exe"
    }
}

if ($missing) {
    throw "stage-app: required paths missing under $AppRoot`n  - " + ($missing -join "`n  - ")
}

# Report the final staging size.
$totalBytes = (Get-ChildItem -LiteralPath $AppRoot -Recurse -File -ErrorAction SilentlyContinue |
               Measure-Object -Property Length -Sum).Sum
Write-Host ("  -> staging size: {0:N1} MB" -f ($totalBytes / 1MB)) -ForegroundColor Green

Write-Host ""
Write-Host "[stage-app] OK -- ready for smoke-test + ISCC" -ForegroundColor Green
