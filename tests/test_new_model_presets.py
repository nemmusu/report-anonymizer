"""The curated preset list must load, inherit defaults, and point at
files declared in their curated repos.

Top-5 quality presets shipped:
* ministral-3-8b-reasoning-bf16  (Q=83)
* rtila-qwen3.5-9b-q4            (Q=82)
* qwen3.5-9b-bf16                (Q=78)
* jackrong-qwen3.5-4b-distill-q4 (Q=78)
* ministral-3-8b-reasoning-q5    (Q=76)

Plus ``default`` (Jackrong 4B Q4_K_M distill on CPU, same model as
``jackrong-qwen3.5-4b-distill-q4`` but with ``n_gpu_layers=0``;
smallest+best preset for users with no GPU). See ``BENCHMARKS.md``
for the per-model numbers.

Curation policy is *quality first, format second*: a quant earns
the slot when it beats the next contender outright (the Q4 distills
of Qwen 3.5 4B / 9B are the canonical examples).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from anonymize.hf_models import CURATED_REPOS
from anonymize.server_profile import (
    builtin_profiles_path,
    load_profiles,
    render_command,
)


def _builtin_profile(name: str) -> dict:
    """Read a single profile dict straight from the shipped YAML.

    Bypasses the merged ``load_profiles()`` view so policy assertions
    (``default`` is CPU+Q5 etc.) test what we actually ship, not what
    a developer's local user override happens to say.
    """
    data = yaml.safe_load(builtin_profiles_path().read_text(encoding="utf-8"))
    for p in data.get("profiles", []):
        if isinstance(p, dict) and p.get("name") == name:
            return p
    raise AssertionError(f"builtin profile {name!r} missing")


_NEW_PRESETS = (
    "default",
    "ministral-3-8b-reasoning-bf16",
    "ministral-3-8b-reasoning-q5",
    "rtila-qwen3.5-9b-q4",
    "jackrong-qwen3.5-4b-distill-q4",
    "qwen3.5-9b-bf16",
)


_NEW_REPOS = {
    "unsloth/Qwen3.5-4B-GGUF",
    "unsloth/Qwen3.5-9B-GGUF",
    "rtila-corporation/rtila-assistant-lite-1.5",
    "Jackrong/Qwen3.5-4B-Claude-4.6-Opus-Reasoning-Distilled-GGUF",
    "Jackrong/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-GGUF",
    # Reasoning is consolidated under one repo; both BF16 and Q5_K_M
    # builds are pinned in ``recommended_files`` so the curated
    # dropdown stays one-row-per-model.
    "unsloth/Ministral-3-8B-Reasoning-2512-GGUF",
}


def test_new_presets_are_loaded() -> None:
    profiles = {p.name: p for p in load_profiles()}
    for name in _NEW_PRESETS:
        assert name in profiles, f"missing preset {name!r}"


def test_new_presets_inherit_default_binary() -> None:
    """``extends: default`` must propagate ``binary`` (else llama-server
    cannot start without the user editing the preset first)."""
    profiles = {p.name: p for p in load_profiles()}
    default_binary = profiles["default"].binary
    for name in _NEW_PRESETS:
        p = profiles[name]
        assert p.binary == default_binary, (
            f"{name}: binary should inherit from default, got {p.binary!r}"
        )


def test_new_presets_render_required_flags() -> None:
    profiles = {p.name: p for p in load_profiles()}
    for name in _NEW_PRESETS:
        cmd = render_command(profiles[name])
        assert "--ctx-size" in cmd
        assert "--parallel" in cmd
        assert "-m" in cmd or "--model" in cmd
        # GGUF path must end in .gguf so HF download knows what to fetch.
        model_arg_idx = cmd.index("-m") if "-m" in cmd else cmd.index("--model")
        assert cmd[model_arg_idx + 1].endswith(".gguf"), cmd[model_arg_idx + 1]


def test_curated_catalog_lists_new_repos() -> None:
    repo_ids = {r.repo_id for r in CURATED_REPOS}
    assert _NEW_REPOS.issubset(repo_ids)


def test_preset_filename_matches_curated_recommended() -> None:
    """Each preset's ``model_filename`` should appear in its repo's curated
    recommended_files list, so the Model Manager keeps a single source of
    truth between presets and the catalog. ``cpu_only`` reuses the default
    model so it gets covered by the default check."""
    profiles = {p.name: p for p in load_profiles()}
    by_repo = {r.repo_id: set(r.recommended_files) for r in CURATED_REPOS}
    for name in _NEW_PRESETS:
        p = profiles[name]
        assert p.model_filename, f"{name}: model_filename is empty"
        assert p.model_repo, f"{name}: model_repo is empty"
        recs = by_repo.get(p.model_repo, set())
        assert p.model_filename in recs, (
            f"{name}: filename {p.model_filename!r} not declared in "
            f"recommended_files of {p.model_repo!r} (have {sorted(recs)})"
        )


def test_default_is_smallest_best_cpu() -> None:
    """The shipped default preset is the smallest GGUF that still
    clears the curated quality bar, routed to CPU. Currently the
    Jackrong Claude-Opus distill of Qwen 3.5 4B in Q4_K_M (Q=78,
    ~2.5 GB on disk).

    Reads the builtin YAML directly so a developer's user-scope
    override (``~/.config/document-anonymizer/server.yml``) cannot
    mask the policy.
    """
    default = _builtin_profile("default")
    fname = default.get("model_filename") or ""
    assert "Q4_K_M" in fname, (
        f"default preset must point at the Q4_K_M distill GGUF; "
        f"got {fname!r}"
    )
    assert "4B" in fname, (
        f"default preset must point at the 4B build; got {fname!r}"
    )
    assert default.get("n_gpu_layers") == 0, (
        "default must run on CPU (n_gpu_layers=0), got "
        f"{default.get('n_gpu_layers')!r}"
    )


def test_no_q8_kv_cache_on_f16_models() -> None:
    """Mixing q8 KV cache with an f16/bf16 weight model trades quality
    for almost no VRAM saving, the curated presets must keep the
    cache at f16 across the board."""
    profiles = {p.name: p for p in load_profiles()}
    for name in _NEW_PRESETS:
        p = profiles[name]
        if (
            "BF16" in p.model_filename
            or "F16" in p.model_filename.upper()
        ):
            assert p.cache_type_k == "f16", (
                f"{name}: BF16/F16 model must use f16 KV cache "
                f"(got K={p.cache_type_k!r})"
            )
            assert p.cache_type_v == "f16", (
                f"{name}: BF16/F16 model must use f16 KV cache "
                f"(got V={p.cache_type_v!r})"
            )


def test_top_f1_winner_is_ministral_reasoning() -> None:
    """The F1 leader on our corpus is the reasoning Ministral 3 8B."""
    by_id = {c.repo_id: c for c in CURATED_REPOS}
    winner = max(
        (c for c in CURATED_REPOS if c.benchmark_f1 is not None),
        key=lambda c: c.benchmark_f1,
    )
    assert winner.repo_id == "unsloth/Ministral-3-8B-Reasoning-2512-GGUF", (
        f"unexpected F1 leader: {winner.repo_id} (F1={winner.benchmark_f1})"
    )
    assert winner.benchmark_f1 >= 0.80, (
        f"F1 leader scored {winner.benchmark_f1}; expected >= 0.80"
    )
