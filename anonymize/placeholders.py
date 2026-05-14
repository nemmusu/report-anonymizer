"""Shape-preserving placeholder generators.

Each generator is a pure function ``(value, log, rule_name) -> str``. The
counter state is kept in :class:`anonymize.decisions_log.DecisionsLog`
(persistent JSONL), so the *same* canonical input always resolves to the
*same* placeholder across runs and across categories that share a bucket.

Design goals (anchored on the user's pentest-report use case):

* **Length preservation**. The PDF in-place adapter measures the source
  rectangle width and refuses to shrink the font more than 50 %. A
  length-preserving placeholder fits without any reflow.
* **Shape preservation / context preservation**. Two anonymized values
  that started with the same prefix in the input keep that shared prefix
  in the output, so the report reader can still tell that they belong
  to the same family of leaks (host clusters, related credentials,
  numbers from the same operator, …).
* **Determinism**. Same canonical value -> same trailing index (per
  bucket), so re-running the pipeline on the same source produces
  byte-identical placeholders.
* **Locale neutrality**. No assumption that phones are Italian, that
  brands speak English, or that hex tokens are 32 chars; every generator
  works on any-locale input.
"""
from __future__ import annotations

import re
from typing import Optional

from .decisions_log import DecisionsLog


# Phone country codes that are 1, 2 or 3 digits long.  We do not need
# the full ITU table; only enough to cover the common cases. Any value
# not on this list falls back to the heuristic "longest prefix that
# leaves at least 8 local digits".
_CC_TWO = {
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43",
    "44", "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56",
    "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84",
    "86", "90", "91", "92", "93", "94", "95", "98",
}
_CC_THREE = {
    "212", "213", "216", "218", "220", "221", "222", "223", "224", "225",
    "226", "227", "228", "229", "230", "231", "232", "233", "234", "235",
    "236", "237", "238", "239", "240", "241", "242", "243", "244", "245",
    "246", "247", "248", "249", "250", "251", "252", "253", "254", "255",
    "256", "257", "258", "260", "261", "262", "263", "264", "265", "266",
    "267", "268", "269", "290", "291", "297", "298", "299", "350", "351",
    "352", "353", "354", "355", "356", "357", "358", "359", "370", "371",
    "372", "373", "374", "375", "376", "377", "378", "380", "381", "382",
    "383", "385", "386", "387", "389", "420", "421", "423", "500", "501",
    "502", "503", "504", "505", "506", "507", "508", "509", "590", "591",
    "592", "593", "594", "595", "596", "597", "598", "670", "672", "673",
    "674", "675", "676", "677", "678", "679", "680", "681", "682", "683",
    "685", "686", "687", "688", "689", "690", "691", "692", "850", "852",
    "853", "855", "856", "880", "886", "960", "961", "962", "963", "964",
    "965", "966", "967", "968", "970", "971", "972", "973", "974", "975",
    "976", "977", "992", "993", "994", "995", "996", "998",
}


_PHONE_RULE_BUCKET = "_phone_canonical"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _canonical_phone(value: str) -> str:
    """Return canonical E.164 form usable as a stable lookup key.

    Examples (all map to ``+393440405580``)::

        +393440405580    -> +393440405580
        393440405580     -> +393440405580
        3440405580       -> +393440405580
        +39 344 0405580  -> +393440405580
        +3881681931      -> +393881681931  (stray ``+`` on a bare IT mobile)

    For non-Italian numbers we keep the user-supplied digits as-is and
    just normalise the leading ``+``.
    """
    digits = _digits(value)
    if not digits:
        return value
    # IT international: digits already start with the IT country code.
    if digits.startswith("39") and len(digits) >= 11:
        return "+" + digits
    # Bare 10-digit IT mobile (with or without a stray leading ``+``):
    # add the ``+39`` prefix for canonical form.
    if len(digits) == 10 and digits[0] == "3":
        return "+39" + digits
    return "+" + digits


def _split_country_carrier(digits: str) -> tuple[str, str, str]:
    """Return ``(country_code, carrier, local_rest)`` split for ``digits``.

    ``digits`` is the run of digits with no '+' or separators.  The
    country code is matched against :data:`_CC_THREE` first (longest
    match), then :data:`_CC_TWO`, otherwise we take a single digit.
    Carrier is the next 3 digits when at least 7 local digits remain;
    otherwise as many as we have minus the trailing 4-digit subscriber
    block.
    """
    cc = ""
    if len(digits) >= 3 and digits[:3] in _CC_THREE:
        cc, rest = digits[:3], digits[3:]
    elif len(digits) >= 2 and digits[:2] in _CC_TWO:
        cc, rest = digits[:2], digits[2:]
    elif digits:
        cc, rest = digits[:1], digits[1:]
    else:
        cc, rest = "", digits
    if len(rest) >= 7:
        carrier_len = 3
    elif len(rest) >= 5:
        carrier_len = max(0, len(rest) - 4)
    else:
        carrier_len = 0
    carrier = rest[:carrier_len]
    local = rest[carrier_len:]
    return cc, carrier, local


def _stable_index(
    log: DecisionsLog, bucket: str, canonical: str
) -> int:
    cur = log.t0_assignments.get(bucket, {}).get(canonical)
    if cur is not None:
        try:
            return int(cur, 10)
        except ValueError:
            try:
                return int(cur, 16)
            except ValueError:
                pass
    n = log.next_index_for(bucket)
    log.record_t0_assignment(bucket, canonical, str(n))
    return n


def phone_intl(value: str, *, log: DecisionsLog, rule_name: str = "") -> str:
    """Length-, country- and carrier-preserving phone placeholder.

    ``+1 (415) 555-1234``  -> ``+1 (415) 555-0001``
    ``+44 7700 900123``    -> ``+44 7700 000001``
    ``+393331234567``      -> ``+393330000001``
    ``3440405580``         -> ``3440000001``  (bare Italian mobile)

    Separators (``space``, ``-``, ``.``, ``(``, ``)``) are kept exactly
    where they appeared in the source, so the placeholder reads like a
    phone number from the same locale.
    """
    digits = _digits(value)
    if not digits:
        return value
    canonical = _canonical_phone(value)
    n = _stable_index(log, _PHONE_RULE_BUCKET, canonical)

    has_plus = value.lstrip().startswith("+")
    if has_plus:
        cc, carrier, local = _split_country_carrier(digits)
    else:
        # Bare numbers: try the canonical (with country code injected),
        # then strip back the leading CC since the source did not have it.
        canon_digits = _digits(canonical)
        cc, carrier, local = _split_country_carrier(canon_digits)
        if cc and digits.startswith(cc):
            pass  # leave cc visible only if it was actually in source
        else:
            cc = ""

    keep = (cc + carrier) if has_plus else (cc + carrier).lstrip(cc)
    rest_len = len(digits) - len(cc) - len(carrier) if has_plus else len(digits) - len(carrier)
    rest_len = max(0, rest_len)
    seq = f"{n:0{max(rest_len, 1)}d}"
    seq = seq[-rest_len:] if rest_len > 0 else ""
    new_digits = (cc if has_plus else "") + carrier + seq
    # Pad / trim to the *exact* source digit count.
    if len(new_digits) < len(digits):
        new_digits = new_digits + "0" * (len(digits) - len(new_digits))
    elif len(new_digits) > len(digits):
        new_digits = new_digits[: len(digits)]

    # Walk the original string, copying separators verbatim, replacing
    # digit-by-digit with ``new_digits``.
    out: list[str] = []
    di = 0
    for ch in value:
        if ch.isdigit():
            out.append(new_digits[di] if di < len(new_digits) else "0")
            di += 1
        else:
            out.append(ch)
    result = "".join(out)
    # Identity guard: for very short inputs (e.g. ``+236``) the
    # zero-padded sequence collapses back to the source digits and the
    # placeholder ends up identical to the value. ``merge_candidates``
    # would silently drop it. Mutate the last digit deterministically
    # so the placeholder is at least one character different.
    if result == value and any(c.isdigit() for c in value):
        last_digit_idx = -1
        for i in range(len(result) - 1, -1, -1):
            if result[i].isdigit():
                last_digit_idx = i
                break
        if last_digit_idx >= 0:
            d = int(result[last_digit_idx])
            d2 = (d + 1) % 10
            if d2 == d:
                d2 = (d + 2) % 10
            result = result[:last_digit_idx] + str(d2) + result[last_digit_idx + 1 :]
    return result


def hex_keep_prefix(
    value: str, *, log: DecisionsLog, rule_name: str = "", prefix_len: int = 8
) -> str:
    """Length-preserving placeholder that keeps the first ``prefix_len``
    characters of the source.

    The reader can still tell that two anonymized credentials shared the
    same prefix in the source, which is the whole point of the change:

    ``nfdddf80a3b1c4...d72f``  -> ``nfdddf80000000000000000000000001``
    ``nfdddf80aaaa1111...c0c0``  -> ``nfdddf80000000000000000000000002``
    """
    if not value:
        return value
    canonical = value.lower()
    n = _stable_index(log, rule_name or "_hex_keep_prefix", canonical)
    keep = value[: min(prefix_len, len(value))]
    seq_hex = f"{n:x}"
    body_len = max(0, len(value) - len(keep))
    if len(seq_hex) >= body_len:
        body = seq_hex[-body_len:] if body_len else ""
    else:
        body = "0" * (body_len - len(seq_hex)) + seq_hex
    return keep + body


def hex_zero_seq(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Legacy strategy kept for backward compatibility.

    Same length as the source, all zeros except for a short trailing
    sequential hex index. Use ``hex_keep_prefix`` for new patterns.
    """
    if not value:
        return value
    canonical = value.lower()
    n = _stable_index(log, rule_name or "_hex_zero_seq", canonical)
    seq = f"{n:x}"
    if len(seq) >= len(value):
        return seq[-len(value):]
    return ("0" * (len(value) - len(seq))) + seq


def email_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Length-matched email placeholder ``userNNN@vendor.example``.

    Total length is forced to match the source (zero-padded user index
    or truncated domain) so PDF in-place can render it without reflow.
    """
    if "@" not in value:
        return value
    n = _stable_index(log, rule_name or "_email", value.lower())
    domain = "vendor.example"
    target = len(value)
    base_user = "user"
    user_pad = max(1, target - len(domain) - 1 - len(base_user))
    user = base_user + f"{n:0{user_pad}d}"
    placeholder = f"{user}@{domain}"
    if len(placeholder) > target:
        placeholder = placeholder[:target]
    elif len(placeholder) < target:
        placeholder = placeholder + "0" * (target - len(placeholder))
    return placeholder


def hostname_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """``bastion-prod-01.acme.example`` -> ``bastion-prod-001.example.test``.

    Keeps the *first* label prefix (everything up to the first digit or
    `.`), then a sequential index, then a fixed neutral domain.  Length
    is matched to the source.
    """
    if not value:
        return value
    n = _stable_index(log, rule_name or "_host", value.lower())
    target = len(value)
    head = re.match(r"[A-Za-z][A-Za-z\-_]*", value)
    prefix = head.group(0) if head else "host"
    domain = ".example.test"
    seq_pad = max(1, target - len(prefix) - len(domain))
    seq = f"{n:0{seq_pad}d}"
    placeholder = f"{prefix}{seq}{domain}"
    if len(placeholder) > target:
        placeholder = placeholder[:target]
    elif len(placeholder) < target:
        placeholder = placeholder + "0" * (target - len(placeholder))
    return placeholder


def ipv4_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """RFC 5737 documentation block ``203.0.113.<NN>``."""
    n = _stable_index(log, rule_name or "_ipv4", value)
    return f"203.0.113.{n}"


def brand_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Length-matched generic brand placeholder ``VendorNNN``."""
    if not value:
        return value
    n = _stable_index(log, rule_name or "_brand", value.lower())
    target = len(value)
    head = "Vendor"
    seq_pad = max(1, target - len(head))
    seq = f"{n:0{seq_pad}d}"
    placeholder = head + seq
    if len(placeholder) > target:
        placeholder = placeholder[:target]
    elif len(placeholder) < target:
        placeholder = placeholder + "0" * (target - len(placeholder))
    return placeholder


def app_pkg_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Reverse-domain app package / bundle id placeholder.

    Used for both Android packages (``com.acme.mobileapp``) and iOS
    bundle ids (``it.acmebank.mobile``).  Preserves length and the
    ``com.vendor.app`` shape; the trailing index keeps two distinct
    sources mapped to two distinct placeholders.
    """
    if not value:
        return value
    n = _stable_index(log, rule_name or "_app_pkg", value.lower())
    target = len(value)
    head = "com.vendor.app"
    if len(head) >= target:
        return head[:target]
    seq_pad = target - len(head)
    seq = f"{n:0{seq_pad}d}"
    return head + seq


# Backwards-compat alias: the function was renamed from
# ``android_pkg_placeholder`` to ``app_pkg_placeholder`` when the
# category was rebranded.  Keeping this alias prevents external code
# (decisions logs, third-party scripts) from breaking immediately;
# new call sites should use the new name.
android_pkg_placeholder = app_pkg_placeholder


def header_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Preserve the ``X-`` prefix of HTTP headers; replace body with
    ``Vendor<index>``.
    """
    if not value:
        return value
    n = _stable_index(log, rule_name or "_header", value.lower())
    target = len(value)
    if value[:2].lower() == "x-":
        head = "X-Vendor"
    else:
        head = "Vendor"
    seq_pad = max(1, target - len(head))
    seq = f"{n:0{seq_pad}d}"
    placeholder = head + seq
    if len(placeholder) > target:
        placeholder = placeholder[:target]
    elif len(placeholder) < target:
        placeholder = placeholder + "0" * (target - len(placeholder))
    return placeholder


def credentials_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Length- and char-class-preserving placeholder for usernames and
    plaintext passwords.

    Each digit / uppercase / lowercase / special-character position in
    the source is replaced with a neutral character of the same class,
    seeded by a stable index so two different credentials never collide.
    Special characters (``.``, ``-``, ``_``, ``@``, ``!``, …) and
    whitespace are preserved verbatim so ``user@acme.com`` stays a
    plausible-looking email-shaped username and ``Welcome01!`` keeps
    its punctuation intact.
    """
    if not value:
        return value
    n = _stable_index(log, rule_name or "_credentials", value)
    digit_seq = f"{n:010d}"  # 10 digits is plenty for any sane credential
    letters = "abcdefghijklmnopqrstuvwxyz"
    di = 0
    li = n % 26
    out: list[str] = []
    for c in value:
        if c.isdigit():
            out.append(digit_seq[di % len(digit_seq)])
            di += 1
        elif c.isupper():
            out.append(letters[li % 26].upper())
            li += 1
        elif c.islower():
            out.append(letters[li % 26])
            li += 1
        else:
            out.append(c)
    return "".join(out)


def generic_keep_prefix(
    value: str, *, log: DecisionsLog, rule_name: str = "", prefix_len: int = 4
) -> str:
    """Generic length-preserving placeholder; first ``prefix_len`` chars
    of the source are kept, the rest is zero-padded sequential index.
    """
    if not value:
        return value
    n = _stable_index(log, rule_name or "_generic", value.lower())
    keep = value[: min(prefix_len, len(value))]
    body_len = max(0, len(value) - len(keep))
    seq = f"{n:0{max(body_len, 1)}d}"
    if len(seq) > body_len:
        seq = seq[-body_len:] if body_len else ""
    else:
        seq = "0" * (body_len - len(seq)) + seq
    return keep + seq


_AWS_ARN_RE = __import__("re").compile(
    r"^(arn:[a-z][a-z0-9-]*:[a-z0-9-]*:[a-z0-9-]*:)([0-9]*)(:.*)$",
    __import__("re").IGNORECASE,
)
_EC2_INSTANCE_RE = __import__("re").compile(
    r"^(i-)([0-9a-f]{8,17})$", __import__("re").IGNORECASE
)
_UUID_RE = __import__("re").compile(
    r"^([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    __import__("re").IGNORECASE,
)
_AD_SID_RE = __import__("re").compile(
    r"^(S-1-5-(?:21|32))-([0-9-]+)$", __import__("re").IGNORECASE
)


def infra_id_placeholder(
    value: str, *, log: DecisionsLog, rule_name: str = ""
) -> str:
    """Cloud / infrastructure / Active-Directory resource identifier.

    Recognises a few common shapes and rewrites the customer-tied
    parts while keeping the format stable:

    - **AWS ARN** (``arn:aws:iam::123456789012:role/MyRole``) keeps
      the partition / service / region prefix and the trailing
      resource path; the 12-digit account id is replaced with a
      length-preserving sequential value, and the resource name's
      brand-tied parts are flattened to ``vendor-<index>``.
    - **EC2 instance id** (``i-0a1b2c3d4e5f6789a``) keeps the ``i-``
      prefix + the first 8 hex characters of the source, fills the
      rest with hex zeros and a trailing index so two distinct
      sources stay distinguishable.
    - **UUID** (Azure tenant / subscription / AD ObjectGUID) keeps
      the first 8 hex of the source then zeros, preserving dashes.
    - **Active-Directory SID** (``S-1-5-21-…``) keeps the well-known
      authority prefix, replaces the per-domain sub-authorities
      with a deterministic numeric block.

    Anything that doesn't match these shapes falls through to
    :func:`generic_keep_prefix`, which keeps the first 4 source
    characters and replaces the rest with a sequential index.
    """
    if not value:
        return value

    n = _stable_index(log, rule_name or "_infra_id", value.lower())
    target = len(value)

    m = _AWS_ARN_RE.match(value)
    if m:
        head, account, tail = m.group(1), m.group(2), m.group(3)
        # 12-digit account id: keep the shape (or whatever length was
        # there).  The tail typically looks like ``:role/MyRole`` -
        # we replace the resource name with ``vendor-N``.
        acct_pad = max(1, len(account))
        new_account = f"{n:0{acct_pad}d}"[-acct_pad:]
        if "/" in tail:
            head_t, _, _ = tail.partition("/")
            new_tail = f"{head_t}/vendor-{n}"
        else:
            new_tail = f":vendor-{n}"
        cand = f"{head}{new_account}{new_tail}"
        if len(cand) > target:
            cand = cand[:target]
        elif len(cand) < target:
            cand = cand + "0" * (target - len(cand))
        return cand

    m = _EC2_INSTANCE_RE.match(value)
    if m:
        prefix, hex_tail = m.group(1), m.group(2).lower()
        keep = hex_tail[:8]
        body_len = len(hex_tail) - len(keep)
        seq = f"{n:0{max(body_len, 1)}x}"
        if len(seq) > body_len:
            seq = seq[-body_len:] if body_len else ""
        else:
            seq = "0" * (body_len - len(seq)) + seq
        return f"{prefix}{keep}{seq}"

    m = _UUID_RE.match(value)
    if m:
        head = m.group(1).lower()
        # 4-4-4-12 zero blocks with a trailing 8-char index so the
        # final UUID still parses but two distinct sources stay
        # distinguishable.
        idx = f"{n:08x}"
        return f"{head}-0000-0000-0000-{'0' * 4}{idx}"

    m = _AD_SID_RE.match(value)
    if m:
        head = m.group(1)
        # The sub-authority block can vary in length; we replace it
        # with a deterministic pattern padded to the source length.
        pad = max(1, target - len(head) - 1)  # account for the leading "-"
        seq = f"{n:0{pad}d}"
        if len(seq) > pad:
            seq = seq[-pad:]
        return f"{head}-{seq}"

    return generic_keep_prefix(value, log=log, rule_name=rule_name or "_infra_id")


_STRATEGY_FNS = {
    "phone_intl": phone_intl,
    "phone_it": phone_intl,  # legacy alias
    "hex_keep_prefix": hex_keep_prefix,
    "hex_zero_seq": hex_zero_seq,
    "email": email_placeholder,
    "hostname": hostname_placeholder,
    "ipv4": ipv4_placeholder,
    "brand": brand_placeholder,
    "app_pkg": app_pkg_placeholder,
    "android_pkg": app_pkg_placeholder,  # legacy alias
    "header": header_placeholder,
    "credentials": credentials_placeholder,
    "infra_id": infra_id_placeholder,
    "generic_keep_prefix": generic_keep_prefix,
}


def is_overlong_placeholder(value: str, placeholder: str, *, max_extra: int = 4) -> bool:
    """True when ``placeholder`` is too long to safely fit in the
    rectangle of the source ``value`` after PDF in-place redaction.

    Length-preserving placeholders never trip this check; the threshold
    catches the pathological case where a 5-char source like ``+39LAB``
    receives a 13-char placeholder, OR a 44-char source receives a
    51-char placeholder, both overflow into adjacent columns once the
    PDF in-place adapter tries to render them.

    Up to ``max_extra`` (default 4) additional characters are tolerated
    because the PASS 2 shrink-and-clip path handles them cleanly.
    """
    if not value or not placeholder:
        return False
    return len(placeholder) > len(value) + max_extra


def clamp_to_value_length(value: str, placeholder: str) -> str:
    """Truncate ``placeholder`` to ``len(value)`` so PDF in-place can
    render it without overflowing into adjacent text. The truncated
    suffix is replaced with ``0`` characters where possible to keep
    the placeholder visually recognisable as a "redacted, anonymised"
    token.
    """
    n = len(value)
    if n <= 0 or len(placeholder) <= n:
        return placeholder
    head_len = max(1, n - 3)
    tail_len = n - head_len
    return placeholder[:head_len] + ("0" * tail_len)


_CATEGORY_DEFAULT_STRATEGY: dict[str, str] = {
    "phones": "phone_intl",
    "keys": "hex_keep_prefix",
    "emails": "email",
    "brand": "brand",
    "app_packages": "app_pkg",
    "headers": "header",
    "credentials": "credentials",
    "ids": "generic_keep_prefix",
    "infra_ids": "infra_id",
    "user_agents": "generic_keep_prefix",
    "other": "generic_keep_prefix",
}


def auto_derive_placeholder(
    value: str, category: str, *, log: DecisionsLog, rule_name: str = ""
) -> Optional[str]:
    """Pick a sensible placeholder for ``value`` based on ``category``.

    Used when the LLM proposed an empty / identity placeholder (e.g.
    ``value="CONFIRMED_VULN-13"``, ``suggested_placeholder="CONFIRMED_VULN-13"``)
   , the operator approved it in Review but ``merge_candidates``
    would otherwise drop it because identity placeholders are no-ops.

    For ``network`` we try IPv4 first (matches ``X.X.X.X``); if the
    value is not a valid dotted-quad we fall back to ``hostname``.
    """
    cat = (category or "other").strip().lower()
    if cat == "network":
        try:
            parts = value.split(".")
            if len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts):
                return ipv4_placeholder(value, log=log, rule_name=rule_name or "_auto_ip")
        except Exception:
            pass
        return hostname_placeholder(
            value, log=log, rule_name=rule_name or "_auto_host"
        )
    strategy = _CATEGORY_DEFAULT_STRATEGY.get(cat, "generic_keep_prefix")
    return resolve_strategy(strategy, value, log=log, rule_name=rule_name or f"_auto_{cat}")


def resolve_strategy(
    name: str, value: str, *, log: DecisionsLog, rule_name: str = ""
) -> Optional[str]:
    """Run the named strategy. Returns ``None`` if the name is unknown
    so the caller can fall back to the legacy template-based path.
    """
    fn = _STRATEGY_FNS.get((name or "").strip().lower())
    if fn is None:
        return None
    return fn(value, log=log, rule_name=rule_name)


__all__ = [
    "phone_intl",
    "hex_keep_prefix",
    "hex_zero_seq",
    "email_placeholder",
    "hostname_placeholder",
    "ipv4_placeholder",
    "brand_placeholder",
    "app_pkg_placeholder",
    "android_pkg_placeholder",
    "header_placeholder",
    "credentials_placeholder",
    "infra_id_placeholder",
    "generic_keep_prefix",
    "resolve_strategy",
    "is_overlong_placeholder",
    "clamp_to_value_length",
    "auto_derive_placeholder",
    "_canonical_phone",
]
