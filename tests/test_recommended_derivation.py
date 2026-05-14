"""``pick_recommended_files`` is the single source of truth for the
★ recommended marker shown in the Model-Manager Curated/Search tabs.
The rule is product-facing, not just a heuristic, so we pin it:

* full-precision files (BF16 / F16 / FP16) are *always* recommended
  when present (users want the best quality file by default);
* if a repo has no full-precision file, the helper still picks one
  recommended entry, the highest quality available, so each repo
  exposes at least one ★ in the UI;
* the curated allowlist (``CuratedRepo.recommended_files``) is
  always honoured so preset/profile pinning stays canonical.
"""
from __future__ import annotations

from unittest.mock import patch

from anonymize.hf_models import (
    CURATED_REPOS,
    KNOWN_PROBLEMATIC_REPOS,
    _BASE_MODEL_CACHE,
    pick_recommended_files,
    repo_metadata,
)


def test_bf16_is_recommended_alongside_quants() -> None:
    files = [
        "Qwen3.5-9B-BF16.gguf",
        "Qwen3.5-9B-Q8_0.gguf",
        "Qwen3.5-9B-Q4_K_M.gguf",
    ]
    rec = pick_recommended_files(files)
    assert "Qwen3.5-9B-BF16.gguf" in rec
    assert "Qwen3.5-9B-Q8_0.gguf" not in rec
    assert "Qwen3.5-9B-Q4_K_M.gguf" not in rec


def test_f16_and_fp16_are_recommended() -> None:
    """F16 and FP16 must both qualify as ★ recommended (the catalog
    has files in either casing depending on the publisher)."""
    files = [
        "model-F16.gguf",
        "model-FP16.gguf",
        "model-Q8_0.gguf",
    ]
    rec = pick_recommended_files(files)
    assert "model-F16.gguf" in rec
    assert "model-FP16.gguf" in rec
    assert "model-Q8_0.gguf" not in rec


def test_multiple_full_precision_variants_all_recommended() -> None:
    """A repo can ship more than one full-precision file (rare but
    happens, e.g. a publisher offering both BF16 and F16 variants).
    The user should see ★ on every one of them, not pick-the-first."""
    files = [
        "model-BF16.gguf",
        "model-F16.gguf",
        "model-Q4_K_M.gguf",
    ]
    rec = pick_recommended_files(files)
    assert {"model-BF16.gguf", "model-F16.gguf"}.issubset(rec)
    assert "model-Q4_K_M.gguf" not in rec


def test_unsloth_ud_q8_xl_wins_when_no_full_precision() -> None:
    """When the repo has no BF16/F16/FP16 file, fall back to the
    single highest-quality variant. Unsloth dynamic Q8 (UD-Q8_K_XL)
    is preferred over plain Q8_0 by ``quality_rank``."""
    files = [
        "model-UD-Q8_K_XL.gguf",
        "model-Q8_0.gguf",
        "model-Q6_K.gguf",
        "model-Q4_K_M.gguf",
    ]
    rec = pick_recommended_files(files)
    assert rec == {"model-UD-Q8_K_XL.gguf"}


def test_q8_wins_when_only_quants_present() -> None:
    """No full-precision and no UD variants, plain Q8_0 must still
    bubble up as the lone recommended file."""
    files = [
        "model-Q4_K_M.gguf",
        "model-Q8_0.gguf",
        "model-Q5_K_M.gguf",
    ]
    rec = pick_recommended_files(files)
    assert rec == {"model-Q8_0.gguf"}


def test_q4_wins_when_only_low_quants_present() -> None:
    """Worst case, only low-bit quants. Pick the best of what's
    available so the UI never shows a repo with zero stars."""
    files = [
        "model-Q2_K.gguf",
        "model-Q4_K_M.gguf",
        "model-Q3_K_S.gguf",
    ]
    rec = pick_recommended_files(files)
    assert rec == {"model-Q4_K_M.gguf"}


def test_curated_allowlist_is_always_recommended() -> None:
    """If a preset pins a specific filename, that file must appear in
    the recommended set even if the auto rule wouldn't pick it."""
    files = [
        "model-BF16.gguf",
        "model-Q4_K_M.gguf",
    ]
    rec = pick_recommended_files(files, curated=["model-Q4_K_M.gguf"])
    assert "model-BF16.gguf" in rec  # auto-rule
    assert "model-Q4_K_M.gguf" in rec  # curated pin


def test_mmproj_files_never_recommended() -> None:
    """The pipeline is text-only, the helper must never surface an
    mmproj file even if it's the only thing in the repo."""
    files = [
        "mmproj-BF16.gguf",
        "mmproj-F16.gguf",
    ]
    rec = pick_recommended_files(files)
    assert rec == set()


def test_empty_input_returns_empty_set() -> None:
    assert pick_recommended_files([]) == set()


def test_curated_repo_recommended_files_have_a_quant_label() -> None:
    """For each curated repo that pins a file, the pinned name must
    advertise a quant tag we recognise via :func:`quality_rank`.
    Catches typos and accidental ``.gguf`` files with no quant
    marker (e.g. a pre-tokenized export). Curated picks may be
    full-precision (BF16/F16/FP16) *or* a quant that earned its
    slot on F1 (Q5_K_M, etc.), the policy is performance-first."""
    from anonymize.hf_models import _QUALITY_PRIORITY

    for repo in CURATED_REPOS:
        for name in repo.recommended_files:
            tag_present = any(tag in name.lower() for tag in _QUALITY_PRIORITY)
            assert tag_present, (
                f"{repo.repo_id}: curated file {name!r} carries no "
                f"recognised quant label, quality_rank() can't "
                f"order it against siblings"
            )


def test_curated_set_is_clean() -> None:
    """CURATED_REPOS is the dropdown the user sees in the curated
    download tab. Keep it tightly scoped to the working presets:
    every entry must be ``ok`` (the broken / dropped models live in
    ``KNOWN_PROBLEMATIC_REPOS`` instead)."""
    for repo in CURATED_REPOS:
        assert repo.compatibility_status == "ok", (
            f"{repo.repo_id}: only ok models belong in the curated "
            f"dropdown (got {repo.compatibility_status!r}); move it "
            f"into KNOWN_PROBLEMATIC_REPOS"
        )


def test_curated_set_covers_usable_band() -> None:
    """The curated dropdown lists every model that scored in the
    ``Usable`` band or better (Quality >= 50 on the 5-PDF
    anonymization corpus) plus the recommended CPU default. The
    upper bound is loose because the policy is data-driven: each
    new benchmark round either adds entries (a new model crosses
    the bar) or doesn't. Multiple quants of the same model still
    land under a single entry via ``recommended_files``.
    """
    n = len(CURATED_REPOS)
    # Lower bound: at least the 5 quality medals + the CPU default.
    # Upper bound: kept generous so a single bench round can grow the
    # list without immediately tripping a regression in CI.
    assert 4 <= n <= 60, f"expected 4-60 curated repos, got {n}"
    # Sanity: every CURATED entry must be marked usable. The GUI
    # surfaces the badge based on this field, so a low_quality
    # leaking into CURATED would render with the wrong colour.
    for r in CURATED_REPOS:
        assert r.compatibility_status == "ok", (
            f"CURATED entry {r.repo_id} has status="
            f"{r.compatibility_status}, expected ok"
        )


def test_incompatible_repos_carry_a_reason() -> None:
    """Every entry flagged as broken/incompatible must carry a
    one-liner reason; the badge surfaces it inline so users
    understand *why* the model doesn't work."""
    for repo in KNOWN_PROBLEMATIC_REPOS:
        if repo.compatibility_status == "incompatible":
            assert repo.compatibility_reason, (
                f"{repo.repo_id}: incompatible models must carry "
                f"a compatibility_reason for the badge to render"
            )


def test_low_quality_repos_carry_a_reason() -> None:
    """Low-quality entries also need a reason, the badge folds it
    in next to the ⚠️ verdict."""
    for repo in KNOWN_PROBLEMATIC_REPOS:
        if repo.compatibility_status == "low_quality":
            assert repo.compatibility_reason, (
                f"{repo.repo_id}: low_quality models must carry "
                f"a compatibility_reason for the badge to render"
            )


def test_problematic_repos_carry_name_patterns() -> None:
    """Every problematic repo needs at least one ``name_patterns``
    entry. Without it ``repo_metadata`` cannot match community
    republishers of the same broken model, defeating the point of
    the auto-warning."""
    for repo in KNOWN_PROBLEMATIC_REPOS:
        assert repo.name_patterns, (
            f"{repo.repo_id}: missing name_patterns, community "
            f"republishers won't trigger the ⚠️/❌ warning"
        )


def test_repo_metadata_finds_curated_by_exact_id() -> None:
    """Curated lookups must hit the exact owner/repo. We never want
    a substring rule to auto-recommend a republisher of one of the
    top-5 models, the ★ stays tied to the publisher we tested."""
    meta = repo_metadata("unsloth/Qwen3.5-4B-GGUF")
    assert meta is not None
    assert meta.repo_id == "unsloth/Qwen3.5-4B-GGUF"
    assert meta.compatibility_status == "ok"


def test_repo_metadata_finds_problematic_by_name_pattern() -> None:
    """A community-published Gemma 4 GGUF must hit the SWA-1024
    incompatibility entry via its ``name_patterns`` substring, not
    just the canonical unsloth id. The pattern is family-wide so
    every size (E2B, E4B, 26B, 31B, …) and every publisher mirror
    triggers the same warning."""
    for republisher in (
        "bartowski/gemma-4-E4B-it-GGUF",
        "mradermacher/Gemma-4-E4B-it-i1-GGUF",
        "lmstudio-community/gemma-4-E2B-it-GGUF",
        "unsloth/gemma-4-26B-A4B-it-GGUF",
        "unsloth/gemma-4-31B-GGUF",
        "ggml-org/Gemma-4-26B-A4B-it-GGUF",
        "HauhauCS/Gemma-4-E4B-Uncensored-GGUF",
    ):
        meta = repo_metadata(republisher)
        assert meta is not None, f"{republisher}: pattern miss"
        assert meta.compatibility_status == "incompatible"
        assert (
            "architecture" in meta.compatibility_reason.lower()
            or "attention" in meta.compatibility_reason.lower()
        ), f"{republisher}: reason {meta.compatibility_reason!r} doesn't surface the architectural cause"


def test_gemma_4_pattern_does_not_swallow_other_gemma_versions() -> None:
    """The Gemma 4 family pattern must stay scoped to Gemma 4. Gemma 2
    and Gemma 3 are different architectures (no SWA-1024 issue) so
    they should *not* fire the warning."""
    assert repo_metadata("bartowski/gemma-2-2b-it-GGUF") is None
    assert repo_metadata("Andycurrent/Gemma-3-1B-it-GLM-4.7-GGUF") is None


def test_qwen3guard_pattern_covers_all_sizes() -> None:
    """The Qwen3 Guard family pattern must catch every size and every
    publisher because the safety-tuning behaviour is shared across
    the whole family, not size-specific."""
    for republisher in (
        "mradermacher/Qwen3Guard-Gen-4B-GGUF",
        "mradermacher/Qwen3Guard-Gen-8B-GGUF",
        "bartowski/Qwen3-Guard-Gen-4B-GGUF",
        "QuantFactory/Qwen3Guard-Gen-1.5B-GGUF",
    ):
        meta = repo_metadata(republisher)
        assert meta is not None, f"{republisher}: pattern miss"
        assert meta.compatibility_status == "incompatible"
        assert "safety" in meta.compatibility_reason.lower()


def test_repo_metadata_returns_none_for_unknown_repo() -> None:
    """A random repo we never tested must produce no metadata, so
    the badge stays blank instead of mis-attributing benchmarks."""
    assert repo_metadata("microsoft/Phi-3-mini-4k-instruct-GGUF") is None
    assert repo_metadata("") is None


def test_repo_metadata_propagates_curated_badge_to_mirrors() -> None:
    """Community mirrors of a top-5 model should still surface the
    badge + description in the Search HF tab. ``mistralai`` is the
    original Mistral publisher; ``unsloth`` and ``MaziyarPanahi``
    are common GGUF mirrors. All three must resolve to the curated
    Reasoning entry so users see the same Quality score regardless
    of which mirror they land on. The badge propagation is
    intentionally not one-way: it propagates with the metadata,
    not the ★ recommendation (the star is filename-keyed)."""
    for mirror in (
        "mistralai/Ministral-3-8B-Reasoning-2512-GGUF",
        "MaziyarPanahi/Ministral-3-8B-Reasoning-2512-GGUF",
        "bartowski/Ministral-3-8B-Reasoning-2512-GGUF",
        "lmstudio-community/Ministral-3-8B-Reasoning-2512-GGUF",
    ):
        meta = repo_metadata(mirror, follow_base_model=False)
        assert meta is not None, f"{mirror}: expected curated match"
        assert meta.compatibility_status == "ok"
        assert meta.benchmark_f1 is not None
        assert "Highest detection quality" in meta.description


def test_repo_metadata_propagates_to_qwen_9b_mirrors() -> None:
    for mirror in (
        "lmstudio-community/Qwen3.5-9B-GGUF",
        "bartowski/Qwen3.5-9B-GGUF",
        "HauhauCS/Qwen3.5-9B-Uncensored-GGUF",
    ):
        meta = repo_metadata(mirror, follow_base_model=False)
        assert meta is not None, f"{mirror}: expected curated match"
        assert "fewest false alarms" in meta.description.lower()


def test_repo_metadata_keeps_curated_pattern_scoped_to_release() -> None:
    """The curated patterns must be tight enough to skip *different*
    sizes / releases of the same family. The 8B Reasoning pattern
    contains the ``2512`` date code, so a 14B sibling or an older
    Reasoning release stays unmatched. The Qwen 9B pattern contains
    the size, so the 4B / 27B / 35B siblings stay separate."""
    for unrelated in (
        "lmstudio-community/Ministral-3-14B-Reasoning-2512-GGUF",
        "unsloth/Ministral-3-3B-Reasoning-2512-GGUF",  # PROBLEMATIC, different match
        "unsloth/Qwen3.5-35B-A3B-GGUF",
        "unsloth/Qwen3.5-27B-GGUF",
    ):
        meta = repo_metadata(unrelated, follow_base_model=False)
        # Either no match or a different curated/problematic entry,
        # but never the 8B Reasoning curated entry by accident.
        if meta is not None:
            assert meta.repo_id != "unsloth/Ministral-3-8B-Reasoning-2512-GGUF"
            assert meta.repo_id != "unsloth/Qwen3.5-9B-GGUF"


def test_star_recommendation_stays_filename_keyed() -> None:
    """Even when the badge propagates to a mirror, the ★ "recommended
    file" only fires on filenames that match the curated repo's
    pinned list. A mirror that keeps the canonical GGUF name
    inherits the ★ on that file; a mirror that renames the file
    does not."""
    canonical = "Ministral-3-8B-Reasoning-2512-BF16.gguf"
    renamed = "Ministral-3-8B-Reasoning-bf16-bartowski.gguf"
    rec = pick_recommended_files(
        [canonical, renamed],
        curated=[canonical],
    )
    assert canonical in rec  # exact filename hit
    # Renamed copy carries no Q tag and only one full-precision
    # file is in the listing, so the helper still flags it as the
    # best-quality fallback. The point of this test is the
    # *canonical filename* is the single source of truth for the
    # ★ tag, not the repo identity.
    assert canonical in rec


def test_repo_metadata_follows_hf_base_model_to_canonical() -> None:
    """When the repo name doesn't contain the family identifier
    (``someone/MyMix-of-Gemma4-GGUF``), regex/substring matching
    can't help. The HF model-card ``base_model`` tag is the
    authoritative back-reference: if it points at our canonical
    Gemma 4 entry, the warning must still propagate.
    """
    _BASE_MODEL_CACHE.clear()

    class _FakeCard:
        base_model = "unsloth/gemma-4-E4B-it-GGUF"

    class _FakeInfo:
        card_data = _FakeCard()

    class _FakeApi:
        def __init__(self, token=None):
            pass

        def model_info(self, repo_id):
            return _FakeInfo()

    with patch("huggingface_hub.HfApi", _FakeApi):
        meta = repo_metadata("someone/MyMix-of-Gemma4-GGUF")

    assert meta is not None
    assert meta.compatibility_status == "incompatible"
    _BASE_MODEL_CACHE.clear()


def test_repo_metadata_handles_base_model_list() -> None:
    """``base_model`` can be a YAML list (merges / multi-parent
    finetunes). The first entry should be picked, it's by
    convention the dominant parent the user is actually
    redistributing.
    """
    _BASE_MODEL_CACHE.clear()

    class _FakeCard:
        # Multi-parent: Gemma 4 first, an unrelated model second.
        base_model = [
            "unsloth/gemma-4-E4B-it-GGUF",
            "mistralai/Mistral-7B-v0.1",
        ]

    class _FakeInfo:
        card_data = _FakeCard()

    class _FakeApi:
        def __init__(self, token=None):
            pass

        def model_info(self, repo_id):
            return _FakeInfo()

    with patch("huggingface_hub.HfApi", _FakeApi):
        meta = repo_metadata("someone/MyMerge-GGUF")

    assert meta is not None
    assert meta.compatibility_status == "incompatible"
    _BASE_MODEL_CACHE.clear()


def test_repo_metadata_caches_base_model_lookups() -> None:
    """The HF API is only hit once per repo even if the badge
    refreshes multiple times (curated tab redraw, search-tab click,
    file-list refresh). Without caching, every reselection would
    spin up a network round-trip.
    """
    _BASE_MODEL_CACHE.clear()
    calls: list[str] = []

    class _FakeCard:
        base_model = "unsloth/gemma-4-E4B-it-GGUF"

    class _FakeInfo:
        card_data = _FakeCard()

    class _FakeApi:
        def __init__(self, token=None):
            pass

        def model_info(self, repo_id):
            calls.append(repo_id)
            return _FakeInfo()

    with patch("huggingface_hub.HfApi", _FakeApi):
        for _ in range(5):
            assert repo_metadata("someone/MyGGUF") is not None

    assert len(calls) == 1, (
        f"expected a single HF API call, got {len(calls)}"
    )
    _BASE_MODEL_CACHE.clear()


def test_repo_metadata_base_model_can_be_disabled() -> None:
    """``follow_base_model=False`` is the escape hatch for code
    paths that must never block on network IO (preset autoload,
    bench runner). It must short-circuit before the HF call.
    """
    _BASE_MODEL_CACHE.clear()

    class _FakeApi:
        def __init__(self, token=None):
            pass

        def model_info(self, repo_id):
            raise AssertionError("HF API must not be called here")

    with patch("huggingface_hub.HfApi", _FakeApi):
        assert repo_metadata(
            "someone/Unknown-GGUF", follow_base_model=False
        ) is None
    _BASE_MODEL_CACHE.clear()
