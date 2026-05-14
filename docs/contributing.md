# Contributing

Thanks for your interest! This is the short guide for getting a
PR through the door.

## Setup

```bash
git clone https://github.com/nemmusu/report-anonymizer
cd report-anonymizer
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## Run the test suite

```bash
make test                                    # full suite (~10 s)
QT_QPA_PLATFORM=offscreen pytest -k chunker  # one slice
```

The CI workflow runs the same suite on Python 3.10, 3.11, 3.12.
A PR is only mergeable if **all 295 tests pass**.

## Code style

- **Type hints** on every public function. Internal helpers can skip.
- **Docstrings** on classes and any function whose behaviour isn't
  obvious from the name (one paragraph max).
- **Comments only when the WHY is non-obvious.** No "this calls X"
  comments, readers can see that.
- **Don't add abstractions for a hypothetical future requirement.**
  Three similar lines are better than a premature abstraction.

`ruff` and `black` are wired up via `pyproject.toml`; the editor of
your choice should pick them up automatically.

## What we look for in a PR

- **Tests.** Every new behaviour gets a test. The pytest suite has
  GUI tests that work offscreen via `QT_QPA_PLATFORM=offscreen`,
  no display required.
- **No silent regressions.** `make test` must stay green; if you
  break a test, fix the test or revisit the change.
- **Concise description.** Title + one paragraph of *why*. PRs that
  reproduce the README in the body get sent back :-)
- **Screenshots** for UI changes (drop them in `docs/screenshots/`).

## What we don't accept

- Calls to remote LLM APIs as default behaviour. The project is
  local-first by design, cloud is opt-in only, and on the
  [roadmap](https://github.com/nemmusu/report-anonymizer/blob/master/README.md#-roadmap).
- Telemetry / analytics, even anonymous.
- Auto-updates / self-modifying code paths.

## Reporting bugs

Open an issue with:

- Steps to reproduce (smallest possible repro).
- Expected vs actual behaviour.
- `python --version`, `pip list`, plus `uname -a` (Linux/macOS) or
  `systeminfo` / `ver` (Windows).
- The relevant contents of the user-config root (redact any HF
  token!): `~/.config/document-anonymizer/` on Linux,
  `%APPDATA%\report-anonymizer\` on Windows,
  `~/Library/Application Support/report-anonymizer/` on macOS.
- Output of `python bin/anonymize-dossier selftest`.

## Adding a format adapter

1. Subclass `FormatAdapter` from
   [`anonymize/format_adapters/base.py`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/format_adapters/base.py).
2. Implement `extract()` and `write()`.
3. Register in `format_adapters/__init__.py:get_adapter`.
4. Add a round-trip test in `tests/test_<format>_adapter.py`.
5. **Optional but recommended for binary formats**: implement
   `inventory_images()` (return `list[InventoryImageRaw]`) and
   `apply_image_redactions()` so the new format participates in
   the **Review &raquo; Images** flow. The default implementations
   are no-ops, so an adapter that doesn't override them simply
   skips image redaction.

The existing adapters (`docx_adapter.py`, `xlsx_adapter.py`, …) are
the cleanest reference, start from one of those. For image-pass
parity look at `pdf_inplace_adapter.py`, `docx_adapter.py` and
`pptx_adapter.py`.

## Benchmarking your changes

```bash
# Headline benchmark on the full curated corpus.
BENCH_CORPUS_ROOT=/path/to/pdfs make bench-precision

# Spot-check a custom set of GGUFs without touching the autogen manifest.
BENCH_CORPUS_ROOT=/path/to/pdfs python bench/run_extra_models_benchmark.py \
    --manifest "$(python -c 'from anonymize._paths import models_dir; print(models_dir())')/_bench_user_models.json" \
    --out-root /tmp/anonbench/user_models
```

The user-manifest path lets you bench a hand-picked subset of
GGUFs (e.g. when validating a new top-5 candidate) without rerunning
the whole catalog. Each entry is `{repo, file, status, bytes}`; the
runner builds an in-memory `ServerProfile`, starts llama-server,
runs the precision harness against the corpus, scores against the
ground-truth, and emits `report.json` + `report.md`.
