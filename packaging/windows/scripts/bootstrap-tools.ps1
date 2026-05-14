<#
.SYNOPSIS
    Bootstraps portable build tools into packaging/windows/build-cache/tools/.

.DESCRIPTION
    Downloads (idempotently) the three pieces of toolchain needed to build the
    Windows installer without touching the host system:

      1. 7zr.exe                        - 7-Zip standalone, used to extract
                                          Inno Setup's NSIS-style installer.
      2. Inno Setup 6.4.0 (portable)    - provides ISCC.exe used in §2.9.
      3. MinGW-w64 (WinLibs UCRT)       - provides gcc.exe + windres.exe used
                                          to compile the C launchers in §2.5.

    Every download is SHA256-verified against a hard-coded hashtable. The
    script is idempotent: artefacts already present with a matching hash are
    skipped.

.PARAMETER ToolsRoot
    Optional override for the destination root. Defaults to
    "<repo>/packaging/windows/build-cache/tools/".

.PARAMETER Force
    Force redownload even when the local file matches the expected hash.

.PARAMETER SkipHashVerify
    Skip the SHA256 verification step. Intended for one-shot bootstrap when
    pinning new hashes (the script will then print the observed hashes so the
    developer can paste them back into $KnownHashes). DO NOT USE for routine
    builds.

.EXAMPLE
    pwsh -ExecutionPolicy Bypass -File .\scripts\bootstrap-tools.ps1
#>
[CmdletBinding()]
param(
    [string]$ToolsRoot,
    [switch]$Force,
    [switch]$SkipHashVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Resolve paths relative to this script regardless of $PWD.
# ---------------------------------------------------------------------------
$ScriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot     = Split-Path -Parent $ScriptDir
if (-not $ToolsRoot) {
    $ToolsRoot = Join-Path $WindowsRoot 'build-cache\tools'
}
$DownloadsRoot   = Join-Path $WindowsRoot 'build-cache\downloads'

New-Item -ItemType Directory -Force -Path $ToolsRoot, $DownloadsRoot | Out-Null

# ---------------------------------------------------------------------------
# Pinned upstream artefacts.
#
# IMPORTANT (Worker C):
#   The SHA256 values below are pinned to the upstream releases that were the
#   current "stable" picks at plan time (2026-Q2). When -SkipHashVerify is
#   used on a fresh machine, observed hashes are PRINTED so they can be
#   pasted back here and re-verified. Never run a production build with
#   -SkipHashVerify enabled.
# ---------------------------------------------------------------------------
$Artefacts = @(
    @{
        Name      = '7zr.exe'
        Url       = 'https://www.7-zip.org/a/7zr.exe'
        # 7-Zip ships ``7zr.exe`` under an unversioned URL and reuploads
        # it as new builds land, so this hash needs a refresh whenever
        # the upstream binary changes. Observed on 2026-05-12 — bump on
        # the next SHA256 mismatch.
        Sha256    = 'ABCF64AE1CBAFDDB5395E4CDD3BDC7E3E0561D54A0C6380E3DD43BDBFFE519A2'
        SaveAs    = '7zr.exe'
        Extract   = $false
        ToolPath  = '7zr.exe'   # final location under $ToolsRoot
    },
    @{
        Name          = 'innosetup-6.7.1.exe'
        Url           = 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_1/innosetup-6.7.1.exe'
        Sha256        = '4D11E8050B6185E0D49BD9E8CC661A7A59F44959A621D31D11033124C4E8A7B0'
        SaveAs        = 'innosetup-6.7.1.exe'
        Extract       = $true
        ExtractTo     = 'innosetup'
        ExtractMethod = 'iss-silent'  # Modern Inno Setup self-installers (>=6.5) are no
                                      # longer NSIS-style archives, so 7zr cannot crack
                                      # them. Run the installer with /VERYSILENT /DIR=
                                      # /CURRENTUSER instead -- writes only the portable
                                      # binaries we need under build-cache\tools.
        ToolPath      = 'innosetup\ISCC.exe'
    },
    @{
        Name      = 'mingw-w64 (winlibs UCRT)'
        Url       = 'https://github.com/brechtsanders/winlibs_mingw/releases/download/15.2.0posix-14.0.0-ucrt-r7/winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64ucrt-14.0.0-r7.zip'
        Sha256    = 'CB2FBAD6162540CDF5E1FACDCE08D4DAC359E8CF64F7F696A99274291763B815'
        SaveAs    = 'winlibs-mingw-w64ucrt.zip'
        Extract   = $true
        ExtractTo = '.'   # zip already contains a mingw64/ top-level directory
        ToolPath  = 'mingw64\bin\gcc.exe'
    }
)

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
function Invoke-DownloadWithRetry {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [Parameter(Mandatory)] [string]$Destination,
        [int]$MaxAttempts = 3
    )
    $delays = @(5, 15, 45)
    for ($i = 0; $i -lt $MaxAttempts; $i++) {
        try {
            Write-Host "  -> downloading $Url" -ForegroundColor DarkGray
            # -UseBasicParsing keeps the call compatible with Server Core /
            # PowerShell 5.1 hosts without IE engine.
            Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -ErrorAction Stop
            return
        } catch {
            $wait = $delays[[Math]::Min($i, $delays.Length - 1)]
            Write-Warning ("Download attempt {0}/{1} failed: {2}. Retrying in {3}s..." -f ($i + 1), $MaxAttempts, $_.Exception.Message, $wait)
            Start-Sleep -Seconds $wait
        }
    }
    throw "Failed to download $Url after $MaxAttempts attempts."
}

function Test-Sha256 {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$ExpectedHash
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $observed = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
    return ($observed -eq $ExpectedHash.ToUpperInvariant())
}

function Invoke-7zExtract {
    param(
        [Parameter(Mandatory)] [string]$Sevenzr,
        [Parameter(Mandatory)] [string]$Archive,
        [Parameter(Mandatory)] [string]$DestDir
    )
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    Write-Host "  -> extracting $(Split-Path -Leaf $Archive) via 7zr -> $DestDir" -ForegroundColor DarkGray
    # -y       : assume yes to all queries
    # -bso0    : silence stdout
    # -bsp0    : silence progress (we already have our own messages)
    & $Sevenzr x -y "-o$DestDir" $Archive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "7zr failed extracting $Archive (exit $LASTEXITCODE)"
    }
}

function Invoke-ZipExtract {
    param(
        [Parameter(Mandatory)] [string]$Archive,
        [Parameter(Mandatory)] [string]$DestDir
    )
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    Write-Host "  -> Expand-Archive $(Split-Path -Leaf $Archive) -> $DestDir" -ForegroundColor DarkGray
    Expand-Archive -LiteralPath $Archive -DestinationPath $DestDir -Force
}

function Invoke-InnoSilentInstall {
    param(
        [Parameter(Mandatory)] [string]$Installer,
        [Parameter(Mandatory)] [string]$DestDir
    )
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    Write-Host "  -> running $(Split-Path -Leaf $Installer) /VERYSILENT /DIR=$DestDir" -ForegroundColor DarkGray
    $absDest = (Resolve-Path -LiteralPath $DestDir).Path
    $proc = Start-Process -FilePath $Installer `
                          -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/CURRENTUSER',"/DIR=$absDest") `
                          -PassThru -Wait
    if ($proc.ExitCode -ne 0) {
        throw "Inno Setup self-installer failed (exit $($proc.ExitCode))"
    }
}

# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------
Write-Host "[bootstrap-tools] ToolsRoot = $ToolsRoot"
Write-Host "[bootstrap-tools] DownloadsRoot = $DownloadsRoot"

foreach ($a in $Artefacts) {
    $name      = $a.Name
    $download  = Join-Path $DownloadsRoot $a.SaveAs
    $finalTool = Join-Path $ToolsRoot $a.ToolPath

    Write-Host ""
    Write-Host "== $name ==" -ForegroundColor Cyan

    # Quick path: the final tool already exists, and the cached archive (if
    # any) still matches the expected hash. Skip everything.
    if ((-not $Force) -and (Test-Path -LiteralPath $finalTool)) {
        if ($SkipHashVerify -or (Test-Sha256 -Path $download -ExpectedHash $a.Sha256)) {
            Write-Host "  -> already present at $finalTool (skip)" -ForegroundColor Green
            continue
        }
    }

    # Download (or reuse a matching cached file).
    if ($Force -or -not (Test-Path -LiteralPath $download) -or -not (Test-Sha256 -Path $download -ExpectedHash $a.Sha256)) {
        if (Test-Path -LiteralPath $download) {
            Remove-Item -LiteralPath $download -Force
        }
        Invoke-DownloadWithRetry -Url $a.Url -Destination $download
    } else {
        Write-Host "  -> using cached $($a.SaveAs)" -ForegroundColor DarkGray
    }

    # Verify SHA256.
    $observedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash.ToUpperInvariant()
    if ($SkipHashVerify -or $a.Sha256 -like 'TODO_*') {
        Write-Warning "SHA256 verification skipped for '$name'. Observed: $observedHash"
        Write-Warning "  -> paste this hash into `$Artefacts.Sha256 to pin the artefact."
    } elseif ($observedHash -ne $a.Sha256.ToUpperInvariant()) {
        throw "SHA256 mismatch for $name`n  expected: $($a.Sha256)`n  observed: $observedHash"
    } else {
        Write-Host "  -> SHA256 OK ($observedHash)" -ForegroundColor Green
    }

    # Place the artefact under $ToolsRoot.
    if (-not $a.Extract) {
        Copy-Item -LiteralPath $download -Destination $finalTool -Force
        Write-Host "  -> installed $finalTool" -ForegroundColor Green
        continue
    }

    $extractDest = Join-Path $ToolsRoot $a.ExtractTo
    if (Test-Path -LiteralPath $extractDest) {
        Remove-Item -Recurse -Force -LiteralPath $extractDest
    }
    New-Item -ItemType Directory -Force -Path $extractDest | Out-Null

    $extractMethod = if ($a.ContainsKey('ExtractMethod')) { $a.ExtractMethod } else { 'auto' }
    switch ($extractMethod) {
        'iss-silent' {
            Invoke-InnoSilentInstall -Installer $download -DestDir $extractDest
        }
        default {
            if ($download.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) {
                Invoke-ZipExtract -Archive $download -DestDir $extractDest
            } else {
                # Legacy NSIS-style installers can still be cracked with 7zr.
                $sevenzr = Join-Path $ToolsRoot '7zr.exe'
                if (-not (Test-Path -LiteralPath $sevenzr)) {
                    throw "7zr.exe must be bootstrapped before $name."
                }
                Invoke-7zExtract -Sevenzr $sevenzr -Archive $download -DestDir $extractDest
            }
        }
    }

    if (-not (Test-Path -LiteralPath $finalTool)) {
        throw "Expected tool not found after extraction: $finalTool"
    }
    Write-Host "  -> installed $finalTool" -ForegroundColor Green
}

Write-Host ""
Write-Host "[bootstrap-tools] OK -- ISCC, gcc, windres ready under $ToolsRoot" -ForegroundColor Green
