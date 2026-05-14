<#
.SYNOPSIS
    Extracts the runtime payload (WeasyPrint DLLs, poppler/pdftotext.exe,
    three llama.cpp variants) into staging/app/.

.DESCRIPTION
    Reads from build-cache/payload/, populates staging/app/runtime/,
    staging/app/tools/ and staging/app/llama-variants/{cpu,cuda,vulkan}/.

    Operates idempotently: each destination is wiped and recreated to
    guarantee that stale DLLs from a previous (failed) run don't leak into
    the staging tree.

    Edge cases addressed (cf. plan §6 A1-A6, D1-D5):
      - The PyInstaller bundle inside weasyprint-windows.zip may live under
        either `_internal/` or the archive root depending on the upstream
        release. We search recursively for the whitelisted DLLs instead of
        assuming a fixed path.
      - DLL whitelist explicitly excludes the embedded Python runtime
        (python3.dll, python312.dll, _MEIxxx.dll) so that we don't ship a
        second Python next to the embeddable one.
      - Validates every expected DLL is present; aborts loudly otherwise so
        the build cannot silently produce a Setup.exe with a broken
        WeasyPrint runtime.

.PARAMETER PayloadRoot
.PARAMETER StagingRoot
.PARAMETER Lean
    Skip CUDA + Vulkan variants (matching prepare-payload.ps1 -Lean).
#>
[CmdletBinding()]
param(
    [string]$PayloadRoot,
    [string]$StagingRoot,
    [switch]$Lean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
if (-not $PayloadRoot) { $PayloadRoot = Join-Path $WindowsRoot 'build-cache\payload' }
if (-not $StagingRoot) { $StagingRoot = Join-Path $WindowsRoot 'staging' }

$AppRoot       = Join-Path $StagingRoot 'app'
$RuntimeDir    = Join-Path $AppRoot 'runtime'
$ToolsDir      = Join-Path $AppRoot 'tools'
$VariantsDir   = Join-Path $AppRoot 'llama-variants'

# ---------------------------------------------------------------------------
# Whitelists.
#
# Pango / Cairo / GLib / GDK-PixBuf / Fontconfig / Freetype + the closure of
# their direct transitive dependencies as shipped by the upstream
# weasyprint-windows.zip v68.1.
# ---------------------------------------------------------------------------
$WeasyPrintDllWhitelist = @(
    # Pango / HarfBuzz text layout.
    'libpango-1.0-0.dll',
    'libpangoft2-1.0-0.dll',
    'libpangocairo-1.0-0.dll',     # only present on legacy (<=v52) bundles
    'libpangowin32-1.0-0.dll',     # only present on legacy bundles
    'libharfbuzz-0.dll',
    'libharfbuzz-subset-0.dll',
    'libgraphite2.dll',
    # Cairo rasterisation (only present on legacy WeasyPrint bundles <=v52;
    # v53+ uses the pure-Python pydyf PDF generator and ships none of these).
    'libcairo-2.dll',
    'libcairo-gobject-2.dll',
    'libpixman-1-0.dll',
    # GLib / GObject.
    'libgobject-2.0-0.dll',
    'libglib-2.0-0.dll',
    'libgio-2.0-0.dll',
    'libgmodule-2.0-0.dll',
    'libgthread-2.0-0.dll',
    'libpcre2-8-0.dll',
    # GDK-PixBuf (image loading for HTML <img>) -- legacy only.
    'libgdk_pixbuf-2.0-0.dll',
    # Fontconfig / FreeType.
    'libfontconfig-1.dll',
    'libfreetype-6.dll',
    # FriBiDi (bidirectional text).
    'libfribidi-0.dll',
    # Thai / Indic word boundary helpers used by Pango.
    'libthai-0.dll',
    'libdatrie-1.dll',
    # Compression and helpers.
    'libpng16-16.dll',
    'libjpeg-62.dll',
    'libtiff-5.dll',
    'libwebp-7.dll',
    'libbrotlicommon.dll',
    'libbrotlidec.dll',
    'libexpat-1.dll',
    'libxml2-2.dll',
    'libiconv-2.dll',
    'libintl-8.dll',
    'libffi-8.dll',
    'libbz2-1.dll',
    'liblzma-5.dll',
    'libssl-3.dll',
    'libcrypto-3.dll',
    'libgcc_s_seh-1.dll',
    'libstdc++-6.dll',
    'libwinpthread-1.dll',
    'zlib1.dll'
)

# Whitelist for poppler-windows: pdftotext.exe + the DLLs in its bin/ dir.
$PopplerExeWhitelist = @('pdftotext.exe', 'pdfinfo.exe')

# Required DLLs that, if missing in the extracted bundle, must abort the
# build. WeasyPrint v53+ no longer needs Cairo / pangocairo / gdk-pixbuf
# because the PDF backend is now pure-Python (pydyf); only the Pango +
# Fontconfig + Harfbuzz layout chain is mandatory.
$RequiredWeasyPrintDlls = @(
    'libpango-1.0-0.dll',
    'libpangoft2-1.0-0.dll',
    'libgobject-2.0-0.dll',
    'libglib-2.0-0.dll',
    'libfontconfig-1.dll',
    'libfreetype-6.dll',
    'libharfbuzz-0.dll',
    'libpng16-16.dll',
    'zlib1.dll'
)

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
function Reset-Directory {
    param([Parameter(Mandatory)] [string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -Recurse -Force -LiteralPath $Path
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Expand-ArchiveToTemp {
    param(
        [Parameter(Mandatory)] [string]$Archive,
        [Parameter(Mandatory)] [string]$Label
    )
    $temp = Join-Path $env:TEMP ("rapack-{0}-{1}" -f $Label, ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    Write-Host "  -> expanding $Archive -> $temp" -ForegroundColor DarkGray
    Expand-Archive -LiteralPath $Archive -DestinationPath $temp -Force
    return $temp
}

function Find-FilesByName {
    param(
        [Parameter(Mandatory)] [string]$Root,
        [Parameter(Mandatory)] [string[]]$Names
    )
    $found = @{}
    Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            if ($Names -contains $_.Name) {
                # Always keep the first occurrence; bundles never ship the
                # same DLL twice in materially-different versions.
                if (-not $found.ContainsKey($_.Name)) {
                    $found[$_.Name] = $_.FullName
                }
            }
        }
    return $found
}

function Invoke-PyInstallerOnefileScrape {
    <#
        Recent WeasyPrint windows.zip releases (>=v66) ship a PyInstaller
        --onefile self-extracting executable instead of a multi-file --onedir
        layout. The bootloader unpacks all DLLs / data into a transient
        %TEMP%\_MEIxxxxxx directory and removes it on process exit. To recover
        the bundled DLLs we launch the executable with stdin held open (so the
        process blocks reading HTML from stdin) and copy the DLLs out of the
        live _MEI directory before forcibly terminating the child.
    #>
    param(
        [Parameter(Mandatory)] [string]$Exe,
        [Parameter(Mandatory)] [string]$DestRoot
    )
    New-Item -ItemType Directory -Force -Path $DestRoot | Out-Null

    $tempRoot = $env:TEMP
    $preExisting = New-Object System.Collections.Generic.HashSet[string]
    Get-ChildItem -LiteralPath $tempRoot -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
        ForEach-Object { [void]$preExisting.Add($_.Name) }

    $tmpPdf = Join-Path $env:TEMP ('rapack-weasy-noop-{0}.pdf' -f ([guid]::NewGuid().ToString('N').Substring(0,8)))
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    # weasyprint reads HTML from stdin when the input arg is "-"; we feed an
    # output path it will never reach so the process blocks on stdin EOF.
    $psi.Arguments = ('- "{0}"' -f $tmpPdf)
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true

    Write-Host "  -> launching $Exe to extract _MEI bootloader payload" -ForegroundColor DarkGray
    $proc = [System.Diagnostics.Process]::Start($psi)
    try {
        $meiDir = $null
        # PyInstaller's bootloader writes ~28 MB of payload into _MEI in
        # several discrete passes; we have to wait for *all* of it before
        # snapshotting, otherwise we copy a half-populated tree and the
        # required DLL check fails downstream.
        #
        # Strategy:
        #   1. Find the new _MEI* dir (~50-200 ms after launch).
        #   2. Poll until both libcairo-2.dll *and* libpango-1.0-0.dll appear
        #      (these are toward the end of the extraction order in v68.x).
        #   3. Wait one more 250 ms tick for stragglers (gdk-pixbuf loader
        #      cache regen, fontconfig caches, etc.).
        for ($i = 0; $i -lt 200 -and -not $meiDir -and -not $proc.HasExited; $i++) {
            Start-Sleep -Milliseconds 50
            $cand = Get-ChildItem -LiteralPath $tempRoot -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
                    Where-Object { -not $preExisting.Contains($_.Name) } |
                    Select-Object -First 1
            if ($cand) {
                # Wait for two of the *last-extracted* large DLLs to land so
                # that we don't snapshot a half-populated tree. python313.dll
                # is the heaviest single artefact (~6 MB) in v68; libpango is
                # near the top of the alphabetised PyInstaller TOC and
                # provides a second readiness signal.
                $pangoSrc = Join-Path $cand.FullName 'libpango-1.0-0.dll'
                $pythonSrc = Join-Path $cand.FullName 'python313.dll'
                $altPython = Get-ChildItem -LiteralPath $cand.FullName -Filter 'python3*.dll' -File -ErrorAction SilentlyContinue | Select-Object -First 1
                if ((Test-Path -LiteralPath $pangoSrc) -and (((Test-Path -LiteralPath $pythonSrc)) -or $altPython)) {
                    $meiDir = $cand
                }
            }
        }
        if (-not $meiDir) {
            throw "PyInstaller _MEI directory did not finish populating under $tempRoot within 10s for $Exe"
        }
        Start-Sleep -Milliseconds 250
        Write-Host "  -> snapshotting $($meiDir.FullName)" -ForegroundColor DarkGray
        # Copy the entire _MEI tree -- DLLs, data subdirectories, fontconfig
        # caches and gdk-pixbuf loader caches all live under this root and
        # are needed downstream. Get-ChildItem + Copy-Item -LiteralPath in a
        # ForEach gives us deterministic per-entry copies (Copy-Item doesn't
        # expand wildcards under -LiteralPath).
        Get-ChildItem -LiteralPath $meiDir.FullName -Force |
            ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $DestRoot $_.Name) -Recurse -Force
            }
    }
    finally {
        if (-not $proc.HasExited) {
            try { $proc.Kill() } catch {}
        }
        try { $proc.WaitForExit(5000) | Out-Null } catch {}
        try { $proc.Dispose() } catch {}
    }
}

# ---------------------------------------------------------------------------
# Step (a): WeasyPrint runtime DLLs.
# ---------------------------------------------------------------------------
Write-Host "== Step (a) WeasyPrint runtime ==" -ForegroundColor Cyan
$weasyZip = Join-Path $PayloadRoot 'weasyprint-windows.zip'
if (-not (Test-Path -LiteralPath $weasyZip)) {
    throw "Missing payload artefact: $weasyZip. Run prepare-payload.ps1 first."
}
Reset-Directory -Path $RuntimeDir

$weasyTemp = Expand-ArchiveToTemp -Archive $weasyZip -Label 'weasy'
try {
    $dllMap = Find-FilesByName -Root $weasyTemp -Names $WeasyPrintDllWhitelist

    $missing = $RequiredWeasyPrintDlls | Where-Object { -not $dllMap.ContainsKey($_) }
    if ($missing) {
        # Upstream switched to PyInstaller --onefile mode at some release in
        # the v6x series, which means the zip only contains the bootloader
        # exe (no DLLs at the top level). Detect this and unpack the
        # self-extracting payload at runtime via _MEI snapshotting.
        $weasyExe = Get-ChildItem -LiteralPath $weasyTemp -Recurse -File -Filter 'weasyprint.exe' -ErrorAction SilentlyContinue |
                    Select-Object -First 1
        if ($weasyExe) {
            Write-Host "  -> top-level DLL search found nothing; treating $($weasyExe.Name) as PyInstaller --onefile" -ForegroundColor DarkYellow
            $unpacked = Join-Path $weasyTemp '_unpacked_onefile'
            Invoke-PyInstallerOnefileScrape -Exe $weasyExe.FullName -DestRoot $unpacked
            $dllMap = Find-FilesByName -Root $unpacked -Names $WeasyPrintDllWhitelist
            $missing = $RequiredWeasyPrintDlls | Where-Object { -not $dllMap.ContainsKey($_) }
        }
    }
    if ($missing) {
        throw "WeasyPrint bundle is missing required DLLs: $($missing -join ', ')"
    }

    foreach ($name in $dllMap.Keys) {
        Copy-Item -LiteralPath $dllMap[$name] -Destination (Join-Path $RuntimeDir $name) -Force
    }
    Write-Host "  -> copied $($dllMap.Count) DLL(s) to $RuntimeDir" -ForegroundColor Green

    # Copy the loader/font support trees if present. WeasyPrint bundles
    # gdk-pixbuf-2.0/ (image loaders) and either etc/fonts/ or share/fonts/
    # depending on the upstream release.
    foreach ($subdir in @('gdk-pixbuf-2.0', 'fonts', 'etc', 'share\fontconfig', 'share\fonts', 'lib\gdk-pixbuf-2.0', 'girepository-1.0')) {
        $candidates = Get-ChildItem -LiteralPath $weasyTemp -Recurse -Directory -ErrorAction SilentlyContinue |
                      Where-Object { $_.FullName.ToLowerInvariant().EndsWith('\' + $subdir.ToLowerInvariant()) }
        foreach ($cand in $candidates) {
            $rel = $subdir
            $destSub = Join-Path $RuntimeDir $rel
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destSub) | Out-Null
            Write-Host "  -> copying support tree: $rel" -ForegroundColor DarkGray
            Copy-Item -LiteralPath $cand.FullName -Destination $destSub -Recurse -Force
            break
        }
    }
} finally {
    Remove-Item -Recurse -Force -LiteralPath $weasyTemp -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Step (b): poppler / pdftotext.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step (b) poppler tools ==" -ForegroundColor Cyan
$popplerZip = Join-Path $PayloadRoot 'Release-24.08.0-0.zip'
if (-not (Test-Path -LiteralPath $popplerZip)) {
    throw "Missing payload artefact: $popplerZip"
}
Reset-Directory -Path $ToolsDir

$popplerTemp = Expand-ArchiveToTemp -Archive $popplerZip -Label 'poppler'
try {
    $popplerBin = Get-ChildItem -LiteralPath $popplerTemp -Recurse -Directory -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -eq 'bin' -and (Test-Path (Join-Path $_.FullName 'pdftotext.exe')) } |
                  Select-Object -First 1
    if (-not $popplerBin) {
        throw "Could not locate poppler bin/ directory inside $popplerZip"
    }
    Write-Host "  -> poppler bin found at $($popplerBin.FullName)" -ForegroundColor DarkGray

    # Whitelisted .exe (only the tools we actually call from Python).
    foreach ($exeName in $PopplerExeWhitelist) {
        $src = Join-Path $popplerBin.FullName $exeName
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $ToolsDir $exeName) -Force
        }
    }
    # Companion DLLs from the same bin/ - poppler ships a curated, mostly
    # self-contained set there.
    Get-ChildItem -LiteralPath $popplerBin.FullName -Filter '*.dll' -File -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ToolsDir $_.Name) -Force }

    if (-not (Test-Path (Join-Path $ToolsDir 'pdftotext.exe'))) {
        throw "pdftotext.exe missing after extracting poppler"
    }
    Write-Host "  -> copied pdftotext.exe + companion DLLs to $ToolsDir" -ForegroundColor Green
} finally {
    Remove-Item -Recurse -Force -LiteralPath $popplerTemp -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Step (c): llama.cpp variants.
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== Step (c) llama.cpp variants ==" -ForegroundColor Cyan
$variantSpec = @(
    @{ Name = 'cpu';    AlwaysInstall = $true },
    @{ Name = 'cuda';   AlwaysInstall = $false },
    @{ Name = 'vulkan'; AlwaysInstall = $false }
)

foreach ($v in $variantSpec) {
    $vname = $v.Name
    if (-not $v.AlwaysInstall -and $Lean) {
        Write-Host "  -> $vname [SKIPPED via -Lean]" -ForegroundColor DarkYellow
        continue
    }
    $variantSrcDir = Join-Path $PayloadRoot ('llama-cpp\' + $vname)
    if (-not (Test-Path -LiteralPath $variantSrcDir)) {
        if ($v.AlwaysInstall) {
            throw "Required llama.cpp $vname variant directory missing: $variantSrcDir"
        }
        Write-Host "  -> $vname [no payload available; skip]" -ForegroundColor DarkYellow
        continue
    }

    $variantZip = Get-ChildItem -LiteralPath $variantSrcDir -Filter '*.zip' -File -ErrorAction SilentlyContinue |
                  Select-Object -First 1
    if (-not $variantZip) {
        throw "No zip found under $variantSrcDir"
    }

    $destVariant = Join-Path $VariantsDir $vname
    Reset-Directory -Path $destVariant

    Write-Host "  -> $vname : extracting $($variantZip.Name)" -ForegroundColor DarkGray
    $tmp = Expand-ArchiveToTemp -Archive $variantZip.FullName -Label "llama-$vname"
    try {
        $serverExe = Get-ChildItem -LiteralPath $tmp -Recurse -File -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -eq 'llama-server.exe' } | Select-Object -First 1
        if (-not $serverExe) {
            throw "llama-server.exe not found inside $($variantZip.Name)"
        }
        $binDir = Split-Path -Parent $serverExe.FullName
        Get-ChildItem -LiteralPath $binDir -File -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destVariant $_.Name) -Force }
        # Any sibling directories (e.g. ggml-cuda/, helpers/) -- preserve.
        Get-ChildItem -LiteralPath $binDir -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destVariant $_.Name) -Recurse -Force }

        if (-not (Test-Path (Join-Path $destVariant 'llama-server.exe'))) {
            throw "llama-server.exe missing after extracting $vname"
        }
        Write-Host "  -> $vname : OK ($destVariant)" -ForegroundColor Green
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "[extract-runtime] OK" -ForegroundColor Green
