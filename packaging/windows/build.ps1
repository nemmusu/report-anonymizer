<#
.SYNOPSIS
    Master orchestrator for the Report Anonymizer Windows installer build.

.DESCRIPTION
    Runs the full Phase 2 pipeline end-to-end:

      1. bootstrap-tools.ps1      (7zr + Inno Setup + MinGW into build-cache/tools)
      2. prepare-payload.ps1      (Python embed + WeasyPrint + poppler + llama variants)
      3. extract-runtime.ps1      (cherry-pick DLLs + pdftotext + 3x llama-server)
      4. convert-assets.ps1       (app_icon.svg -> app_icon.ico)
      5. build-python-tree.ps1    (extract embed + pip install + strip)
      6. freeze-lockfile.ps1      (pip freeze -> requirements-lock-windows.txt)
      7. compile-launchers.ps1    (windres + gcc -> ReportAnonymizer.exe + CLI)
      8. stage-app.ps1            (assemble staging\app\repo + launcher)
      9. smoke-test.ps1           (battery of post-stage validations)
     10. ISCC compile             (-> dist\Report-Anonymizer-Setup-x64-<ver>.exe)

    Standard guarantees:
      - idempotent: every step short-circuits when its outputs are up to date.
      - lockfile: build-cache\.build.lock prevents two concurrent builds.
      - pre-flight: aborts if less than 3 GB of free disk on the build drive.
      - SHA256 + size report printed at the end.

.PARAMETER Version
    Optional version override (e.g. "1.0.1-rc1"). Defaults to the value of
    pyproject.toml:7 in the repository root.

.PARAMETER Clean
    Wipe build-cache\ and dist\ before starting. Implies a cold rebuild
    (~18 min for the default build, ~12 min with -Lean).

.PARAMETER SkipBootstrap
    Skip step 1 (tooling bootstrap). Use this in CI where the tools are
    already cached.

.PARAMETER Lean
    Drop the CUDA + Vulkan llama.cpp variants. Produces
    Report-Anonymizer-Setup-x64-<ver>-lean.exe at ~225 MB instead of ~405 MB.

.EXAMPLE
    pwsh -ExecutionPolicy Bypass -File .\build.ps1 -Version 1.0.0 -Clean
#>
[CmdletBinding()]
param(
    [string]$Version,
    [switch]$Clean,
    [switch]$SkipBootstrap,
    [switch]$Lean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$WindowsRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $WindowsRoot)
$ScriptsDir  = Join-Path $WindowsRoot 'scripts'
$CacheRoot   = Join-Path $WindowsRoot 'build-cache'
$StagingRoot = Join-Path $WindowsRoot 'staging'
$DistRoot    = Join-Path $WindowsRoot 'dist'
$InnoScript  = Join-Path $WindowsRoot 'inno\ReportAnonymizer.iss'
$LockFile    = Join-Path $CacheRoot '.build.lock'

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
function Write-Section($title) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Write-Host ("== {0}" -f $title) -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor Cyan
}

function Resolve-Version() {
    if ($Version) { return $Version }
    $pyproject = Join-Path $RepoRoot 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyproject)) {
        throw "Cannot locate pyproject.toml in repo root to infer version."
    }
    $m = Select-String -LiteralPath $pyproject -Pattern '^version\s*=\s*"(?<v>[^"]+)"' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $m) { throw "Could not parse version from $pyproject" }
    return $m.Matches[0].Groups['v'].Value
}

function Acquire-BuildLock() {
    if (-not (Test-Path -LiteralPath $CacheRoot)) {
        New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    }
    if (Test-Path -LiteralPath $LockFile) {
        $other = Get-Content -LiteralPath $LockFile -Raw
        $otherPid = ($other -split '\s+')[0]
        if ($otherPid -and ($otherPid -match '^\d+$')) {
            $proc = Get-Process -Id ([int]$otherPid) -ErrorAction SilentlyContinue
            if ($proc) {
                throw "Another build is already running (PID $otherPid). Lock: $LockFile"
            }
        }
        Write-Warning "Stale build lock found; reclaiming it."
        Remove-Item -LiteralPath $LockFile -Force
    }
    "$PID $(Get-Date -Format o)" | Set-Content -LiteralPath $LockFile -Encoding ASCII
}

function Release-BuildLock() {
    if (Test-Path -LiteralPath $LockFile) {
        try { Remove-Item -LiteralPath $LockFile -Force } catch {}
    }
}

function Invoke-Step([string]$Title, [string]$Script, [hashtable]$Params) {
    Write-Section $Title
    $path = Join-Path $ScriptsDir $Script
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Step script missing: $path"
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # Reset $LASTEXITCODE so the post-call check doesn't trip Set-StrictMode
    # when a sub-script never invokes a native command.
    $global:LASTEXITCODE = 0
    & $path @Params
    if ($LASTEXITCODE -ne 0) { throw "Step '$Title' failed (exit $LASTEXITCODE)" }
    $sw.Stop()
    Write-Host ("--> {0} completed in {1:N1}s" -f $Title, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Pre-flight.
# ---------------------------------------------------------------------------
Write-Section "Pre-flight"
$Version = Resolve-Version
Write-Host "Version            : $Version"
Write-Host "Repo root          : $RepoRoot"
Write-Host "Build cache        : $CacheRoot"
Write-Host "Staging            : $StagingRoot"
Write-Host "Dist               : $DistRoot"
Write-Host "Lean build         : $Lean"
Write-Host "Skip bootstrap     : $SkipBootstrap"
Write-Host "Clean              : $Clean"

$RequiredFreeBytes = 3GB
$disk = (Get-PSDrive -Name (Split-Path -Path $WindowsRoot -Qualifier).TrimEnd(':') -PSProvider FileSystem -ErrorAction SilentlyContinue)
if ($disk -and ($disk.Free -lt $RequiredFreeBytes)) {
    throw ("Insufficient free disk space: {0:N1} GB available on {1}: , {2:N1} GB required." -f ($disk.Free/1GB), $disk.Name, ($RequiredFreeBytes/1GB))
}

if ($Clean) {
    Write-Section "Clean: wiping build-cache + staging + dist"
    foreach ($p in @($CacheRoot, $StagingRoot, $DistRoot)) {
        if (Test-Path -LiteralPath $p) {
            Remove-Item -Recurse -Force -LiteralPath $p
        }
    }
}

foreach ($p in @($CacheRoot, $StagingRoot, $DistRoot)) {
    if (-not (Test-Path -LiteralPath $p)) {
        New-Item -ItemType Directory -Force -Path $p | Out-Null
    }
}

Acquire-BuildLock

try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    # -----------------------------------------------------------------------
    # 1. Toolchain bootstrap.
    # -----------------------------------------------------------------------
    if (-not $SkipBootstrap) {
        Invoke-Step -Title 'Step 1/10: bootstrap-tools'   -Script 'bootstrap-tools.ps1'   -Params @{ }
    } else {
        Write-Host "Step 1/10: bootstrap-tools SKIPPED via -SkipBootstrap" -ForegroundColor DarkYellow
    }

    # -----------------------------------------------------------------------
    # 2. Payload download.
    # -----------------------------------------------------------------------
    $payloadParams = @{}
    if ($Lean) { $payloadParams['Lean'] = $true }
    Invoke-Step -Title 'Step 2/10: prepare-payload' -Script 'prepare-payload.ps1' -Params $payloadParams

    # -----------------------------------------------------------------------
    # 3. Extract runtime + variants.
    # -----------------------------------------------------------------------
    $extractParams = @{}
    if ($Lean) { $extractParams['Lean'] = $true }
    Invoke-Step -Title 'Step 3/10: extract-runtime' -Script 'extract-runtime.ps1' -Params $extractParams

    # -----------------------------------------------------------------------
    # 4. Asset conversion (svg -> ico).
    # -----------------------------------------------------------------------
    Invoke-Step -Title 'Step 4/10: convert-assets' -Script 'convert-assets.ps1' -Params @{}

    # -----------------------------------------------------------------------
    # 5. Build Python tree.
    # -----------------------------------------------------------------------
    Invoke-Step -Title 'Step 5/10: build-python-tree' -Script 'build-python-tree.ps1' -Params @{}

    # -----------------------------------------------------------------------
    # 6. Freeze lockfile.
    # -----------------------------------------------------------------------
    Invoke-Step -Title 'Step 6/10: freeze-lockfile' -Script 'freeze-lockfile.ps1' -Params @{}

    # -----------------------------------------------------------------------
    # 7. Compile launchers.
    # -----------------------------------------------------------------------
    Invoke-Step -Title 'Step 7/10: compile-launchers' -Script 'compile-launchers.ps1' -Params @{ Version = $Version }

    # -----------------------------------------------------------------------
    # 8. Stage app tree.
    # -----------------------------------------------------------------------
    $stageParams = @{}
    if ($Lean) { $stageParams['Lean'] = $true }
    Invoke-Step -Title 'Step 8/10: stage-app' -Script 'stage-app.ps1' -Params $stageParams

    # -----------------------------------------------------------------------
    # 9. Smoke test.
    # -----------------------------------------------------------------------
    $smokeParams = @{}
    if ($Lean) { $smokeParams['Lean'] = $true }
    Invoke-Step -Title 'Step 9/10: smoke-test' -Script 'smoke-test.ps1' -Params $smokeParams

    # -----------------------------------------------------------------------
    # 10. ISCC compile.
    # -----------------------------------------------------------------------
    Write-Section 'Step 10/10: ISCC compile'
    # Resolution order:
    #   1. Bootstrap-managed portable Inno Setup (preferred -- pins the
    #      version the project tested against).
    #   2. Inno Setup pre-installed by the Windows runner image
    #      (``windows-latest`` ships 6.x at
    #      ``C:\Program Files (x86)\Inno Setup 6\ISCC.exe``); we fall
    #      back to it so the build survives a bootstrap that produced
    #      a 0-exit code but didn't actually drop the binary -- this
    #      bites on shared CI runners where ``/VERYSILENT /DIR=`` can
    #      no-op silently.
    #   3. ``ISCC`` / ``iscc`` on PATH (developer machines).
    $iscc = Join-Path $CacheRoot 'tools\innosetup\ISCC.exe'
    if (-not (Test-Path -LiteralPath $iscc)) {
        $fallbackCandidates = @(
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
            'C:\Program Files\Inno Setup 6\ISCC.exe'
        )
        $iscc = $null
        foreach ($cand in $fallbackCandidates) {
            if (Test-Path -LiteralPath $cand) {
                Write-Warning "Bootstrap ISCC.exe missing; falling back to $cand"
                $iscc = $cand
                break
            }
        }
        if (-not $iscc) {
            $pathCmd = Get-Command 'ISCC' -ErrorAction SilentlyContinue
            if (-not $pathCmd) { $pathCmd = Get-Command 'iscc' -ErrorAction SilentlyContinue }
            if ($pathCmd) {
                Write-Warning "Bootstrap ISCC.exe missing; falling back to $($pathCmd.Source)"
                $iscc = $pathCmd.Source
            }
        }
        if (-not $iscc) {
            throw "ISCC.exe not found at $(Join-Path $CacheRoot 'tools\innosetup\ISCC.exe'), nor pre-installed on the runner. Run bootstrap-tools.ps1 first."
        }
    }
    if (-not (Test-Path -LiteralPath $InnoScript)) {
        throw "Inno script missing: $InnoScript"
    }

    $baseName = "Report-Anonymizer-Setup-x64-$Version"
    if ($Lean) { $baseName += '-lean' }

    $defArgs = @(
        "/DMyAppVersion=$Version",
        "/DStagingDir=$StagingRoot",
        "/DOutputDir=$DistRoot",
        "/DOutputBaseFilename=$baseName"
    )
    if ($Lean) {
        $defArgs += @('/DIncludeCuda=0', '/DIncludeVulkan=0')
    } else {
        $defArgs += @('/DIncludeCuda=1', '/DIncludeVulkan=1')
    }

    Write-Host "ISCC.exe $($defArgs -join ' ') $InnoScript" -ForegroundColor DarkGray
    & $iscc @defArgs $InnoScript
    if ($LASTEXITCODE -ne 0) {
        throw "ISCC.exe failed (exit $LASTEXITCODE)"
    }

    $finalExe = Join-Path $DistRoot ($baseName + '.exe')
    if (-not (Test-Path -LiteralPath $finalExe)) {
        throw "Expected Setup.exe missing after ISCC: $finalExe"
    }

    # -----------------------------------------------------------------------
    # Final report.
    # -----------------------------------------------------------------------
    $sw.Stop()
    $size = (Get-Item -LiteralPath $finalExe).Length
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $finalExe).Hash

    Write-Section 'BUILD OK'
    Write-Host ("Output  : {0}" -f $finalExe)
    Write-Host ("Size    : {0:N1} MB ({1:N0} bytes)" -f ($size/1MB), $size)
    Write-Host ("SHA256  : {0}" -f $hash)
    Write-Host ("Elapsed : {0:N1} minutes" -f $sw.Elapsed.TotalMinutes)
    Write-Host ""

    $reportPath = Join-Path $DistRoot ($baseName + '.sha256.txt')
    "{0} *{1}" -f $hash.ToLower(), (Split-Path -Leaf $finalExe) | Set-Content -LiteralPath $reportPath -Encoding ASCII
    Write-Host "SHA256 manifest: $reportPath" -ForegroundColor DarkGray
}
finally {
    Release-BuildLock
}
