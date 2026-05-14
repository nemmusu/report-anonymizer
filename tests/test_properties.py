"""Property-based tests for the substitution engine."""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from anonymize.format_adapters.base import SubstitutionRule, apply_to_text


@given(text=st.text(min_size=0, max_size=200))
@settings(max_examples=80)
def test_apply_with_no_rules_is_identity(text: str) -> None:
    out, evs = apply_to_text(text, [])
    assert out == text and evs == []


@given(
    text=st.text(min_size=0, max_size=200),
    pairs=st.lists(
        st.tuples(
            st.text(alphabet="abcdef", min_size=1, max_size=6),
            st.text(alphabet="ZXYW", min_size=1, max_size=6),
        ),
        min_size=0,
        max_size=5,
    ),
)
@settings(max_examples=120)
def test_apply_idempotent_when_to_disjoint_from_from(text, pairs) -> None:
    rules = [SubstitutionRule(from_=f, to=t, category="other") for f, t in pairs if f != t]
    out1, _ = apply_to_text(text, rules)
    out2, _ = apply_to_text(out1, rules)
    assert out1 == out2
