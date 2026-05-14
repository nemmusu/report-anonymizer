<#
.SYNOPSIS
    Downloads upstream payload artefacts into build-cache/payload/.

.DESCRIPTION
    Fetches the six (or three with -Lean) binary artefacts that end up inside
    the final Setup.exe:

      1. Python 3.12 embeddable amd64 zip                       (~12 MB)
      2. weasyprint-windows.zip v68.1                           (~28 MB)
      3. poppler-windows Release-24.08.0-0.zip                  (~20 MB)
      4. llama-cpp (CPU AVX2) - llama-b<NNN>-bin-win-cpu-x64    (~10 MB)
      5. llama-cpp (CUDA 12.x) - llama-b<NNN>-bin-win-cuda-cu12 (~150 MB)  [skipped with -Lean]
      6. llama-cpp (Vulkan)    - llama-b<NNN>-bin-win-vulkan    (~30 MB)   [skipped with -Lean]

    Each download is verified against a pinned SHA256, retried on transient
    network failures, and cached so that subsequent invocations skip what is
    already on disk and matches.

    The llama.cpp release tag is resolved at runtime via the GitHub API
    (`gh api repos/ggml-org/llama.cpp/releases/latest`) so that we always pin
    to a real, currently-published build; the resolved tag is then frozen in
    build-cache/payload/llama-cpp/.tag so that subsequent rebuilds use the
    same binaries. Pass -LlamaTag to override.

.PARAMETER PayloadRoot
    Optional override for build-cache/payload/.

.PARAMETER Lean
    Skip the CUDA and Vulkan llama.cpp variants. Produces a ~225 MB Setup
    instead of the default ~405 MB.

.PARAMETER LlamaTag
    Explicit llama.cpp release tag (e.g. 'b6789'). If omitted, the latest
    release tag is queried via `gh api` (preferred) or the GitHub REST API
    via Invoke-RestMethod (fallback). The resolved tag is cached in
    payload/llama-cpp/.tag and reused on subsequent runs.

.PARAMETER Force
    Force redownload even when local files match expected hashes.

.PARAMETER SkipHashVerify
    Skip SHA256 verification. Prints observed hashes so the developer can
    update the pinned values. DO NOT USE for production builds.
#>
[CmdletBinding()]
param(
    [string]$PayloadRoot,
    [switch]$Lean,
    [string]$LlamaTag,
    [switch]$Force,
    [switch]$SkipHashVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths.
# ---------------------------------------------------------------------------
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
if (-not $PayloadRoot) {
    $PayloadRoot = Join-Path $WindowsRoot 'build-cache\payload'
}
$LlamaRoot = Join-Path $PayloadRoot 'llama-cpp'
New-Item -ItemType Directory -Force -Path $PayloadRoot, $LlamaRoot | Out-Null

# ---------------------------------------------------------------------------
# Pinned non-llama artefacts.
# ---------------------------------------------------------------------------
$BaseArtefacts = @(
    @{
        Name   = 'Python 3.12.10 embeddable amd64'
        Url    = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
        Sha256 = '4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3'
        SaveAs = 'python-3.12.10-embed-amd64.zip'
    },
    @{
        Name   = 'WeasyPrint Windows bundle v68.1'
        Url    = 'https://github.com/Kozea/WeasyPrint/releases/download/v68.1/weasyprint-windows.zip'
        Sha256 = '848E286B59C3FECAC9829803FBBD7FC35D3DDF4DD38CE363BC6C3CEE41C356E4'
        SaveAs = 'weasyprint-windows.zip'
    },
    @{
        Name   = 'poppler-windows Release-24.08.0-0'
        Url    = 'https://github.com/oschwartz10612/poppler-windows/releases/download/v24.08.0-0/Release-24.08.0-0.zip'
        Sha256 = '58A6F9AE269756231D2F9AA6CBA39D75FEC6DEACAF3C4A50683383B5F3D5A527'
        SaveAs = 'Release-24.08.0-0.zip'
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

function Get-PayloadArtefact {
    param(
        [Parameter(Mandatory)] [hashtable]$Artefact,
        [Parameter(Mandatory)] [string]$DestDir
    )
    $dest = Join-Path $DestDir $Artefact.SaveAs
    Write-Host ""
    Write-Host "== $($Artefact.Name) ==" -ForegroundColor Cyan

    if (-not $Force -and (Test-Path -LiteralPath $dest)) {
        if ($SkipHashVerify -or $Artefact.Sha256 -like 'TODO_*') {
            Write-Host "  -> cached $dest (hash verification skipped)" -ForegroundColor DarkGray
            return $dest
        }
        if (Test-Sha256 -Path $dest -ExpectedHash $Artefact.Sha256) {
            Write-Host "  -> cached $dest (SHA256 OK)" -ForegroundColor Green
            return $dest
        }
        Write-Warning "Cached file failed hash check; redownloading."
        Remove-Item -LiteralPath $dest -Force
    }

    Invoke-DownloadWithRetry -Url $Artefact.Url -Destination $dest

    $observed = (Get-FileHash -Algorithm SHA256 -LiteralPath $dest).Hash.ToUpperInvariant()
    if ($SkipHashVerify -or $Artefact.Sha256 -like 'TODO_*') {
        Write-Warning "SHA256 verification skipped for '$($Artefact.Name)'. Observed: $observed"
    } elseif ($observed -ne $Artefact.Sha256.ToUpperInvariant()) {
        throw "SHA256 mismatch for $($Artefact.Name)`n  expected: $($Artefact.Sha256)`n  observed: $observed"
    } else {
        Write-Host "  -> SHA256 OK ($observed)" -ForegroundColor Green
    }
    return $dest
}

function Resolve-LlamaTag {
    param([string]$Override, [string]$CachePath)

    if ($Override) {
        Write-Host "[llama.cpp] using override tag: $Override" -ForegroundColor DarkGray
        Set-Content -LiteralPath $CachePath -Value $Override -Encoding ASCII
        return $Override
    }

    # Default behaviour now PINS a known-good llama.cpp release tag
    # whose binary SHA256 hashes match the ``$LlamaVariants`` table
    # below. Dynamically resolving the GitHub ``releases/latest``
    # endpoint was a build-breaker: upstream cuts a fresh release
    # every few days (b9109 -> b9116 in a week), the script picks up
    # the new tag, and the pinned SHAs no longer match the binaries
    # at the new URL -> ``SHA256 mismatch for llama.cpp cpu (b9116)``.
    #
    # Bumping policy: edit ``$DefaultLlamaTag`` AND the three
    # ``Sha256`` fields in ``$LlamaVariants`` together; don't bump one
    # without the other.
    #
    # To opt back into latest-tracking, pass ``-LlamaTag latest`` (or
    # set ``$env:LLAMA_TAG=latest``). The dynamic resolution path is
    # preserved for that case.
    $DefaultLlamaTag = 'b9109'
    $resolveLatest = $false
    $envOverride = $env:LLAMA_TAG
    if ($envOverride -and $envOverride -ne 'latest') {
        Write-Host "[llama.cpp] using env-override tag: $envOverride" -ForegroundColor DarkGray
        Set-Content -LiteralPath $CachePath -Value $envOverride -Encoding ASCII
        return $envOverride
    }
    if ($envOverride -eq 'latest') {
        $resolveLatest = $true
    }

    if (-not $resolveLatest) {
        Write-Host "[llama.cpp] using pinned tag: $DefaultLlamaTag" -ForegroundColor DarkGray
        Set-Content -LiteralPath $CachePath -Value $DefaultLlamaTag -Encoding ASCII
        return $DefaultLlamaTag
    }

    if (Test-Path -LiteralPath $CachePath) {
        $cached = (Get-Content -LiteralPath $CachePath -Raw).Trim()
        if ($cached) {
            Write-Host "[llama.cpp] reusing cached release tag: $cached" -ForegroundColor DarkGray
            return $cached
        }
    }

    Write-Host "[llama.cpp] resolving latest release tag..." -ForegroundColor DarkGray

    $tag = $null
    $ghCmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($ghCmd) {
        try {
            $json = & gh api repos/ggml-org/llama.cpp/releases/latest 2>$null
            if ($LASTEXITCODE -eq 0 -and $json) {
                $obj = $json | ConvertFrom-Json
                $tag = $obj.tag_name
            }
        } catch {
            Write-Warning "gh api call failed: $($_.Exception.Message)"
        }
    }

    if (-not $tag) {
        # Fallback to direct REST call. Anonymous requests share the
        # runner public IP's 60 req/h pool and get rate-limited on
        # busy days (the build dies with ``403 rate limit exceeded``);
        # if a ``GITHUB_TOKEN`` / ``GH_TOKEN`` is set in the env (CI),
        # add it as a Bearer token so we land in the 1000 req/h
        # authenticated bucket.
        $headers = @{ 'User-Agent' = 'report-anonymizer-build' }
        $token = $env:GITHUB_TOKEN
        if (-not $token) { $token = $env:GH_TOKEN }
        if ($token) {
            $headers['Authorization'] = "Bearer $token"
        }
        try {
            $obj = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest' `
                                     -Headers $headers `
                                     -ErrorAction Stop
            $tag = $obj.tag_name
        } catch {
            throw "Unable to resolve latest llama.cpp release tag. Pass -LlamaTag <b####> explicitly. Reason: $($_.Exception.Message)"
        }
    }

    if (-not $tag) {
        throw "Empty tag returned from GitHub for llama.cpp."
    }

    Write-Host "[llama.cpp] resolved tag = $tag" -ForegroundColor Green
    Set-Content -LiteralPath $CachePath -Value $tag -Encoding ASCII
    return $tag
}

# ---------------------------------------------------------------------------
# 1. Base artefacts (always downloaded).
# ---------------------------------------------------------------------------
Write-Host "[prepare-payload] PayloadRoot = $PayloadRoot"
foreach ($a in $BaseArtefacts) {
    [void](Get-PayloadArtefact -Artefact $a -DestDir $PayloadRoot)
}

# ---------------------------------------------------------------------------
# 2. llama.cpp variants.
# ---------------------------------------------------------------------------
$tagCache = Join-Path $LlamaRoot '.tag'
$tag      = Resolve-LlamaTag -Override $LlamaTag -CachePath $tagCache

# llama.cpp releases name files like
#   llama-b9109-bin-win-cpu-x64.zip
#   llama-b9109-bin-win-cuda-12.4-x64.zip
#   llama-b9109-bin-win-vulkan-x64.zip
# Some recent releases ship cudart-* DLLs as a separate zip; we ignore those
# in this stage (extract-runtime.ps1 deals with cherry-picking).
$LlamaVariants = @(
    @{
        Variant = 'cpu'
        File    = "llama-$tag-bin-win-cpu-x64.zip"
        Sha256  = '94E4B6230055566D4FDAD02304AF6CB79B19529491076A432337C1ED939540F1'
        Always  = $true
    },
    @{
        Variant = 'cuda'
        File    = "llama-$tag-bin-win-cuda-12.4-x64.zip"
        Sha256  = '896B92DBB7B9C6A1A2AF085EEB5771EC66F4F6235D95FCBBF5FFE355C3318B03'
        Always  = $false
    },
    @{
        Variant = 'vulkan'
        File    = "llama-$tag-bin-win-vulkan-x64.zip"
        Sha256  = 'E9EFE0A3E025EED2E052AE24942A7BC4355E63355F9B1D2888B86A7C01086665'
        Always  = $false
    }
)

foreach ($lv in $LlamaVariants) {
    if (-not $lv.Always -and $Lean) {
        Write-Host ""
        Write-Host "== llama.cpp $($lv.Variant) [SKIPPED via -Lean] ==" -ForegroundColor DarkYellow
        continue
    }
    $variantDir = Join-Path $LlamaRoot $lv.Variant
    New-Item -ItemType Directory -Force -Path $variantDir | Out-Null
    $art = @{
        Name   = "llama.cpp $($lv.Variant) ($tag)"
        Url    = "https://github.com/ggml-org/llama.cpp/releases/download/$tag/$($lv.File)"
        Sha256 = $lv.Sha256
        SaveAs = $lv.File
    }
    [void](Get-PayloadArtefact -Artefact $art -DestDir $variantDir)
}

Write-Host ""
Write-Host "[prepare-payload] OK -- artefacts cached under $PayloadRoot" -ForegroundColor Green
if ($Lean) {
    Write-Host "[prepare-payload] Lean build (CPU-only): CUDA + Vulkan variants were skipped." -ForegroundColor DarkYellow
}
