# Report Anonymizer · Windows installer build

This directory is the **build-time** home for the Windows installer.
It is not part of the runtime payload. Everything under `build-cache/`,
`staging/` and `dist/` is regenerated from upstream sources by the scripts
documented below — nothing in this tree needs to be checked into git after
a build.

The end-product is

```
packaging\windows\dist\Report-Anonymizer-Setup-x64-<version>.exe
```

(or `…-lean.exe` when built with `-Lean`).

The high-level design lives in
[`.cursor/plans/windows_installer_build_01fea968.plan.md`](../../.cursor/plans/windows_installer_build_01fea968.plan.md);
this README is the operational "how do I build / debug / iterate" guide.

---

## Prerequisites

The build runs entirely in-place: it never modifies the host system. You only
need a stock Windows 10 1809+ box with:

| Requirement                | Why                                                              |
|----------------------------|------------------------------------------------------------------|
| PowerShell 5.1 or later    | The orchestration scripts (`build.ps1`, `scripts/*.ps1`).        |
| ~3 GB free on the build drive | build-cache + staging tree fluctuate around 2 GB during a run.|
| Network access             | First-run downloads ~250 MB of tooling and ~250 MB of payload.   |
| GitHub CLI (`gh`) [optional] | Used to resolve the latest llama.cpp release tag automatically. Falls back to anonymous REST. |

You do **not** need: Visual Studio, Python on PATH, Chocolatey, Inno Setup
installed system-wide, MinGW system-wide, an admin shell, or a code-signing
certificate. Every tool is downloaded to `build-cache/tools/` as a portable
artefact and verified by SHA256.

## One-shot build

From the repository root:

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0
```

If `-Version` is omitted, the value is parsed from `pyproject.toml:7`.

To start from a completely clean slate (force re-download of everything):

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0 -Clean
```

To build the slim variant (CPU AVX2 only, ~225 MB instead of ~405 MB):

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0 -Lean
```

Expected end-to-end timing on a typical developer machine:

| Configuration | First run | Cached run |
|---------------|----------:|-----------:|
| Default       | ~18 min   | ~6–7 min   |
| `-Lean`       | ~12 min   | ~5 min     |

At the end you'll get the `.exe`, a `.sha256.txt` companion file, and a
summary block printed to the console.

## What the pipeline does

`build.ps1` is a thin orchestrator that runs the following scripts in
order. Each is **idempotent** — re-running the master script after a
partial failure picks up exactly where you left off:

| #  | Script                                | Purpose                                                                 |
|----|----------------------------------------|-------------------------------------------------------------------------|
| 1  | `scripts/bootstrap-tools.ps1`          | Downloads 7zr.exe, Inno Setup 6.4.0 portable, MinGW-w64 (WinLibs UCRT). |
| 2  | `scripts/prepare-payload.ps1`          | Downloads Python embed, WeasyPrint windows zip, poppler, 3x llama.cpp. |
| 3  | `scripts/extract-runtime.ps1`          | Cherry-picks DLLs from the WeasyPrint zip; copies pdftotext + llama-server variants. |
| 4  | `scripts/convert-assets.ps1`           | Converts `assets/app_icon.svg` to `launcher/app_icon.ico` (multi-res). |
| 5  | `scripts/build-python-tree.ps1`        | Extracts Python embeddable, bootstraps pip, installs requirements.txt, strips dead weight, validates Qt plugins. |
| 6  | `scripts/freeze-lockfile.ps1`          | `pip freeze` -> `requirements-lock-windows.txt` (committed to repo).   |
| 7  | `scripts/compile-launchers.ps1`        | windres + gcc -> `ReportAnonymizer.exe` and `report-anonymizer-cli.exe`. |
| 8  | `scripts/stage-app.ps1`                | Copies repo source trees + launchers into `staging/app/`.              |
| 9  | `scripts/smoke-test.ps1`               | Reinforced battery (imports, WeasyPrint, libmagic, pandoc, PySide6 widgets, pdftotext, hardware JSON, llama-server --version, CLI selftest). |
| 10 | ISCC compile (Inno Setup)              | Produces `dist/Report-Anonymizer-Setup-x64-<ver>.exe`.                 |

Each script can be invoked directly while iterating (e.g. to debug the
launcher compilation step in isolation):

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\scripts\compile-launchers.ps1 -Version 1.0.0
```

## Directory layout

```text
packaging/windows/
├── README.md                     <-- this file
├── .gitignore                    <-- excludes build-cache/, staging/, dist/, .exe/.ico/.res
├── build.ps1                     <-- master orchestrator
│
├── inno/
│   └── ReportAnonymizer.iss      <-- Inno Setup 6 script (Pascal wizard for llama.cpp variant)
│
├── launcher/                     <-- C launchers (compiled output gitignored)
│   ├── launcher_common.h
│   ├── launcher_gui.c            <-- subsystem WINDOWS (Qt GUI)
│   ├── launcher_cli.c            <-- subsystem CONSOLE (forwards argv to bin/anonymize-dossier)
│   ├── launcher.manifest         <-- UAC asInvoker + DPI awareness + UTF-8 + longPathAware
│   ├── app_icon.rc               <-- icon + manifest + VS_VERSION_INFO
│   └── app_icon.ico              <-- generated by convert-assets.ps1 (gitignored)
│
├── scripts/                      <-- the 9 PS1 scripts listed in the table above
│
├── build-cache/                  <-- gitignored, ~700 MB after a default first run
│   ├── tools/                    <-- 7zr.exe, innosetup/, mingw64/
│   ├── downloads/                <-- raw .exe/.zip artefacts
│   ├── payload/                  <-- python-embed.zip, weasyprint-windows.zip, poppler zip,
│   │   └── llama-cpp/{cpu,cuda,vulkan}/<zip>
│   └── assetvenv/                <-- temporary Python venv (Pillow + cairosvg)
│
├── staging/                      <-- gitignored, ~1.5 GB while building
│   └── app/
│       ├── python/               <-- embeddable + site-packages
│       ├── runtime/              <-- Pango/Cairo/HarfBuzz DLLs + fonts/
│       ├── tools/                <-- pdftotext.exe + llama-server.exe (chosen variant)
│       ├── repo/                 <-- anonymize/, gui/, bin/, config/, prompts/, templates/, assets/
│       ├── launcher/             <-- ReportAnonymizer.exe + report-anonymizer-cli.exe + app_icon.ico
│       └── llama-variants/{cpu,cuda,vulkan}/  <-- selected by the wizard at install time
│
└── dist/                         <-- gitignored, the .exe artefact
```

## Tuning knobs

### `-SkipBootstrap`

Once `build-cache/tools/` is populated, you can shave ~5–15 seconds by
asking the orchestrator to skip step 1 entirely:

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0 -SkipBootstrap
```

### Pinning the llama.cpp release tag

`prepare-payload.ps1` resolves the *latest* llama.cpp release tag at first
run and caches it under `build-cache/payload/llama-cpp/.tag`. To force a
specific tag (useful when reproducing an old build, or when upstream
publishes a broken release):

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\scripts\prepare-payload.ps1 -LlamaTag b6789
```

Delete `build-cache/payload/llama-cpp/.tag` to clear the pin.

### Silent install parameters (downstream)

The Inno Setup script honours `/SILENT` and `/VERYSILENT` plus one of
`/CPU`, `/CUDA`, `/VULKAN`, `/SKIP` to choose the llama-server variant
non-interactively. Example for an unattended CI deployment:

```powershell
Report-Anonymizer-Setup-x64-1.0.0.exe /VERYSILENT /CUDA /DIR="C:\ReportAnonymizer"
```

## Troubleshooting

### `Set-ExecutionPolicy` blocks the scripts

PowerShell 5.1 defaults to `Restricted`. Bypass per-invocation:

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0
```

No permanent policy change is needed.

### Windows Defender slows the build × 3 (or quarantines a launcher .exe)

Defender's real-time scanning is the single largest contributor to a slow
first build. The compiled launcher .exe files are also unsigned (v1 does
not include code-signing), so SmartScreen / Defender may flag them
defensively. Exclude the build tree to bring timings back to baseline:

```powershell
Add-MpPreference -ExclusionPath (Resolve-Path .\packaging\windows).Path
```

Remove the exclusion after the build finishes if you prefer.

### Behind a corporate proxy

`Invoke-WebRequest` honours `HTTP_PROXY` / `HTTPS_PROXY`. Set them before
running the build:

```powershell
$env:HTTPS_PROXY = 'http://user:pass@proxy.example.com:8080'
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0
```

For air-gapped builds, manually populate `build-cache/downloads/` and
`build-cache/payload/` with the artefacts named in `bootstrap-tools.ps1`
and `prepare-payload.ps1`; the scripts skip downloads when the cached
files match the expected SHA256.

### SHA256 mismatch on a freshly-downloaded artefact

The pinned hashes live in the `$Artefacts` / `$BaseArtefacts` /
`$LlamaVariants` tables inside the scripts. If the hash check fails:

1. Make sure no proxy or AV is rewriting the downloaded file.
2. If upstream genuinely published a new build, run the script once with
   `-SkipHashVerify` to see the **observed** hash and paste it back into
   the pinned table (then commit the bump).

Never ship a Setup.exe built with `-SkipHashVerify` to end users.

### `-Lean` skips CUDA and Vulkan

Use the lean build when:

- You're producing a CI smoke build (faster, smaller).
- You're shipping to an enterprise audience that does not have GPUs.
- You're iterating on the wizard / launcher and don't need to validate the
  GPU variants on every rebuild.

The result is `Report-Anonymizer-Setup-x64-<ver>-lean.exe` (~225 MB) with
only the CPU AVX2 variant pre-bundled; the wizard hides the disabled radio
buttons.

### Smoke test fails because the build host has no GPU

The smoke test fails CPU runtime hard but treats CUDA and Vulkan as
warnings when their `--version` invocation exits non-zero. This is
intentional: GPU drivers may legitimately be absent on a CI runner. The
.exe is still built; end users with a matching GPU/driver pick the
variant at install time.

### Re-running after a crash

`build.ps1` writes a PID lock at `build-cache/.build.lock`. If a previous
run died without releasing it, just run again — the script detects the
stale PID and reclaims the lock automatically. To start over from
scratch:

```powershell
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0 -Clean
```

## Where do user files live after install?

This is **runtime** information, but it is useful to keep close to the
build docs:

| Use case                  | Path                                                                  |
|---------------------------|-----------------------------------------------------------------------|
| Per-user configuration    | `%APPDATA%\report-anonymizer\` (server.yml, app_settings.yml, presets) |
| Per-user data + cache     | `%LOCALAPPDATA%\report-anonymizer\` (downloaded GGUF models, cache)    |
| Application install root  | `%LOCALAPPDATA%\Programs\report-anonymizer\app\`                      |
| Installer choice sentinel | `%APPDATA%\report-anonymizer\.installer_choice.json` (variant chosen) |

The uninstaller prompts before deleting `%APPDATA%\report-anonymizer\` and
`%LOCALAPPDATA%\report-anonymizer\`; say "No" if you plan to reinstall.

## Code-signing (out of scope for v1)

The current `.exe` is *not* signed. SmartScreen will display "Windows
protected your PC" on the first run — users click *More info* → *Run
anyway* to proceed. Code-signing is tracked for v1.1 (requires an OV/EV
certificate, ~$100/year).

## Reproducing a release deterministically

CI (Fase 5, separate workstream) consumes `requirements-lock-windows.txt`
in the repo root via

```yaml
pip install -r requirements-lock-windows.txt --require-hashes
```

To reproduce a tagged release locally:

```powershell
git checkout v1.0.0
pwsh -ExecutionPolicy Bypass -File .\packaging\windows\build.ps1 -Version 1.0.0 -Clean
```

The resulting `.exe` should hash-match the GitHub Release artefact
modulo the unsigned bits (signature timestamp, ISCC randomness in the
compressed stream). The SHA256 of the **payload** is identical.
