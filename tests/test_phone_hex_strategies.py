"""End-to-end tests for the placeholder strategies wired into
``rules_pass``. The shape-preserving generators themselves are
unit-tested in ``tests/test_placeholders.py`` against the public API in
``anonymize.placeholders``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize.decisions_log import DecisionsLog
from anonymize.placeholders import (
    _canonical_phone,
    hex_zero_seq as _hex_zero_seq,
    phone_intl as _phone_placeholder,
)
from anonymize.rules_pass import (
    _CompiledRule,
    _resolve_placeholder,
    run_rules_pass,
)
from anonymize.scanner import scan_path


PATTERNS = Path(__file__).parent.parent / "config" / "leak_patterns.yml"


@pytest.fixture
def log(tmp_path):
    return DecisionsLog.load(tmp_path / "decisions.jsonl")


# ---- canonicalisation ------------------------------------------------------


@pytest.mark.parametrize(
    "value,canonical",
    [
        ("+393440405580", "+393440405580"),
        ("393440405580", "+393440405580"),
        ("3440405580", "+393440405580"),
        ("+39 344 0405580", "+393440405580"),
        ("+39 344 040 5580", "+393440405580"),
        # bare 10-digit IT mobile (with stray + prefix): normalize to full E.164
        ("+3881681931", "+393881681931"),
        # already canonical 12-digit international: keep as-is (normalised plus)
        ("+15555550101", "+15555550101"),
    ],
)
def test_canonical_phone(value, canonical):
    assert _canonical_phone(value) == canonical


# ---- phone strategy --------------------------------------------------------


def test_phone_placeholder_preserves_length(log):
    for v in [
        "+393440405580",
        "393440405580",
        "3440405580",
        "+3881681931",
        "+393881681931",
    ]:
        out = _phone_placeholder(v, log=log, rule_name="italian_mobile")
        assert len(out) == len(v), f"length differs for {v!r}: got {out!r}"


def test_phone_placeholder_preserves_plus_prefix(log):
    out_with = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    out_without = _phone_placeholder("393440405580", log=log, rule_name="italian_mobile")
    assert out_with.startswith("+")
    assert not out_without.startswith("+")


def test_phone_placeholder_preserves_carrier_prefix(log):
    """The first 3 digits of the local number (carrier identifier) are kept."""
    out = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    # output shape: +39 <carrier 3 digits> <7 zeros + seq>
    assert out.startswith("+39344")
    out2 = _phone_placeholder("+393881681931", log=log, rule_name="italian_mobile")
    assert out2.startswith("+39388")


def test_phone_placeholder_canonical_consistency(log):
    """Same physical number across formats must share the same trailing index."""
    a = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    b = _phone_placeholder("393440405580", log=log, rule_name="italian_mobile")
    c = _phone_placeholder("3440405580", log=log, rule_name="bare_it_mobile")
    # extract the trailing 7-digit sequence
    sa = a[-7:]
    sb = b[-7:]
    sc = c[-7:]
    assert sa == sb == sc


def test_phone_placeholder_different_numbers_different_indices(log):
    a = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    b = _phone_placeholder("+393881681931", log=log, rule_name="italian_mobile")
    assert a[-7:] != b[-7:]


def test_phone_placeholder_idempotent_within_log(log):
    a1 = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    a2 = _phone_placeholder("+393440405580", log=log, rule_name="italian_mobile")
    assert a1 == a2


def test_phone_resolve_via_strategy_uses_phone_it(log):
    rule = _CompiledRule(
        name="italian_mobile",
        category="phones",
        pattern=__import__("re").compile(r"\+?39\d{10}"),
        placeholder_template="",
        placeholder_strategy="phone_it",
    )
    out = _resolve_placeholder(rule, "+393440405580", log=log)
    assert out.startswith("+39344") and len(out) == len("+393440405580")


# ---- hex strategy ----------------------------------------------------------


def test_hex_zero_seq_preserves_length_and_distinguishability(log):
    a = _hex_zero_seq("b4fc4171101438774f186f41057e0b1d", log=log, rule_name="hex_credentials_32")
    b = _hex_zero_seq("328cd0ceba38fb6b8c9af4fe9d6c43fd", log=log, rule_name="hex_credentials_32")
    assert len(a) == 32 and len(b) == 32
    assert a != b
    # mostly zeros except trailing index
    assert a.startswith("0" * 24)
    assert b.startswith("0" * 24)


def test_hex_zero_seq_idempotent(log):
    a1 = _hex_zero_seq("aa" * 16, log=log, rule_name="hex_credentials_32")
    a2 = _hex_zero_seq("AA" * 16, log=log, rule_name="hex_credentials_32")
    assert a1 == a2


# ---- end-to-end via run_rules_pass ----------------------------------------


def test_run_rules_pass_phones_and_keys_real_strategies(tmp_path, monkeypatch):
    """End-to-end: write a tiny dossier with phones+keys, scan + run rules,
    verify each candidate has a strategy-shaped placeholder."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "advisory.md").write_text(
        "Phones: +393440405580, +393881681931, bare 3337310009\n"
        "Key: b4fc4171101438774f186f41057e0b1d\n"
        "Other key: 328cd0ceba38fb6b8c9af4fe9d6c43fd\n",
        encoding="utf-8",
    )
    scan = scan_path(src)
    log = DecisionsLog.load(tmp_path / "dec.jsonl")
    cands, _ = run_rules_pass(
        scan,
        patterns_path=PATTERNS,
        decisions=log,
    )
    by_value = {c.value: c for c in cands}

    # Phones -> length-preserving + carrier-preserving
    p1 = by_value["+393440405580"]
    assert len(p1.suggested_placeholder) == len("+393440405580")
    assert p1.suggested_placeholder.startswith("+39344")

    p2 = by_value["+393881681931"]
    assert len(p2.suggested_placeholder) == len("+393881681931")
    assert p2.suggested_placeholder.startswith("+39388")

    p3 = by_value["3337310009"]
    assert len(p3.suggested_placeholder) == 10
    assert p3.suggested_placeholder.startswith("333")

    # Distinct phone numbers -> distinct trailing indices
    assert p1.suggested_placeholder[-7:] != p2.suggested_placeholder[-7:]

    # Hex keys -> length-preserving + distinguishable, with the first 8
    # characters of the source preserved (the new ``hex_keep_prefix``
    # strategy: pentest readers can still tell that two anonymized
    # credentials originally shared a prefix).
    k1 = by_value["b4fc4171101438774f186f41057e0b1d"]
    k2 = by_value["328cd0ceba38fb6b8c9af4fe9d6c43fd"]
    assert len(k1.suggested_placeholder) == 32
    assert len(k2.suggested_placeholder) == 32
    assert k1.suggested_placeholder != k2.suggested_placeholder
    assert k1.suggested_placeholder.startswith("b4fc4171")
    assert k2.suggested_placeholder.startswith("328cd0ce")
