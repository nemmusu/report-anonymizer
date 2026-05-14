<#
.SYNOPSIS
    Reinforced post-stage smoke test (plan §2.5 / §2.7).

.DESCRIPTION
    Validates the staging tree end-to-end before ISCC is allowed to build a
    Setup.exe. The full battery covers:

      (a) Python imports of the heavy modules.
      (b) WeasyPrint HTML -> PDF full pipeline (validates Pango/Cairo DLLs).
      (c) libmagic.from_buffer (validates python-magic-bin's libmagic.dll).
      (d) Pandoc compatibility: convert a 5 KB chunk of our own sample
          report to HTML with --mathml --standalone (validates the pinned
          pandoc 3.5 from pypandoc-binary).
      (e) PySide6 widget instantiation: QApplication + QLabel + QIcon(.ico)
          + QPixmap(.svg) - asserts the Qt plugins for platforms,
          imageformats and iconengines all load.
      (f) poppler: pdftotext.exe -v exits 0.
      (g) hardware report JSON via `python.exe -m anonymize.hardware` -
          parsed via ConvertFrom-Json, asserts presence of `gpus` key.
      (h) llama-server --version for cpu / cuda / vulkan variants
          (CPU MUST succeed; CUDA/Vulkan warn-only when no driver).
      (i) report-anonymizer-cli.exe selftest.

    A single non-warning failure causes the build to abort.

.PARAMETER StagingRoot
.PARAMETER Lean
    Skip CUDA + Vulkan variant checks.
#>
[CmdletBinding()]
param(
    [string]$StagingRoot,
    [switch]$Lean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Native-command runner that captures stdout + stderr fully, without letting
# PowerShell 5.1 turn the first stderr line into a terminating ErrorRecord
# (which $ErrorActionPreference='Stop' would do for `& cmd 2>&1`).
# ---------------------------------------------------------------------------
function Invoke-Native {
    param(
        [Parameter(Mandatory)] [string]   $FilePath,
        [string[]]                        $ArgList = @(),
        [string]                          $WorkingDir = $PWD.Path
    )
    function Format-NativeArg([string]$a) {
        if ($null -eq $a) { return '""' }
        if ($a -eq '')    { return '""' }
        if ($a -notmatch '[\s"]') { return $a }
        # Escape backslashes only when followed by a quote, then wrap in quotes.
        $escaped = $a -replace '(\\+)("|$)', '$1$1$2' -replace '"', '\"'
        return '"' + $escaped + '"'
    }
    $argString = ($ArgList | ForEach-Object { Format-NativeArg $_ }) -join ' '

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $FilePath
    $psi.Arguments              = $argString
    $psi.WorkingDirectory       = $WorkingDir
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    # Read both streams to EOF before WaitForExit to avoid the classic
    # "child fills the OS pipe buffer and deadlocks" bug.
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $proc.WaitForExit()
    [pscustomobject]@{
        ExitCode = $proc.ExitCode
        StdOut   = $stdoutTask.Result
        StdErr   = $stderrTask.Result
    }
}

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$WindowsRoot = Split-Path -Parent $ScriptDir
if (-not $StagingRoot) { $StagingRoot = Join-Path $WindowsRoot 'staging' }

$AppRoot   = Join-Path $StagingRoot 'app'
$PyExe     = Join-Path $AppRoot 'python\python.exe'
$RepoDir   = Join-Path $AppRoot 'repo'
$RuntimeDir= Join-Path $AppRoot 'runtime'
$ToolsDir  = Join-Path $AppRoot 'tools'
$PluginsDir= Join-Path $AppRoot 'python\Lib\site-packages\PySide6\plugins'

foreach ($p in @($PyExe, $RepoDir, $RuntimeDir, $ToolsDir)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw "smoke-test: required path missing - $p. Run earlier build steps first."
    }
}

# ---------------------------------------------------------------------------
# Environment block - identical to the launcher's (plan §6 F-series).
# Capture the original values so we can restore them on exit.
# ---------------------------------------------------------------------------
$envBackup = @{}
function Set-EnvBackup([string]$Name, [string]$Value) {
    if (-not $envBackup.ContainsKey($Name)) {
        $envBackup[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

Set-EnvBackup 'QT_QPA_PLATFORM'              'offscreen'
Set-EnvBackup 'ANONYMIZE_SKIP_WIZARD'        '1'
Set-EnvBackup 'PYTHONDONTWRITEBYTECODE'      '1'
Set-EnvBackup 'PYTHONHOME'                   (Join-Path $AppRoot 'python')
Set-EnvBackup 'QT_QPA_PLATFORM_PLUGIN_PATH'  (Join-Path $PluginsDir 'platforms')
Set-EnvBackup 'QT_PLUGIN_PATH'               $PluginsDir
Set-EnvBackup 'PYTHONPATH'                   $RepoDir

$pypandocFiles = Join-Path $AppRoot 'python\Lib\site-packages\pypandoc\files'
$pandocExe     = Join-Path $pypandocFiles 'pandoc.exe'
if (Test-Path -LiteralPath $pandocExe) {
    Set-EnvBackup 'PYPANDOC_PANDOC' $pandocExe
}

$origPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$prefix = @($RuntimeDir, $ToolsDir, (Join-Path $AppRoot 'python'), $pypandocFiles) -join ';'
Set-EnvBackup 'PATH' ($prefix + ';' + $origPath)

try {
    # -----------------------------------------------------------------------
    # (a-e) Python-side checks (one process, fail-fast).
    # -----------------------------------------------------------------------
    Write-Host "== Python imports + WeasyPrint + libmagic + pandoc + Qt ==" -ForegroundColor Cyan

    $appIco  = Join-Path $AppRoot 'launcher\app_icon.ico'
    $heroSvg = Join-Path $RepoDir 'assets\hero.svg'
    $sampleMd= Join-Path $RepoDir 'docs\sample_report\sample_pentest_report.md'

    $pyScript = @"
import os, sys, pathlib, tempfile, traceback

# (a) imports
import anonymize.pipeline  # noqa: F401
import weasyprint
import fitz                # PyMuPDF
import magic
import pypandoc

# (b) WeasyPrint full pipeline
out_pdf = pathlib.Path(tempfile.gettempdir()) / "rapack-smoke.pdf"
weasyprint.HTML(string="<p style='font-family:serif'>x</p>").write_pdf(str(out_pdf))
assert out_pdf.exists() and out_pdf.stat().st_size > 0, "WeasyPrint produced empty PDF"

# (c) libmagic
mime = magic.from_buffer(b"%PDF-1.4 dummy", mime=True)
assert mime.startswith("application/pdf"), f"libmagic mime unexpected: {mime!r}"

# (d) Pandoc compatibility against real sample (only if file exists).
sample = pathlib.Path(r'$sampleMd')
if sample.exists():
    text = sample.read_text(encoding='utf-8', errors='replace')[:5000]
    html = pypandoc.convert_text(text, 'html', 'md',
                                 extra_args=['--mathml', '--standalone'])
    assert '<p' in html or '<h1' in html, 'pandoc convert_text returned unexpected HTML'
else:
    # Fallback: tiny conversion to at least confirm pandoc is callable.
    out = pypandoc.convert_text('# title', 'plain', 'md')
    assert 'title' in out, 'pandoc fallback conversion failed'

# (e) PySide6 widget instantiation - validates plugin loading.
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QPixmap, QIcon

app = QApplication.instance() or QApplication(sys.argv)
lbl = QLabel("smoke")
ico = QIcon(r'$appIco')
assert not ico.isNull(), 'QIcon failed to load app_icon.ico (qico plugin missing?)'
pix = QPixmap(r'$heroSvg')
assert not pix.isNull(), 'QPixmap failed to load hero.svg (qsvg plugin missing?)'

# Now that GUI init worked, also import gui.main to ensure the Python
# package is wired correctly. Do it AFTER QApplication so any QApplication
# created there does not race with ours.
import gui.main  # noqa: F401

print('PY_SMOKE_OK')
"@

    $scratch = Join-Path $env:TEMP ('rapack-smoke-{0}.py' -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    Set-Content -LiteralPath $scratch -Value $pyScript -Encoding UTF8
    try {
        $pyResult = Invoke-Native -FilePath $PyExe -ArgList @($scratch)
    } finally {
        Remove-Item -LiteralPath $scratch -ErrorAction SilentlyContinue
    }
    if ($pyResult.StdOut) { Write-Host $pyResult.StdOut.TrimEnd() }
    if ($pyResult.StdErr) { Write-Host ("--- python stderr ---`n" + $pyResult.StdErr.TrimEnd()) -ForegroundColor DarkYellow }
    if ($pyResult.ExitCode -ne 0 -or ($pyResult.StdOut -notmatch 'PY_SMOKE_OK')) {
        throw "Python smoke battery failed (exit $($pyResult.ExitCode)). See output above."
    }
    Write-Host "  -> Python battery OK" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # (f) poppler.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "== poppler / pdftotext --version ==" -ForegroundColor Cyan
    $pdftotext = Join-Path $ToolsDir 'pdftotext.exe'
    $pdfRes = Invoke-Native -FilePath $pdftotext -ArgList @('-v')
    foreach ($line in (($pdfRes.StdOut + "`n" + $pdfRes.StdErr) -split "`r?`n")) {
        if ($line) { Write-Host "    $line" }
    }
    if ($pdfRes.ExitCode -ne 0) {
        throw "pdftotext.exe -v exited $($pdfRes.ExitCode)"
    }
    Write-Host "  -> pdftotext OK" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # (g) Hardware report JSON.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "== anonymize.hardware report_dict ==" -ForegroundColor Cyan
    $hwRes = Invoke-Native -FilePath $PyExe -ArgList @('-m', 'anonymize.hardware')
    if ($hwRes.ExitCode -ne 0) {
        throw "python -m anonymize.hardware failed (exit $($hwRes.ExitCode)):`nSTDOUT:`n$($hwRes.StdOut)`nSTDERR:`n$($hwRes.StdErr)"
    }
    try {
        $report = $hwRes.StdOut | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "anonymize.hardware did not emit valid JSON:`n$($hwRes.StdOut)`nSTDERR:`n$($hwRes.StdErr)"
    }
    if ($null -eq $report.PSObject.Properties['gpus']) {
        throw "anonymize.hardware report_dict() is missing the 'gpus' key (wizard contract broken)"
    }
    Write-Host "  -> hardware JSON OK ($(@($report.gpus).Count) GPU(s) reported)" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # (h) llama-server --version for each available variant.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "== llama-server --version (per variant) ==" -ForegroundColor Cyan
    $variantSpec = @(
        @{ Name = 'cpu';    Required = $true  },
        @{ Name = 'cuda';   Required = $false },
        @{ Name = 'vulkan'; Required = $false }
    )
    foreach ($v in $variantSpec) {
        if (-not $v.Required -and $Lean) { continue }
        $exe = Join-Path $AppRoot ("llama-variants\{0}\llama-server.exe" -f $v.Name)
        if (-not (Test-Path -LiteralPath $exe)) {
            if ($v.Required) {
                throw "Required llama-server variant missing: $exe"
            }
            Write-Warning "  -> $($v.Name) variant absent (skip)"
            continue
        }
        $vRes = Invoke-Native -FilePath $exe -ArgList @('--version')
        $vout = ($vRes.StdOut + "`n" + $vRes.StdErr).TrimEnd()
        if ($vRes.ExitCode -eq 0) {
            Write-Host "  -> $($v.Name) OK" -ForegroundColor Green
        } else {
            if ($v.Required) {
                throw "llama-server $($v.Name) --version failed (exit $($vRes.ExitCode)):`n$vout"
            } else {
                Write-Warning "llama-server $($v.Name) --version failed on this build host (exit $($vRes.ExitCode)). This is expected when no GPU/driver is available. Output:`n$vout"
            }
        }
    }

    # -----------------------------------------------------------------------
    # (i) report-anonymizer-cli.exe selftest.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "== report-anonymizer-cli.exe selftest ==" -ForegroundColor Cyan
    $cli = Join-Path $AppRoot 'launcher\report-anonymizer-cli.exe'
    if (-not (Test-Path -LiteralPath $cli)) {
        throw "CLI launcher missing: $cli"
    }
    $cliRes = Invoke-Native -FilePath $cli -ArgList @('selftest')
    if ($cliRes.StdOut) { Write-Host $cliRes.StdOut.TrimEnd() }
    if ($cliRes.StdErr) { Write-Host ("--- cli stderr ---`n" + $cliRes.StdErr.TrimEnd()) -ForegroundColor DarkYellow }
    if ($cliRes.ExitCode -ne 0) {
        throw "report-anonymizer-cli.exe selftest exited $($cliRes.ExitCode)"
    }
    Write-Host "  -> CLI selftest OK" -ForegroundColor Green
}
finally {
    foreach ($k in $envBackup.Keys) {
        [Environment]::SetEnvironmentVariable($k, $envBackup[$k], 'Process')
    }
}

Write-Host ""
Write-Host "[smoke-test] ALL CHECKS GREEN" -ForegroundColor Green
