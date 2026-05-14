"""Unit tests for shape-preserving placeholder generators."""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize.decisions_log import DecisionsLog
from anonymize.placeholders import (
    app_pkg_placeholder,
    brand_placeholder,
    email_placeholder,
    generic_keep_prefix,
    header_placeholder,
    hex_keep_prefix,
    hex_zero_seq,
    hostname_placeholder,
    infra_id_placeholder,
    ipv4_placeholder,
    phone_intl,
    resolve_strategy,
)


@pytest.fixture
def log(tmp_path: Path) -> DecisionsLog:
    return DecisionsLog.load(tmp_path / "decisions.jsonl")


# --- phones ----------------------------------------------------------------


def test_phone_preserves_length_for_any_locale(log: DecisionsLog) -> None:
    samples = [
        "+393440405580",
        "+1 (415) 555-1234",
        "+44 7700 900123",
        "+33-1-23-45-67-89",
        "3440405580",
        "393440405580",
    ]
    for s in samples:
        out = phone_intl(s, log=log, rule_name="t")
        assert len(out) == len(s), f"length mismatch for {s!r}: {out!r}"


def test_phone_preserves_separators(log: DecisionsLog) -> None:
    src = "+1 (415) 555-1234"
    out = phone_intl(src, log=log, rule_name="t")
    # Country code + area code (the carrier slot) are preserved.
    assert out.startswith("+1 (415) ")
    # Every non-digit char in the source must appear at the same offset.
    for i, ch in enumerate(src):
        if not ch.isdigit():
            assert out[i] == ch, f"separator drift at offset {i}"
    # The local number is replaced by zeros + sequential index, not the
    # original 555-1234.
    assert "555-1234" not in out


def test_phone_preserves_country_and_carrier(log: DecisionsLog) -> None:
    out = phone_intl("+393334567890", log=log, rule_name="t")
    # +39 + carrier 333 must survive
    assert out.startswith("+39333")


def test_phone_canonical_consistency(log: DecisionsLog) -> None:
    # Same physical Italian mobile in three formats must produce
    # placeholders that share the trailing index.
    a = phone_intl("+393440405580", log=log, rule_name="t")
    b = phone_intl("393440405580", log=log, rule_name="t")
    c = phone_intl("3440405580", log=log, rule_name="t")
    a_digits = "".join(ch for ch in a if ch.isdigit())
    b_digits = "".join(ch for ch in b if ch.isdigit())
    c_digits = "".join(ch for ch in c if ch.isdigit())
    # The trailing digits (the seq index) match across formats.
    assert a_digits[-4:] == b_digits[-4:] == c_digits[-4:]


def test_phone_distinct_numbers_get_distinct_placeholders(log: DecisionsLog) -> None:
    a = phone_intl("+14155551234", log=log, rule_name="t")
    b = phone_intl("+14155557890", log=log, rule_name="t")
    assert a != b


# --- hex_keep_prefix --------------------------------------------------------


def test_hex_keep_prefix_preserves_length_and_prefix(log: DecisionsLog) -> None:
    src = "nfdddf80a3b1c4d5e6f70011223344556"
    # 32-char hex (well, hex-ish, function must work for any opaque token)
    out = hex_keep_prefix(src, log=log, rule_name="hex_credentials_32")
    assert len(out) == len(src)
    assert out.startswith(src[:8])


def test_hex_keep_prefix_distinguishable_same_prefix(log: DecisionsLog) -> None:
    src1 = "nfdddf80" + "a" * 24
    src2 = "nfdddf80" + "b" * 24
    out1 = hex_keep_prefix(src1, log=log, rule_name="hex")
    out2 = hex_keep_prefix(src2, log=log, rule_name="hex")
    assert out1 != out2
    assert out1.startswith("nfdddf80") and out2.startswith("nfdddf80")


def test_hex_keep_prefix_64_chars(log: DecisionsLog) -> None:
    src = "deadbeef" + "0" * 56
    out = hex_keep_prefix(src, log=log, rule_name="hex64")
    assert len(out) == 64
    assert out.startswith("deadbeef")


# --- hex_zero_seq (legacy) -------------------------------------------------


def test_hex_zero_seq_preserves_length(log: DecisionsLog) -> None:
    src = "f" * 32
    out = hex_zero_seq(src, log=log, rule_name="hex")
    assert len(out) == 32
    # All-zero except a short trailing index
    assert out.startswith("0" * 24)


# --- email ------------------------------------------------------------------


def test_email_preserves_length(log: DecisionsLog) -> None:
    src = "mario.rossi@acme.example"
    out = email_placeholder(src, log=log, rule_name="email")
    assert len(out) == len(src)
    assert "@" in out


def test_email_short(log: DecisionsLog) -> None:
    src = "a@b.cc"
    out = email_placeholder(src, log=log, rule_name="email")
    assert len(out) == len(src)


# --- hostname ---------------------------------------------------------------


def test_hostname_keeps_prefix_and_length(log: DecisionsLog) -> None:
    src = "bastion-prod-01.acme.example"
    out = hostname_placeholder(src, log=log, rule_name="host")
    assert len(out) == len(src)
    assert out.startswith("bastion")


# --- ipv4 -------------------------------------------------------------------


def test_ipv4_uses_rfc5737(log: DecisionsLog) -> None:
    out = ipv4_placeholder("93.184.216.34", log=log, rule_name="ip")
    assert out.startswith("203.0.113.")


# --- brand ------------------------------------------------------------------


def test_brand_preserves_length(log: DecisionsLog) -> None:
    src = "AcmeApp"
    out = brand_placeholder(src, log=log, rule_name="brand")
    assert len(out) == len(src)
    assert out.startswith("Vendor")


# --- app packages (Android / iOS / desktop) --------------------------------


def test_app_pkg_preserves_length(log: DecisionsLog) -> None:
    src = "com.acmegsm.beta"
    out = app_pkg_placeholder(src, log=log, rule_name="pkg")
    assert len(out) == len(src)
    assert out.startswith("com.")


def test_app_pkg_handles_ios_bundle_id(log: DecisionsLog) -> None:
    """iOS bundle ids share the reverse-domain shape with Android
    packages, so a single category and strategy must cover both."""
    src = "it.acmebank.mobile"
    out = app_pkg_placeholder(src, log=log, rule_name="pkg-ios")
    assert len(out) == len(src)
    assert out.startswith("com.")


# --- header -----------------------------------------------------------------


def test_header_keeps_x_dash_prefix(log: DecisionsLog) -> None:
    src = "X-AcmeServer-Auth"
    out = header_placeholder(src, log=log, rule_name="hdr")
    assert len(out) == len(src)
    assert out.startswith("X-")


# --- generic ----------------------------------------------------------------


def test_generic_keep_prefix(log: DecisionsLog) -> None:
    src = "ABCD-1234-EFGH-5678"
    out = generic_keep_prefix(src, log=log, rule_name="g")
    assert len(out) == len(src)
    assert out.startswith("ABCD")


# --- resolver ---------------------------------------------------------------


def test_resolve_strategy_dispatches(log: DecisionsLog) -> None:
    out = resolve_strategy(
        "phone_intl", "+393440405580", log=log, rule_name="t"
    )
    assert out is not None
    assert len(out) == len("+393440405580")


def test_resolve_strategy_unknown_returns_none(log: DecisionsLog) -> None:
    assert resolve_strategy("nope", "x", log=log) is None


# --- determinism ------------------------------------------------------------


def test_phone_is_deterministic_within_same_log(log: DecisionsLog) -> None:
    a = phone_intl("+393440405580", log=log, rule_name="t")
    b = phone_intl("+393440405580", log=log, rule_name="t")
    assert a == b


# --- infra_ids (cloud / Active-Directory / infrastructure) -----------------


def test_infra_id_aws_arn_keeps_prefix(log: DecisionsLog) -> None:
    """AWS ARNs must keep the partition + service prefix and the
    resource path shape; only the 12-digit account id and the
    customer-tied resource name get rewritten."""
    src = "arn:aws:iam::123456789012:role/AdminRole"
    out = infra_id_placeholder(src, log=log, rule_name="arn")
    assert out.startswith("arn:aws:iam::")
    assert len(out) == len(src)
    assert "vendor-" in out
    # No leftover digits from the original 12-digit account id.
    assert "123456789012" not in out


def test_infra_id_ec2_keeps_i_prefix(log: DecisionsLog) -> None:
    """EC2 instance ids keep the ``i-`` prefix and the first 8 hex
    characters of the source so two siblings stay visibly related."""
    src = "i-0a1b2c3d4e5f6789a"
    out = infra_id_placeholder(src, log=log, rule_name="ec2")
    assert out.startswith("i-0a1b2c3d")
    assert len(out) == len(src)
    assert out != src


def test_infra_id_uuid_keeps_first_block(log: DecisionsLog) -> None:
    """UUID-shaped values (Azure tenant / subscription / AD
    ObjectGUID) keep their first 8 hex chars; the rest is zeroed
    + a trailing index, dashes preserved."""
    src = "12345678-1234-5678-1234-567812345678"
    out = infra_id_placeholder(src, log=log, rule_name="uuid")
    assert out.startswith("12345678-")
    assert out.count("-") == 4
    assert len(out) == len(src)
    assert "1234-5678" not in out  # original middle blocks rewritten


def test_infra_id_ad_sid_keeps_authority(log: DecisionsLog) -> None:
    """Active-Directory SIDs keep the ``S-1-5-21-`` (or 32) authority
    prefix and rewrite the per-domain sub-authorities."""
    src = "S-1-5-21-1234567890-987654321-111222333-1001"
    out = infra_id_placeholder(src, log=log, rule_name="sid")
    assert out.startswith("S-1-5-21-")
    assert len(out) == len(src)
    assert "1234567890" not in out


def test_infra_id_falls_through_to_generic(log: DecisionsLog) -> None:
    """Unknown shapes (e.g. a GCP project id) take the
    ``generic_keep_prefix`` fallback: same length, first 4 source
    chars kept."""
    src = "acme-prod-1234567"
    out = infra_id_placeholder(src, log=log, rule_name="gcp")
    assert len(out) == len(src)
    assert out.startswith("acme")


def test_infra_id_is_deterministic(log: DecisionsLog) -> None:
    a = infra_id_placeholder(
        "arn:aws:iam::123456789012:role/AdminRole", log=log, rule_name="arn"
    )
    b = infra_id_placeholder(
        "arn:aws:iam::123456789012:role/AdminRole", log=log, rule_name="arn"
    )
    assert a == b
