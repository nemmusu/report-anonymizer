"""Build the synthetic fixture PDFs used by the corpus benchmark.

Every value listed in ``GOLDEN_LEAKS_*`` is a known leak, the
harness uses ≥4/5 detection rate on these documents as part of the
stop criterion. Every value listed in ``GOLDEN_NEGATIVES`` is NOT a
leak, flagging it on any run is a regression.
"""
from __future__ import annotations

from pathlib import Path

import fitz


# ---------------------------------------------------------------------------
# Fixture #1, English credentials in many shapes
# ---------------------------------------------------------------------------

GOLDEN_LEAKS_CREDS: dict[str, list[str]] = {
    "credentials": [
        # Variable NAMES (DB_USER, DB_PASS_2024) are not credentials
        # per the spec, only their values are. Cookies are
        # credentials, not keys (keys is for hex/base64 cryptographic
        # material; session cookies are user-context credentials).
        "j.doe",
        "Welcome01!",
        "svc-backup",
        "Hunter2!",
        "mario.rossi",
        "P@ssw0rd-2026",
        "admin",
        "root",
        "JSESSIONID=8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4",
        "PHPSESSID=ab12cd34ef56gh78ij90kl12mn34op56",
        "ts=YWNtZWJhbms6c2VjcmV0MjAyNg==",
    ],
    "emails": [
        "j.doe@acmebank.com",
        "soc@contoso.io",
    ],
    "brand": ["AcmeBank"],
}


_BODY_CREDS = """\
ACME Bank, Credential exposure findings (English)

We found numerous AcmeBank credentials in scope.

Login form (HTML extract):
  <form action="/api/v1/login">
    Username: j.doe          Password: Welcome01!
    Username: mario.rossi    Password: P@ssw0rd-2026
  </form>

Service-account credentials harvested from a configuration backup:

  DB_USER=svc-backup
  DB_PASS_2024=Hunter2!

URL-encoded body of POST /login:
  user=admin&password=Welcome01!

HTTP Basic auth header:
  Authorization: Basic c3ZjLWJhY2t1cDpIdW50ZXIyIQ==
  (base64-decoded: svc-backup:Hunter2!)

SQL connection string in a Python script:
  mysql://j.doe:Welcome01!@db.acmebank.com/payments

SSH access:
  ssh j.doe@10.10.0.5  # (lab)
  ssh root@bastion.acmebank.com

Session cookies captured during MITM:
  JSESSIONID=8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4
  PHPSESSID=ab12cd34ef56gh78ij90kl12mn34op56
  ts=YWNtZWJhbms6c2VjcmV0MjAyNg==

Contact: j.doe@acmebank.com (lead engineer), soc@contoso.io (SOC).
"""


# ---------------------------------------------------------------------------
# Fixture #2, multilingual + foreign phone formats
# ---------------------------------------------------------------------------

GOLDEN_LEAKS_MULTI: dict[str, list[str]] = {
    "phones": [
        "+1-415-555-2671",
        "+1 (415) 555-2671",
        "+44 20 7946 0958",
        "+33 1 70 96 32 18",
        "+33-1-70-96-32-18",
        "+49 30 1234 5678",
        "+81 3-3251-9000",
        "+55 11 2222-3344",
        "+34 91 360 89 00",
        "+86 21 6886 8888",
        "+7 495 123-45-67",
    ],
    "brand": [
        "AcmeBank",
        "acmebank",
        "ACMEBANK",
        "AcmeBank Pro",
        "Acme-Bank",
    ],
    "emails": ["client.fr@acmebank.fr"],
    # Use a real-looking public IP (NOT RFC 5737 documentation
    # range): 91.222.146.42 belongs to a real ISP block so the
    # detector treats it as a customer leak, verifying that the
    # detector flags real IPs while still respecting RFC 5737.
    "network": ["api.acmebank.com", "91.222.146.42", "vpn.acmebank-prod.io"],
}


_BODY_MULTI = """\
ACME Bank, International scope (multilingual)

ENGLISH
The customer (AcmeBank, also written as acmebank, ACMEBANK, or
AcmeBank Pro on some screens) operates in multiple countries. Their
production gateway is api.acmebank.com (91.222.146.42) and the
remote-access entry point is vpn.acmebank-prod.io. The marketing
site uses Acme-Bank as a hyphenated form.

ITALIAN, Numeri di telefono trovati nelle dump del client mobile:
  Italia:        +393440000001 (placeholder già anonimizzato)
  Stati Uniti:   +1-415-555-2671
  Stati Uniti:   +1 (415) 555-2671   (con parentesi)
  UK:            +44 20 7946 0958
  Francia:       +33 1 70 96 32 18
  Francia:       +33-1-70-96-32-18   (con trattini)

FRANÇAIS, Contacts du client AcmeBank en région EMEA:
  Allemagne:  +49 30 1234 5678
  Espagne:    +34 91 360 89 00
  Russie:     +7 495 123-45-67

日本語 + 中文, APAC support hotline list:
  Japan:  +81 3-3251-9000
  China:  +86 21 6886 8888

PORTUGUÊS, Latam:
  Brasil:  +55 11 2222-3344

Adresse e-mail du contact France: client.fr@acmebank.fr.

Note: ``+393330000001`` and ``+12025550100`` are RFC reserved test
ranges and MUST NOT be flagged as leaks; ``8.8.8.8`` and
``192.168.1.1`` are public DNS / RFC1918 ranges.
"""


# ---------------------------------------------------------------------------
# Fixture #3, brand / package / header / advisory variants
# ---------------------------------------------------------------------------

GOLDEN_LEAKS_BRAND: dict[str, list[str]] = {
    "brand": [
        "AcmeBank",
        "acmebank",
        "ACMEBANK",
        "AcmeBankPro",
        "AcmeBank Pro",
        "AcmeBank-Server",
    ],
    "android": [
        "com.acmebank.app",
        "com.acmebank.app.beta",
        "com.acmebank.tablet",
        "it.acmebank.mobile",
    ],
    "headers": [
        "X-AcmeBank-Auth",
        "X-AcmeBankPro-Token",
        "X-Acme-Trace-Id",
    ],
    "user_agents": [
        "AcmeBankApp/3.4-android",
        "AcmeBankClient/2.1 (iOS)",
    ],
    "ids": [
        "ACME-VULN-2024-0042",
        "ACMEBANK-CHAIN-A",
        "AcmeBank-INC-9001",
    ],
    "network": [
        "api.acmebank.com",
        "vpn.acmebank-prod.io",
        "static.acme-bank.com",
        "*.acmebank.local",
    ],
}


_BODY_BRAND = """\
ACME Bank, Brand & infrastructure inventory

The customer markets itself as AcmeBank but the brand also appears
as acmebank in URIs and configs, ACMEBANK in some legacy banner
strings, AcmeBankPro in the Pro tier, AcmeBank Pro on web pages,
and AcmeBank-Server in server-name HTTP headers.

Mobile Android packages observed during device acquisition:
  com.acmebank.app          (production)
  com.acmebank.app.beta     (beta channel)
  com.acmebank.tablet       (tablet variant)
  it.acmebank.mobile        (Italy-specific build)

Proprietary HTTP headers in the public-facing API:
  X-AcmeBank-Auth: <token>
  X-AcmeBankPro-Token: <jwt>
  X-Acme-Trace-Id: <uuid>

User-Agent strings of the official client builds:
  AcmeBankApp/3.4-android
  AcmeBankClient/2.1 (iOS)

Internal advisory / ticket identifiers cited in this report:
  ACME-VULN-2024-0042
  ACMEBANK-CHAIN-A
  AcmeBank-INC-9001

Production hostnames in scope:
  api.acmebank.com
  vpn.acmebank-prod.io
  static.acme-bank.com
  *.acmebank.local
"""


# ---------------------------------------------------------------------------
# Fixture #4, explicit negatives (these MUST NOT be flagged)
# ---------------------------------------------------------------------------

GOLDEN_NEGATIVES: list[str] = [
    # Standards / libraries
    "NaCl", "libsodium", "OpenSSL", "OAuth", "OAuth2", "JWT", "SAML",
    "FCM", "OneSignal", "Firebase", "Curve25519", "Ed25519", "AES",
    "SHA-256", "PBKDF2", "Argon2",
    # Standard constants / RFC ranges
    "127.0.0.1", "0.0.0.0", "8.8.8.8", "1.1.1.1",
    "203.0.113.10", "198.51.100.5", "192.0.2.99",
    "192.168.1.1", "10.0.0.1", "172.16.0.1",
    # OS / SDK / library versions
    "Android 10", "iOS 17", "Java 17", "Python 3.12", "OpenSSL 3.0",
    # Hardware models
    "Samsung SM-A920F", "Pixel 7", "iPhone 14",
    # Dates
    "7 May 2026", "2026-05-07", "May 7, 2026", "7 maggio 2026",
    # Generic file names / paths
    "ADVISORY.md", "README.md", "exploit.py",
    # Generic descriptive endpoints
    "/api/v1/login", "/healthz", "/metrics",
    # Variable / function / class names
    "License.WebServiceURI", "sodium_key", "key_id", "encoded_key",
    # Reserved phones
    "+1-202-555-0100", "+393330000001",
    # Generic security terms
    "MITM", "CSRF", "XSS", "RCE", "CVE-2024-12345", "CVSS",
]


_BODY_NEG = """\
Standards & references (this section MUST stay intact)

The implementation uses libsodium (NaCl), OpenSSL 3.0 and
Curve25519 for key exchange. JWT and SAML are mentioned for
context. FCM and OneSignal handle push notifications; Firebase is
the build console.

The crypto recipes referenced are AES, SHA-256, PBKDF2 and Argon2.

Network analysis used standard test ranges (RFC 5737:
203.0.113.10, 198.51.100.5, 192.0.2.99) and RFC 1918 internal
addresses (192.168.1.1, 10.0.0.1, 172.16.0.1). Public DNS:
8.8.8.8, 1.1.1.1. Loopback: 127.0.0.1.

Test phones (RFC reserved): +1-202-555-0100, +393330000001.

Lab devices: Samsung SM-A920F, Pixel 7, iPhone 14.
Software: Android 10, iOS 17, Java 17, Python 3.12.

The repo contains ADVISORY.md, README.md, and an exploit.py
script. Generic endpoints in scope: /api/v1/login, /healthz,
/metrics. Identifiers in code: License.WebServiceURI,
sodium_key, key_id, encoded_key.

Dates throughout the report: 7 May 2026, 2026-05-07, May 7, 2026,
7 maggio 2026 (Italian).

Security terminology cited (NOT customer-specific): MITM, CSRF,
XSS, RCE, CVE-2024-12345, CVSS.
"""


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _render(body: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    # Long body splits across pages naturally.
    remaining = body
    while remaining:
        page = doc.new_page(width=612, height=900)
        rect = fitz.Rect(40, 40, 572, 860)
        ret = page.insert_textbox(
            rect,
            remaining,
            fontname="helv",
            fontsize=10,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        if ret >= 0 or not remaining:
            break
        # negative return = how many chars unwritten (negated)
        consumed = max(1, len(remaining) + ret)
        remaining = remaining[consumed:]
    doc.save(str(out_path))
    doc.close()
    return out_path


def build_all(out_root: Path) -> dict[str, Path]:
    out_root.mkdir(parents=True, exist_ok=True)
    return {
        "synthetic_credentials": _render(_BODY_CREDS, out_root / "synthetic_credentials.pdf"),
        "synthetic_multilang": _render(_BODY_MULTI, out_root / "synthetic_multilang.pdf"),
        "synthetic_brand": _render(_BODY_BRAND, out_root / "synthetic_brand.pdf"),
        "synthetic_negatives": _render(_BODY_NEG, out_root / "synthetic_negatives.pdf"),
    }


# Combined ground truth for the harness (per fixture).
GROUND_TRUTH: dict[str, dict[str, list[str]]] = {
    "synthetic_credentials.pdf": GOLDEN_LEAKS_CREDS,
    "synthetic_multilang.pdf": GOLDEN_LEAKS_MULTI,
    "synthetic_brand.pdf": GOLDEN_LEAKS_BRAND,
    "synthetic_negatives.pdf": {},  # everything in this file is a negative
}

NEGATIVES = GOLDEN_NEGATIVES


if __name__ == "__main__":
    ps = build_all(Path("/tmp/anonbench/fixtures"))
    for name, p in ps.items():
        print(f"wrote {name} -> {p}")
