# Screenshots

This folder holds the PNGs referenced from the project README and
the MkDocs site. Below is the current set, what view it captures
and where it shows up.

## Current set

| File | View | Used in |
|---|---|---|
| `diff.png` | Diff view (full window, two-pane PDF render with synced scroll) | README hero, docs landing page |
| `pipeline-wait-approve.png` | Pipeline paused at *Approve & promote* with stepper + log | README demo grid, docs Pipeline tab |
| `review-preview.png` | Review tree + live anonymized-output preview (Text candidates tab) | README demo grid, docs Review tab |
| `review-images.png` | Review &raquo; Images tab: thumbnail strip + per-image editor with baked rect | README demo grid, docs Images tab, FAQ image redaction Q&A, anonymization-scope Embedded images section |
| `build-preview.png` | Review &raquo; Preview of build tab: PDF.js viewer with selectable text + Build button | README demo grid, docs Build preview tab |
| `server-tab.png` | Server panel with preset gallery + Auto-start toggle + sidebar dot | README demo grid, docs Server tab |
| `curated-downloads.png` | Model Manager → Curated downloads tab | README demo grid, docs Models tab |
| `pipeline-run.png` | Pipeline mid-scan with progress bar | reserve / fallback for pipeline section |
| `pipeline-finish.png` | Pipeline at 100 %, all stages green | reserve / fallback for pipeline section |
| `main-window.png` | Empty MainWindow with the drop zone | reserve, "first-launch" copy |
| `import.png` | Import-project dialog with file list and PDF strategy | README "More screenshots" gallery |
| `wizard.png` | First-run wizard hardware-detection step | README "More screenshots" gallery |
| `wizard-download-model.png` | Wizard model-download step (live MB/s + ETA) | README "More screenshots" gallery |
| `edit-preset.png` | Preset editor with runtime / model / performance fields | README "More screenshots" gallery |
| `deployment.png` | "Choose how to run llama-server" dialog | README "More screenshots" gallery |
| `settings.png` | Settings dialog (Pipeline tab) | README "More screenshots" gallery |
| `queue.png` | Model Manager → Queue tab during a download | reserve / FAQ illustration |
| `search-hf.png` | Model Manager → Search Hugging Face tab | reserve / FAQ illustration |

## How to capture

The preferred tool is whatever produces the cleanest 1× screenshot
on your machine. Suggestions per platform:

- **Linux**: `gnome-screenshot --area --interactive` or
  `flameshot gui`.
- **macOS**: `Cmd+Shift+4`, then drag.
- **Windows**: Snipping Tool / Snip & Sketch.

Save as **PNG** (no JPEG, the highlights become muddy). Aim for
~1400 px on the long edge for full-window shots and ~2500 px for
two-pane diff captures so the README renders crisply on retina
displays without bloating the repo.

## Style guidelines

- **No real customer data.** Use the synthetic corpus under
  [`docs/sample_report/`](../sample_report/) or anything you have
  rights to publish.
- **Dark theme.** The default GUI palette is a dark accent; capture
  with that for visual consistency.
- **No personal taskbar / desktop in the frame.** Crop tightly to
  the app window (the in-app screenshot should fill the frame).

## Adding more screenshots

1. Drop the PNG in this folder.
2. Reference it from the README or a docs page with relative path
   `docs/screenshots/<file>.png`.
3. Update the table at the top of this file.
4. Open a PR, see [contributing.md](../contributing.md).
