<#
.SYNOPSIS
    Converts assets/app_icon.svg into launcher/app_icon.ico (multi-resolution).

.DESCRIPTION
    Implements plan §2.3.bis. Creates a temporary venv in
    build-cache/assetvenv with Pillow + cairosvg and renders the SVG into a
    Windows .ico containing 16/24/32/48/64/128/256-px frames. Idempotent:
    skipped when the .ico already exists and the .svg has not been modified
    since its last write.

    Fallback strategy (cf. plan §N1):
      1. cairosvg path (preferred): pure-Python wheels, no system DLL.
      2. If the cairosvg import fails (e.g. missing cairo DLL on the host),
         and a pre-generated `assets/app_icon.ico` is committed to the repo,
         copy that fallback into launcher/.
      3. Otherwise abort loudly so the build does not produce an Setup.exe
         without an icon.

    The venv is created using the staging Python embed (if already built) or
    the system `python` on PATH; Pillow + cairosvg are tiny and the venv is
    reused across runs.

.PARAMETER Force
    Always rebuild the .ico, even when the existing file is fresh.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $WindowsRoot)

$SvgPath        = Join-Path $RepoRoot 'assets\app_icon.svg'
$IcoPath        = Join-Path $WindowsRoot 'launcher\app_icon.ico'
$FallbackIco    = Join-Path $RepoRoot 'assets\app_icon.ico'
$VenvDir        = Join-Path $WindowsRoot 'build-cache\assetvenv'
$VenvPython     = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip        = Join-Path $VenvDir 'Scripts\pip.exe'

if (-not (Test-Path -LiteralPath $SvgPath)) {
    throw "Missing source SVG: $SvgPath"
}

# ---------------------------------------------------------------------------
# Idempotency: skip if the target is newer than the source.
# ---------------------------------------------------------------------------
if (-not $Force -and (Test-Path -LiteralPath $IcoPath)) {
    $svgItem = Get-Item -LiteralPath $SvgPath
    $icoItem = Get-Item -LiteralPath $IcoPath
    if ($icoItem.LastWriteTime -ge $svgItem.LastWriteTime) {
        Write-Host "[convert-assets] app_icon.ico is up to date (skip)" -ForegroundColor Green
        return
    }
}

# ---------------------------------------------------------------------------
# Pick a Python interpreter for the venv. Preference order:
#   1. staging Python embed (already built by build-python-tree.ps1)
#   2. system `py -3` launcher
#   3. plain `python` on PATH
# ---------------------------------------------------------------------------
function Find-PythonInterpreter {
    $stagingPy = Join-Path $WindowsRoot 'staging\app\python\python.exe'
    if (Test-Path -LiteralPath $stagingPy) {
        # The embed python.exe doesn't ship venv, so use it only as a fallback
        # for running the conversion script directly (no venv needed).
        return @{ Path = $stagingPy; CanVenv = $false }
    }
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        return @{ Path = 'py'; CanVenv = $true; Prefix = @('-3') }
    }
    $python = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($python) {
        return @{ Path = 'python'; CanVenv = $true; Prefix = @() }
    }
    return $null
}

function Invoke-Python {
    param(
        [Parameter(Mandatory)] [hashtable]$Interpreter,
        [Parameter(Mandatory)] [string[]]$Args
    )
    $allArgs = @()
    if ($Interpreter.ContainsKey('Prefix')) { $allArgs += $Interpreter.Prefix }
    $allArgs += $Args
    & $Interpreter.Path @allArgs
    return $LASTEXITCODE
}

function Use-FallbackIco {
    param([string]$Reason)
    if (Test-Path -LiteralPath $FallbackIco) {
        Write-Warning "convert-assets: $Reason -- using fallback $FallbackIco"
        Copy-Item -LiteralPath $FallbackIco -Destination $IcoPath -Force
        # The caller is reacting to a previous failure (e.g. cairosvg
        # exited 1 because libcairo is missing on the runner) and
        # ``$LASTEXITCODE`` still carries that non-zero value. Reset it
        # so the parent ``Invoke-Step`` in ``build.ps1:121`` doesn't
        # interpret a successful-fallback as a step failure.
        $global:LASTEXITCODE = 0
        return $true
    }
    return $false
}

# ---------------------------------------------------------------------------
# Build / reuse venv.
# ---------------------------------------------------------------------------
$interp = Find-PythonInterpreter
if (-not $interp) {
    Write-Warning "No system Python found to build the asset venv."
    if (-not (Use-FallbackIco -Reason 'no Python available')) {
        throw "No Python interpreter on PATH and no fallback ICO at $FallbackIco."
    }
    return
}

if ($interp.CanVenv -and -not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "[convert-assets] creating venv at $VenvDir" -ForegroundColor DarkGray
    [void](Invoke-Python -Interpreter $interp -Args @('-m', 'venv', $VenvDir))
    if ($LASTEXITCODE -ne 0) {
        if (-not (Use-FallbackIco -Reason 'venv creation failed')) {
            throw "Failed to create venv at $VenvDir."
        }
        return
    }
}

if ($interp.CanVenv) {
    & $VenvPip install --quiet --disable-pip-version-check 'Pillow>=10.0' 'cairosvg>=2.7'
    if ($LASTEXITCODE -ne 0) {
        if (-not (Use-FallbackIco -Reason 'pip install Pillow+cairosvg failed')) {
            throw "Failed to install Pillow+cairosvg into $VenvDir."
        }
        return
    }
    $python = $VenvPython
} else {
    # Fall back to invoking the embed python; we still need the modules,
    # which it doesn't have by default. So in this branch we always defer
    # to the precompiled fallback ICO.
    if (-not (Use-FallbackIco -Reason 'no venv-capable interpreter')) {
        throw "Embed python cannot host Pillow/cairosvg and no fallback ICO present."
    }
    return
}

# ---------------------------------------------------------------------------
# Run the conversion.
# ---------------------------------------------------------------------------
$pyScript = @"
import io, sys, os
from cairosvg import svg2png
from PIL import Image

src = r'$SvgPath'
dst = r'$IcoPath'
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = []
for sz in sizes:
    png = svg2png(url=src, output_width=sz, output_height=sz)
    imgs.append(Image.open(io.BytesIO(png)).convert('RGBA'))
imgs[0].save(dst, format='ICO', sizes=[(s, s) for s in sizes], append_images=imgs[1:])
print('OK', dst, len(sizes), 'sizes')
"@

$scratch = Join-Path $env:TEMP ('rapack-svg2ico-{0}.py' -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
$pyScript | Set-Content -LiteralPath $scratch -Encoding UTF8
try {
    & $python $scratch
    $rc = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $scratch -ErrorAction SilentlyContinue
}

if ($rc -ne 0 -or -not (Test-Path -LiteralPath $IcoPath)) {
    if (-not (Use-FallbackIco -Reason "cairosvg conversion failed (exit $rc)")) {
        throw "cairosvg conversion failed (exit $rc) and no fallback ICO present."
    }
    return
}

Write-Host "[convert-assets] generated $IcoPath" -ForegroundColor Green
