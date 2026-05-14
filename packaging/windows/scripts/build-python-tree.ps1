<#
.SYNOPSIS
    Builds the Python embeddable + site-packages tree under staging/app/python/.

.DESCRIPTION
    Implements plan §2.4:

      1. Expand the Python 3.12 embeddable zip into staging/app/python/.
      2. Decomment `import site` in python312._pth so that site-packages is
         honoured (the upstream embed disables it by default - cf. plan §E2).
      3. Bootstrap pip via get-pip.py.
      4. pip install -r requirements.txt (so python-magic-bin and
         pypandoc-binary land via the sys_platform == "win32" markers).
      5. Aggressive strip pass: drop __pycache__/, *.dist-info/, *.pyi stubs,
         and PySide6 dev tooling (Designer/Linguist/Assistant/examples) plus
         optional Qt QML if unused.
      6. Validate that the PySide6 Qt plugin DLLs the launcher / smoke-test
         depend on are present; abort if any required plugin is missing.

    Idempotent: subsequent runs reuse the existing tree if Python.exe is
    already present at the expected version (use -Force to rebuild).

.PARAMETER PayloadRoot
.PARAMETER StagingRoot
.PARAMETER Force
    Wipe and rebuild staging/app/python/.
#>
[CmdletBinding()]
param(
    [string]$PayloadRoot,
    [string]$StagingRoot,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $WindowsRoot)

if (-not $PayloadRoot) { $PayloadRoot = Join-Path $WindowsRoot 'build-cache\payload' }
if (-not $StagingRoot) { $StagingRoot = Join-Path $WindowsRoot 'staging' }

$AppRoot   = Join-Path $StagingRoot 'app'
$PyDir     = Join-Path $AppRoot 'python'
$PyExe     = Join-Path $PyDir 'python.exe'
$Embed     = Join-Path $PayloadRoot 'python-3.12.10-embed-amd64.zip'
$Reqs      = Join-Path $RepoRoot 'requirements.txt'

if (-not (Test-Path -LiteralPath $Embed)) {
    throw "Missing Python embed zip: $Embed. Run prepare-payload.ps1 first."
}
if (-not (Test-Path -LiteralPath $Reqs)) {
    throw "Missing requirements.txt: $Reqs"
}

# ---------------------------------------------------------------------------
# Step 1: extract embed.
# ---------------------------------------------------------------------------
if ($Force -and (Test-Path -LiteralPath $PyDir)) {
    Remove-Item -Recurse -Force -LiteralPath $PyDir
}

if (-not (Test-Path -LiteralPath $PyExe)) {
    Write-Host "== Step 1: extracting Python embeddable ==" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
    Expand-Archive -LiteralPath $Embed -DestinationPath $PyDir -Force
    if (-not (Test-Path -LiteralPath $PyExe)) {
        throw "python.exe missing after extracting $Embed"
    }
    Write-Host "  -> python.exe at $PyExe" -ForegroundColor Green
} else {
    Write-Host "[build-python-tree] Reusing existing $PyExe (pass -Force to rebuild)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Step 2: decomment `import site` in python312._pth (plan §E2).
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 2: enable site-packages in python312._pth ==" -ForegroundColor Cyan
$pthCandidates = Get-ChildItem -LiteralPath $PyDir -Filter 'python*._pth' -File -ErrorAction SilentlyContinue
if (-not $pthCandidates) {
    throw "No python*._pth file inside $PyDir"
}
foreach ($pth in $pthCandidates) {
    $content = Get-Content -LiteralPath $pth.FullName -Raw
    $patched = $content -replace '(?m)^\s*#\s*import\s+site\s*$', 'import site'
    if ($patched -notmatch '(?m)^\s*import\s+site\s*$') {
        $patched = $patched.TrimEnd() + "`r`nimport site`r`n"
    }
    Set-Content -LiteralPath $pth.FullName -Value $patched -Encoding ASCII
    Write-Host "  -> patched $($pth.Name)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Step 3: bootstrap pip via get-pip.py.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 3: bootstrap pip ==" -ForegroundColor Cyan
$getPip = Join-Path $PyDir 'get-pip.py'
$pipExe = Join-Path $PyDir 'Scripts\pip.exe'
if (-not (Test-Path -LiteralPath $pipExe)) {
    if (-not (Test-Path -LiteralPath $getPip)) {
        Write-Host "  -> downloading get-pip.py" -ForegroundColor DarkGray
        Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
    }
    & $PyExe $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        throw "get-pip.py bootstrap failed (exit $LASTEXITCODE)"
    }
    Write-Host "  -> pip bootstrapped" -ForegroundColor Green
} else {
    Write-Host "  -> pip already present at $pipExe" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Step 4: install requirements.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 4: pip install -r requirements.txt ==" -ForegroundColor Cyan
# Use the python -m pip invocation so that the embed's PYTHONHOME wins over
# any leakage from the developer's environment.
& $PyExe -m pip install --no-warn-script-location --upgrade pip wheel setuptools
if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade failed (exit $LASTEXITCODE)" }

& $PyExe -m pip install --no-warn-script-location -r $Reqs
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed (exit $LASTEXITCODE)" }
Write-Host "  -> requirements installed" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 5: aggressive strip.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 5: strip dead weight ==" -ForegroundColor Cyan

$sitePkgs = Join-Path $PyDir 'Lib\site-packages'
if (-not (Test-Path -LiteralPath $sitePkgs)) {
    throw "site-packages directory missing after pip install: $sitePkgs"
}

function Remove-ByGlob {
    param(
        [Parameter(Mandatory)] [string]$Root,
        [Parameter(Mandatory)] [string[]]$Globs,
        [switch]$Directory
    )
    $count = 0
    foreach ($glob in $Globs) {
        $items = Get-ChildItem -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue -Filter $glob
        if ($Directory) {
            $items = $items | Where-Object { $_.PSIsContainer }
        } else {
            $items = $items | Where-Object { -not $_.PSIsContainer }
        }
        foreach ($i in $items) {
            try {
                Remove-Item -LiteralPath $i.FullName -Recurse -Force
                $count += 1
            } catch {
                # Some __pycache__ directories may be locked transiently;
                # ignore and continue.
            }
        }
    }
    return $count
}

$removed = 0
$removed += Remove-ByGlob -Root $sitePkgs -Globs @('__pycache__') -Directory
$removed += Remove-ByGlob -Root $sitePkgs -Globs @('*.dist-info') -Directory
$removed += Remove-ByGlob -Root $sitePkgs -Globs @('*.pyi') -Directory:$false
Write-Host "  -> removed $removed dead-weight entries from site-packages" -ForegroundColor DarkGray

# PySide6 dev tooling: large and unused at runtime.
$pyside = Join-Path $sitePkgs 'PySide6'
if (Test-Path -LiteralPath $pyside) {
    foreach ($pattern in @('examples', 'Designer*', 'designer*', 'Linguist*', 'linguist*', 'Assistant*', 'assistant*')) {
        Get-ChildItem -LiteralPath $pyside -Filter $pattern -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                try { Remove-Item -LiteralPath $_.FullName -Recurse -Force } catch {}
            }
    }
    # Drop Qt QML if no Python file under gui/ imports QtQml/Qml. Detect by
    # grepping the staged repo (use the source tree as the reference; the
    # repo copy is identical and the staged copy may not exist yet).
    $usesQml = $false
    $guiSrc = Join-Path $RepoRoot 'gui'
    if (Test-Path -LiteralPath $guiSrc) {
        $hit = Get-ChildItem -LiteralPath $guiSrc -Recurse -Filter '*.py' -File -ErrorAction SilentlyContinue |
               Select-String -SimpleMatch 'QtQml' -List -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $usesQml = $true }
    }
    if (-not $usesQml) {
        $qmlDir = Join-Path $pyside 'Qt\qml'
        if (Test-Path -LiteralPath $qmlDir) {
            Write-Host "  -> dropping PySide6/Qt/qml/ (no QtQml import detected in gui/)" -ForegroundColor DarkGray
            Remove-Item -LiteralPath $qmlDir -Recurse -Force
        }
    } else {
        Write-Host "  -> keeping PySide6/Qt/qml/ (QtQml import detected)" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Step 5.5: install sitecustomize.py - register runtime/ + tools/ via
# os.add_dll_directory() so cffi/ctypes can find the WeasyPrint DLLs.
#
# Background: Python 3.8+ on Windows REMOVED PATH from the search path used
# by ctypes.LoadLibrary / cffi.dlopen for non-system32 DLLs. Even though our
# launcher prepends staging/app/runtime to PATH, that no longer helps - we
# must call os.add_dll_directory() before WeasyPrint imports for libgobject /
# libpango / libharfbuzz / etc. to resolve. The smoke-test and gui/main.py
# both rely on this hook running automatically because `import site` is
# enabled in python312._pth and sitecustomize.py is auto-loaded by site.py.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 5.5: install sitecustomize.py (DLL search hook) ==" -ForegroundColor Cyan
$siteCustomizePath = Join-Path $PyDir 'sitecustomize.py'
$siteCustomizeBody = @'
"""Auto-loaded by site.py at interpreter startup.

Registers the bundled runtime/, tools/ and select site-packages DLL
directories with the Windows DLL loader and PATH, so that cffi / ctypes /
native extensions (WeasyPrint's libgobject, python-magic's libmagic,
poppler's pdftotext, pypandoc's pandoc.exe, etc.) can find their
dependencies.

Required because Python 3.8+ on Windows no longer searches PATH for
non-system DLLs from ctypes.CDLL. We need both:

  * os.add_dll_directory() so ctypes.CDLL() resolves the DLL.
  * PATH prepend so ctypes.util.find_library() (used by python-magic's
    loader) can still find it.
"""
import os
import sys


def _wire_dll_directories() -> None:
    if sys.platform != "win32":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return

    here = os.path.dirname(os.path.abspath(__file__))
    app_root = os.path.dirname(here)  # staging/app/python -> staging/app

    candidates = [
        os.path.join(app_root, "runtime"),
        os.path.join(app_root, "tools"),
    ]

    site_packages = os.path.join(here, "Lib", "site-packages")
    if os.path.isdir(site_packages):
        # python-magic-bin ships libmagic.dll under magic/libmagic/.
        candidates.append(os.path.join(site_packages, "magic", "libmagic"))
        # pypandoc-binary ships pandoc.exe under pypandoc/files/.
        candidates.append(os.path.join(site_packages, "pypandoc", "files"))

    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    path_parts_lower = {p.lower() for p in path_parts if p}

    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        try:
            add_dll_directory(cand)
        except (OSError, FileNotFoundError):
            pass
        if cand.lower() not in path_parts_lower:
            path_parts.insert(0, cand)
            path_parts_lower.add(cand.lower())

    os.environ["PATH"] = os.pathsep.join(p for p in path_parts if p)


_wire_dll_directories()
'@
Set-Content -LiteralPath $siteCustomizePath -Value $siteCustomizeBody -Encoding UTF8
Write-Host "  -> wrote $siteCustomizePath" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 6: validate Qt plugins.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step 6: validate PySide6 plugins ==" -ForegroundColor Cyan
$pluginsRoot = Join-Path $sitePkgs 'PySide6\plugins'
if (-not (Test-Path -LiteralPath $pluginsRoot)) {
    # PySide6 6.6+ ships the plugins under PySide6/plugins on Windows.
    throw "PySide6 plugins root missing: $pluginsRoot"
}

# Strict requirements: must be present.
$requiredPlugins = @(
    'platforms\qwindows.dll',
    'imageformats\qsvg.dll',
    'imageformats\qico.dll',
    'imageformats\qjpeg.dll',
    'iconengines\qsvgicon.dll'
)
# At least one of these style plugins must be present. Qt 6.7 renamed
# qwindowsvistastyle.dll to qmodernwindowsstyle.dll; depending on which
# version of PySide6 lands during pip install we may see either or both.
$styleAlternatives = @(
    'styles\qmodernwindowsstyle.dll',
    'styles\qwindowsvistastyle.dll'
)
$missing = @()
foreach ($rel in $requiredPlugins) {
    $abs = Join-Path $pluginsRoot $rel
    if (-not (Test-Path -LiteralPath $abs)) { $missing += $rel }
}
$haveStyle = $false
foreach ($rel in $styleAlternatives) {
    if (Test-Path -LiteralPath (Join-Path $pluginsRoot $rel)) {
        $haveStyle = $true
        break
    }
}
if (-not $haveStyle) {
    $missing += ('one of ' + ($styleAlternatives -join ' / '))
}
if ($missing) {
    throw "PySide6 plugins missing after pip install + strip:`n  - " + ($missing -join "`n  - ")
}
Write-Host "  -> all required Qt plugins present" -ForegroundColor Green

Write-Host ""
Write-Host "[build-python-tree] OK -- Python tree ready at $PyDir" -ForegroundColor Green
