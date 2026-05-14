"""Project configuration: shared metadata for an anonymization run.

A ``Project`` encapsulates the choices an operator makes when starting a job:
which files (single / multi / folder), where the output goes, what PDF
strategy to use, and where the canonical map / patterns / safe terms live.

The CLI builds a ``Project`` from argparse, the GUI builds it from the
``ImportDialog``. Both then hand it to the engine functions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Literal, Optional

InputMode = Literal["single", "multi", "folder"]
DetectorMode = Literal["single", "multipass"]


# Canonical file names of the per-category prompts that drive the
# multipass detector. They live under ``prompts/detector_multipass/``
# and run in this order. Each prompt is focused on one category and
# is ~600-800 tokens, vs ~3500 for the single-pass monolithic prompt.
# Multipass trades roughly 5x detector latency for a measurable
# precision boost on small (4B) local models. Validated on the 5-PDF
# PrivateWave bench: F1 0.836 -> 0.919, FP 9 -> 3.
MULTIPASS_PROMPT_FILES: tuple[str, ...] = (
    "system_detector_brand.txt",
    "system_detector_network.txt",
    "system_detector_phones.txt",
    "system_detector_emails.txt",
    "system_detector_credentials.txt",
    "system_detector_keys.txt",
    "system_detector_headers.txt",
    "system_detector_app_packages.txt",
    "system_detector_user_agents.txt",
    "system_detector_ids.txt",
    "system_detector_infra_ids.txt",
)


def _abs(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


@dataclass
class Project:
    mode: InputMode = "folder"
    input_paths: list[Path] = field(default_factory=list)
    output_dir: Path = Path(".")
    # When ``mode == "single"`` and the user picked a destination that *looks
    # like a file* (e.g. ``foo.anonymized.pdf``), we split the path so
    # ``output_dir`` is ALWAYS a real directory and ``single_output_filename``
    # carries the desired output basename. This guarantees ``apply()`` can
    # safely ``mkdir`` ``output_dir`` without ever clobbering / shadowing the
    # eventual destination file.
    single_output_filename: Optional[str] = None
    pdf_strategy: Literal["inplace", "rederive"] = "inplace"
    map_path: Path = Path("config/substitution_map.yml")
    patterns_path: Path = Path("config/leak_patterns.yml")
    safe_terms_path: Path = Path("config/safe_terms.yml")
    pending_path: Path = Path("needs_review.yml")
    auto_t0_path: Path = Path("auto_promoted_t0.yml")
    auto_t1_path: Path = Path("auto_promoted_t1.yml")
    decisions_path: Path = Path("decisions_history.jsonl")
    applied_path: Path = Path("applied_substitutions.json")
    verifier_report_path: Path = Path("verifier_report.md")
    # Image-redaction state. These paths sit alongside the textual
    # state files above. ``image_inventory_path`` is auto-built at
    # scan time and lists every embedded image found in the input
    # files (image_id, file, position, dimensions). The operator
    # decisions live in ``image_redactions_path``: one entry per
    # image_id with the rect list and the chosen tool. Thumbnails
    # for the GUI gallery are cached under ``image_thumbs_dir`` and
    # are reused across re-scans (the image_id is the sha256 of the
    # raw image bytes, so the same image always hits the same cache
    # key).
    image_inventory_path: Path = Path("image_inventory.yml")
    image_redactions_path: Path = Path("image_redactions.yml")
    image_thumbs_dir: Path = Path(".anon/img_thumbs")
    llm_url: str = "http://localhost:8080/v1"
    llm_model: str = "qwen3.5-9b"
    t_high: float = 0.92
    t_low: float = 0.75
    n_vote: int = 3
    also_build_pdf_for_md: bool = False
    # Single-file mode only: list of *additional* output formats to
    # produce alongside the anonymized file in its original format.
    # Supported values: "pdf", "html", "md". The pipeline runs pandoc
    # to convert the anonymized file to each requested target after
    # apply. Default: empty (only the original format is emitted).
    extra_export_formats: list[str] = field(default_factory=list)

    # Built-in or user template id used when the Build / re-derive
    # paths produce a styled PDF / HTML. ``None`` keeps the legacy
    # plain ``DEFAULT_CSS`` rendering. The Export dialog also reads
    # this so the import-time pick flows into Export by default.
    export_template_id: Optional[str] = None

    # Server profile + parallelism
    server_profile_name: str = "default"
    concurrency: int = 4

    # Detector chunking strategy: ``"structured"`` (default) splits at
    # Markdown structural boundaries (heading / table / code fence /
    # list / paragraph) and never breaks inside one, better recall on
    # long documents. ``"flat"`` is the legacy character-count
    # splitter, kept for repro / regression testing.
    chunk_strategy: str = "structured"

    # Detector pass strategy.
    #
    # ``"single"`` (default, fast): one monolithic prompt covers all 12
    # categories in a single LLM call per chunk. Roughly 30 s / PDF on
    # the shipped 4B preset, but small models can hallucinate FPs when
    # the prompt is dense.
    #
    # ``"multipass"`` (high accuracy, ~5x slower): one tight,
    # category-scoped prompt per pass, with the same chunk sent through
    # 11 passes. Candidate lists are merged before the critic stage.
    # On the 5-PDF PrivateWave bench this lifts F1 from 0.836 to 0.919
    # (precision +0.12, recall +0.05), at the cost of ~5x detector
    # latency. Recommended for noisy / multi-customer reports and for
    # the 4B preset; the larger presets are less prompt-sensitive.
    detector_mode: DetectorMode = "single"

    # Universal folder support
    follow_symlinks: bool = False
    max_depth: Optional[int] = None
    max_file_size_mb: int = 50
    exclude_paths: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    respect_gitignore: bool = True

    # Privacy / Offline
    offline_mode: bool = False

    # When ``True``, the next scan/detect ignores ``substitution_map.yml``
    # entries when filtering already-known values: every leak in the
    # document is re-detected as if it were a brand new run. Defaults to
    # ``True`` so the GUI never silently reuses stale results from a
    # previous run on a different document.
    force_rescan: bool = True

    # When ``True``, after the verify stage the pipeline runs a
    # deterministic verifier-feedback loop: for every residual hit the
    # verifier reports, derive a candidate from the existing
    # substitution_map (case-aware), promote it, re-apply, re-verify.
    # This catches occurrences the LLM detector missed because they
    # were case variants or sub-tokens of a known leak (the canonical
    # map already had the right answer; we just hadn't applied it).
    # Capped at 2 iterations and only fires for hits that have an
    # ancestor in the map, the manual review queue stays as a safety
    # net for everything else.
    auto_resolve_residuals: bool = True

    # When ``True`` (and an LLM is reachable), the auto-resolve loop
    # falls back to an LLM auditor as a third pass. The auditor sees
    # the anonymized output text + the canonical map and reports
    # typos / concatenations / creative variants of values already
    # in the map. Every audit candidate is grounded in an existing
    # map entry, the auditor never invents a brand on its own. This
    # makes the verifier vendor-agnostic: switching customer = new
    # map = new audit ground truth, with no code changes.
    audit_residuals_with_llm: bool = True

    # Suffixes we should NOT interpret as "this is a file path" when the user
    # types something like ``foo.anonymized``: the leading dot just looks like
    # a separator, but it's the *project* directory name, not the basename of
    # an output file. Anything outside this list AND matching the source
    # extension is treated as a file path; otherwise it's a directory.
    _NON_FILE_PSEUDO_SUFFIXES: tuple[str, ...] = (".anonymized", ".anon", ".out")

    @staticmethod
    def _split_single_dst(
        src: Path, dst_dir: str | Path | None
    ) -> tuple[Path, str]:
        """Normalise the ``dst_dir`` argument for ``mode == "single"``.

        Always returns ``(directory, filename)`` so callers can ``mkdir`` the
        directory without risk of stepping on the eventual file path.

        Heuristic:
          * existing directory          -> ``(dst_dir, default_name)``
          * existing file               -> ``(dst_dir.parent, dst_dir.name)``
          * non-existent, suffix matches src extension (e.g. ``.pdf``)
                                        -> ``(dst_dir.parent, dst_dir.name)``
          * non-existent, suffix is in the pseudo list (``.anonymized`` ...)
                                        -> ``(dst_dir, default_name)``
          * non-existent, any other suffix
                                        -> ``(dst_dir.parent, dst_dir.name)``
          * no suffix                   -> ``(dst_dir, default_name)``
        """
        default_name = f"{src.stem}.anonymized{src.suffix}"
        if dst_dir is None:
            return src.parent, default_name
        p = Path(dst_dir).expanduser()
        if p.exists() and p.is_dir():
            return _abs(p), default_name
        if p.exists() and p.is_file():
            return _abs(p.parent), p.name
        suf = p.suffix.lower()
        if not suf:
            return _abs(p), default_name
        if suf in Project._NON_FILE_PSEUDO_SUFFIXES:
            return _abs(p), default_name
        return _abs(p.parent), p.name

    @classmethod
    def for_single_file(
        cls, src: str | Path, dst_dir: str | Path | None = None, **kw
    ) -> "Project":
        src_p = _abs(src)
        out_dir, out_name = cls._split_single_dst(src_p, dst_dir)
        return cls(
            mode="single",
            input_paths=[src_p],
            output_dir=out_dir,
            single_output_filename=out_name,
            **kw,
        )

    @classmethod
    def for_multi_file(
        cls, sources: Iterable[str | Path], dst_dir: str | Path, **kw
    ) -> "Project":
        return cls(
            mode="multi",
            input_paths=[_abs(p) for p in sources],
            output_dir=_abs(dst_dir),
            **kw,
        )

    @classmethod
    def for_folder(
        cls, src_dir: str | Path, dst_dir: str | Path, **kw
    ) -> "Project":
        return cls(
            mode="folder",
            input_paths=[_abs(src_dir)],
            output_dir=_abs(dst_dir),
            **kw,
        )

    @classmethod
    def autodetect(
        cls,
        sources: Iterable[str | Path],
        dst_dir: str | Path | None = None,
        **kw,
    ) -> "Project":
        ps = [_abs(p) for p in sources]
        if not ps:
            raise ValueError("no source paths")
        if len(ps) == 1 and ps[0].is_dir():
            if dst_dir is None:
                dst = ps[0].parent / ("Anonymized_" + ps[0].name)
            else:
                dst = _abs(dst_dir)
            return cls(mode="folder", input_paths=ps, output_dir=dst, **kw)
        if len(ps) == 1 and ps[0].is_file():
            return cls.for_single_file(ps[0], dst_dir, **kw)
        if dst_dir is None:
            dst = ps[0].parent / "Anonymized_multi"
        else:
            dst = _abs(dst_dir)
        return cls(mode="multi", input_paths=ps, output_dir=dst, **kw)

    def output_path_for(self, scanned) -> Path:
        """Where the anonymized file goes for a given ``ScannedFile``."""
        if self.mode == "single":
            src = self.input_paths[0]
            name = self.single_output_filename or f"{src.stem}.anonymized{src.suffix}"
            return self.output_dir / name
        if self.mode == "multi":
            return self.output_dir / scanned.path.name
        return self.output_dir / scanned.rel

    def state_path(self) -> Path:
        return self.output_dir / ".anon" / "state.json"

    def manifest_path(self) -> Path:
        return self.output_dir / ".anon" / "run_manifest.json"

    def logs_dir(self) -> Path:
        return self.output_dir / ".anon" / "logs"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
            if isinstance(v, list):
                d[k] = [str(x) if isinstance(x, Path) else x for x in v]
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


__all__ = ["Project", "InputMode"]
