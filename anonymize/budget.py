"""Pre-flight token-budget estimator for the chunked detector pipeline.

The detector slices each input segment into chunks of at most
``DetectorConfig.max_chunk_chars`` characters and sends them to
llama-server one-per-request. The server provisions a *slot* of
``ctx_size / parallel`` tokens for each concurrent request. A request
must fit in its slot, including:

  * the system prompt (detector + critic + audit share the same
    structural budget),
  * the few-shot examples (capped at 8 from the decisions log,
    typically ~25 tokens each),
  * the chunk body (≤ ``max_chunk_chars`` characters),
  * the JSON output budget passed to llama-server as ``max_tokens``.

If the slot is too small the request OOMs at server side. This module
estimates the required tokens up-front so the GUI's Run gate can warn
the operator BEFORE the pipeline launches.
"""
from __future__ import annotations

from dataclasses import dataclass


# Calibration, measured against the actual prompts the pipeline
# ships and the live benchmark runs.  ``llama.cpp`` needs to hold
# the full prompt + all generated tokens in its slot KV cache, so
# we size the budget to:
#
#   slot >= system_prompt + few_shot + chunk + max_output_tokens
#
# Numbers below are rounded UP from real measurements so the
# pre-flight refuses configurations that would actually OOM, but
# not so high that working presets get spuriously rejected.
_CHARS_PER_TOKEN = 3.5
# The detector path is the bottleneck. ``prompts/system_detector.txt``
# is 12 748 chars; at 3.5 chars/token that's ~3 640 tokens. We round
# up to 3 700 to leave a small margin for the safe-terms expansion
# the template injects.
_SYSTEM_PROMPT_TOKENS = 3700
# Few-shot: top 8 promote decisions. Empty on first run; populated
# from ``decisions_history.jsonl`` on subsequent runs. ~30 tokens
# per example after JSON formatting.
_FEW_SHOT_TOKENS = 250
# Output budget passed to ``LLMClient.chat`` (``max_tokens``). The
# detector / critic JSON responses come well under 1 000 tokens in
# practice but we size for the configured max so the pre-flight is
# correct even for an unusually long candidate batch.
_DEFAULT_MAX_OUTPUT_TOKENS = 2048
_DEFAULT_MAX_CHUNK_CHARS = 5000


@dataclass
class BudgetEstimate:
    per_request_tokens: int
    slot_tokens: int
    fits: bool
    reason: str = ""

    @property
    def headroom_tokens(self) -> int:
        return self.slot_tokens - self.per_request_tokens

    def explain(self) -> str:
        if self.fits:
            return (
                f"Budget OK: ~{self.per_request_tokens} tokens / request "
                f"vs {self.slot_tokens} per slot "
                f"(headroom {self.headroom_tokens})."
            )
        return self.reason


def estimate_per_request_tokens(
    *,
    max_chunk_chars: int = _DEFAULT_MAX_CHUNK_CHARS,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
    chars_per_token: float = _CHARS_PER_TOKEN,
) -> int:
    """Approximate token cost of a single detector/critic request."""
    chunk_tokens = int(max_chunk_chars / max(0.1, chars_per_token))
    return _SYSTEM_PROMPT_TOKENS + _FEW_SHOT_TOKENS + chunk_tokens + max_output_tokens


def check_slot_budget(
    *,
    ctx_size: int,
    parallel: int,
    max_chunk_chars: int = _DEFAULT_MAX_CHUNK_CHARS,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
) -> BudgetEstimate:
    """Return whether one request fits in a single slot of the running
    server profile. Use this *before* launching the pipeline to refuse
    configurations that would OOM at the first chunk.
    """
    parallel = max(1, int(parallel))
    slot_tokens = max(1, int(ctx_size)) // parallel
    per_request = estimate_per_request_tokens(
        max_chunk_chars=max_chunk_chars,
        max_output_tokens=max_output_tokens,
    )
    fits = per_request <= slot_tokens
    if fits:
        return BudgetEstimate(
            per_request_tokens=per_request,
            slot_tokens=slot_tokens,
            fits=True,
        )
    suggestion: list[str] = []
    # Guide the user toward a working configuration.  Boost ctx first
    # (cheap), then drop parallel.
    target_ctx = per_request * parallel
    suggestion.append(
        f"increase ctx_size to ≥ {target_ctx} (currently {ctx_size})"
    )
    if parallel > 1:
        suggestion.append(
            f"or reduce parallel to {max(1, ctx_size // per_request)} "
            f"(currently {parallel})"
        )
    reason = (
        f"A single request needs ~{per_request} tokens "
        f"(system + few-shot + chunk {max_chunk_chars} chars + "
        f"output {max_output_tokens}), "
        f"but each parallel slot only has {slot_tokens} tokens "
        f"({ctx_size} ctx ÷ {parallel} parallel). "
        f"Llama-server would OOM on the first chunk.  "
        f"Fix: " + "; ".join(suggestion) + "."
    )
    return BudgetEstimate(
        per_request_tokens=per_request,
        slot_tokens=slot_tokens,
        fits=False,
        reason=reason,
    )


__all__ = [
    "BudgetEstimate",
    "estimate_per_request_tokens",
    "check_slot_budget",
]
