# Changelog

All notable changes to Report Anonymizer are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-05-12

First public release with a native Windows installer + the existing
Linux artefacts (AppImage, `.deb`, one-line installer). The Python
pipeline is unchanged in shape; this release focuses on the GUI /
packaging / UX layer.

### Added

- **Native Windows installer**
  - `packaging/windows/build.ps1` orchestrator + Inno Setup 6 script
    that bundles the embedded Python 3.12 runtime, `pandoc`, `pdftotext`
    and three `llama-server.exe` variants (CPU / CUDA / Vulkan).
  - Hardware-aware wizard: detects the primary GPU via `nvidia-smi`
    → PowerShell CIM → wmic, then pre-selects the matching variant
    (CUDA on NVIDIA, Vulkan on AMD / Intel, CPU fallback otherwise).
  - "Keep existing" radio only pre-selected when the on-disk variant
    matches the recommendation; otherwise the recommended row wins.
  - Per-user install (no admin), Start-menu + desktop shortcuts,
    proper uninstaller with **keep-user-data** prompt (default: keep).
  - Sentinel file (`.installer_choice.json`) so the first-run wizard
    skips the "install llama-server" step on installer-aware setups.
- **Pipeline UX overhaul**
  - In-layout **Pipeline activity feed**: per-stage events land in a
    compact in-card list instead of as 8-10 floating toasts.
  - Top-right toast cap raised to a hard max of 3 simultaneous to
    avoid covering the residuals banner / build banner.
  - Green **Build complete** banner with **Open output folder** +
    **View report** CTAs, persists through Verify / Auto-resolve.
  - **Pre-promote leak gate** ("Un-reviewed candidates detected")
    triggered when the Review queue still has rows with
    `decision in {None, "pending"}` at "Approve & continue" time.
  - Per-file progress emission in the detector (`extracting <file>`)
    so the bar label updates while heavy PDF / DOCX extraction runs.
- **Server panel + deployment**
  - **CPU / CUDA / Vulkan backend picker** moved into "Configure
    deployment" on Windows installs; replaces the inline "Llama-server
    binary" combo that lived in the Server panel.
  - Async health probe (`health_nowait`) so the UI no longer pegs
    for 1 s on every poll tick when the server is offline.
  - Auto-correction: profile saved with `deployment_mode=docker` is
    flipped to `local_binary` at boot when Docker is missing AND the
    installer sentinel is present.
  - Spam-quenched `[docker] stop failed` log when Docker is not in
    PATH (the manager early-returns instead of calling `subprocess`).
- **Model Manager**
  - "Open" on the "Model not on disk" dialog now lands the dialog on
    the **Curated downloads** tab with the missing repo pre-selected
    + scrolled into view.
  - Accurate disk-size column on the curated tree: shared
    `params × quantisation` estimator replaces the old
    `vram / 1.2` heuristic that inflated Q4_K_M sizes by ~50%.
- **Review tab**
  - Right-click menu gains **Unapprove all in this category** and
    **Unapprove all approved** as a symmetric counterpart to the
    existing Approve bulk actions.
  - "Send all to Review" from the residuals banner now lands on the
    **Text candidates** sub-tab instead of staying on the previously-
    active sub-tab (usually "Preview of build").
- **First-run wizard**
  - **Estimated disk size** ("`~X GB`") shown next to every preset
    so the operator knows how much they're about to download before
    clicking Next.
  - Skip-download button hidden / disabled when the installer
    sentinel is present (the operator has the binary, only the
    GGUF is missing — skipping leaves a non-functional state).
- **Cross-platform paths**
  - All user-scope helpers (`anonymize/_paths.py`) now resolve the
    correct OS-specific dir: Linux `~/.config/document-anonymizer/`,
    Windows `%APPDATA%\report-anonymizer\`, macOS
    `~/Library/Application Support/report-anonymizer/`.
  - Docs cross-reference the per-OS layout (`docs/architecture.md`,
    `docs/presets.md`, `docs/faq.md`, `docs/contributing.md`).
- **CI / release pipeline**
  - `ci.yml` matrix extended to `ubuntu-latest` + `windows-latest`,
    Python 3.10 / 3.11 / 3.12.
  - `build-windows.yml` runs `packaging/windows/build.ps1`
    and uploads `Report-Anonymizer-Setup-x64-*.exe` as an artefact.
  - `build-linux.yml` runs `packaging/build-all.sh` (.deb + AppImage).
  - `release.yml` is tag-driven: fans out to both builders, uploads
    every artefact to the GitHub Release.

### Fixed

- **Build-anyway race condition** (regression): clicking *Build
  anyway* on the leak-confirmation dialog occasionally did nothing
  because the QMessageBox was already torn down by
  `WA_DeleteOnClose` before `clickedButton()` could be read. The
  result is now captured via the `buttonClicked` signal before
  destruction.
- **`&mdash;`** literal in the Build-complete banner replaced with
  the actual em-dash glyph; `setTextFormat(RichText)` set on the
  label so future `<b>` / `<code>` fragments render correctly.
- **Build card** stuck on the grey "skipped" state in single-mode
  with no extra format requested — message reworded to avoid the
  `_looks_skipped` heuristic, card stays green.
- **Diff view** empty when un-approved pending candidates carried an
  identity placeholder (e.g. LLM proposed `ids` → `ids`):
  `_fill_missing_placeholders` now mints a deterministic
  `[REDACTED-<CATEGORY>-<hash>]` fallback so every non-skip
  candidate lands in the substitution map.
- **Stop button latency**: `chat_many` polls futures with a 0.5 s
  timeout + `FIRST_COMPLETED` instead of blocking on `f.result()`;
  worker `chat` loop exits immediately on `stop_event` instead of
  going through a 1 s `time.sleep`.
- **Closing the window**: synchronous wait on worker join + server
  stop reduced from up to 17 s to ~2 s; the window hides
  immediately so the X click feels instant.
- **Toast popups** could spill outside the window's right edge on
  Windows with non-standard DPI; toasts are now top-level `Qt.Tool`
  windows anchored via `mapToGlobal` with width clamped to the
  host window's content area.
- **Console window flash** on Windows during PDF extraction:
  `subprocess.Popen` patch now also forces `STARTUPINFO` with
  `SW_HIDE` and reroutes `os.system` / `os.popen` through the
  patched `Popen`.
- **Uninstaller** previously removed everything regardless of the
  operator's choice (the `[UninstallDelete]` section ran before the
  prompt). The install tree is now gated by a single explicit
  prompt with default "keep user data".
- **Installer double `default` preset** (`installer-default` legacy
  alias) is renamed back to `default` at load time so the wizard's
  preset list shows a single canonical row.

### Changed

- Documentation overhauled: dedicated [Windows install page](docs/install-windows.md),
  refreshed hero CTA buttons, cross-platform path tables across the
  architecture / presets / FAQ / contributing pages.
- Test suite count: **398 passed, 1 skipped** (was 295 before the
  Windows installer + multi-pass detector work landed).
- `pyproject.toml` now ships OS / Python classifiers + project_urls.

[Unreleased]: https://github.com/nemmusu/report-anonymizer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/nemmusu/report-anonymizer/releases/tag/v1.0.0
