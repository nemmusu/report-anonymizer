"""Every shipped preset must pass the slot-budget pre-flight check.

This test is the authoritative guard that prevents anyone (us, a
contributor, a stale config migration) from shipping a preset whose
``ctx_size / parallel`` slot can't hold a single detector request.
If this test fails, llama-server would OOM on the very first chunk.
"""
from __future__ import annotations

import pytest

from anonymize.budget import check_slot_budget
from anonymize.server_profile import load_profiles


# Names of every preset shipped in ``config/server_profiles.yml``.
# Keeping this list in sync with the YAML is intentional: the test is
# the gate that catches "we added a preset whose slot can't fit a
# detector request", which is only catchable with hard-coded names.
_BUILTIN = (
    "default",
    "ministral-3-8b-reasoning-bf16",
    "rtila-qwen3.5-9b-q4",
    "jackrong-qwen3.5-4b-distill-q4",
    "qwen3.5-9b-bf16",
    "ministral-3-8b-reasoning-q5",
)


@pytest.mark.parametrize("preset", _BUILTIN)
def test_preset_slot_budget(preset: str) -> None:
    profiles = {p.name: p for p in load_profiles()}
    p = profiles.get(preset)
    assert p is not None, f"preset {preset!r} missing from load_profiles()"
    est = check_slot_budget(ctx_size=p.ctx_size, parallel=p.parallel)
    assert est.fits, (
        f"{preset!r} fails slot-budget pre-flight: {est.reason}\n"
        f"  ctx_size={p.ctx_size}, parallel={p.parallel}, "
        f"slot={est.slot_tokens}, required≈{est.per_request_tokens}"
    )


def test_default_has_meaningful_headroom() -> None:
    """The default preset is what most users land on; require at
    least 500 tokens of headroom on top of the worst-case request
    so unusual chunks (long table, long code fence) don't get
    rejected by the server."""
    profiles = {p.name: p for p in load_profiles()}
    p = profiles["default"]
    est = check_slot_budget(ctx_size=p.ctx_size, parallel=p.parallel)
    assert est.headroom_tokens >= 500, (
        f"default preset has only {est.headroom_tokens} tokens of "
        f"headroom, bump ctx_size or reduce parallel."
    )
