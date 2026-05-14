# Anonymization scope

This page is the deep dive behind the README's *What it anonymizes*
table. It describes, category by category, what the detector flags
on a penetration-test report, what placeholders it produces, and the
classes of strings the pipeline deliberately leaves alone so the
report's technical content keeps working.

The source of truth for these rules is [`prompts/system_detector.txt`](https://github.com/nemmusu/report-anonymizer/blob/master/prompts/system_detector.txt)
in the default **Fast** detection mode; the deterministic regex layer that runs
before the LLM lives in
[`config/leak_patterns.yml`](https://github.com/nemmusu/report-anonymizer/blob/master/config/leak_patterns.yml).
When the **High accuracy** (multi-pass) mode is selected from the
Pipeline tab toggle, the same rules live category-by-category under
[`prompts/detector_multipass/`](https://github.com/nemmusu/report-anonymizer/tree/master/prompts/detector_multipass)
(one focused prompt per category, ~800 tokens each).
The placeholder substitutions and category-specific format rules
are in [`prompts/system_critic.txt`](https://github.com/nemmusu/report-anonymizer/blob/master/prompts/system_critic.txt)
and [`anonymize/placeholders.py`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/placeholders.py).

## Design principles

1. **The technical content stays.** Attack chains, exploit code,
   shell commands, payloads, tool output, library names, RFC
   ranges, generic versions: these are what makes the report
   useful as a teaching artefact and must remain untouched. A
   pentest report should remain readable by another pentester
   after anonymization.
2. **Only customer-identifying values move.** The detector's
   precision checklist is "if I removed this value, would the
   attack logic still be understandable?", if yes, it is a leak;
   if no, it is technical description.
3. **Placeholders are length- and shape-preserving.** PDFs are
   redacted in place, so a 17-character phone number must be
   replaced by a 17-character placeholder; a 32-hex token by a
   32-hex placeholder; a brand name by a brand-shaped neutral
   string. This avoids reflow.
4. **Same value → same placeholder, every time.** A real value
   that appears twice (in different cases, in different formats,
   inside a URL or a payload) gets the same neutral substitute
   on every occurrence. The mapping persists in
   `substitution_map.yml`.
5. **Multilingual.** Reports come in any language; the detector
   analyses the input in its native language and emits
   language-neutral placeholders.

## The 12 categories

### 1. `brand`: customer / product names

The customer's company name, product line, suite, vendor, app name,
and any of their case variants when they appear inside URLs,
package names, header names, advisory IDs, etc.

| Original | Placeholder |
|---|---|
| `AcmeBank Pro v25.1.2135` | `VendorApp v25.1.2135` |
| `acmebank` (lowercase, in a URL) | `vendorapp` |
| `ContosoVoice` | `VendorVoice` |
| `NimbusGSM` | `VendorGSM` |

The detector flags **every** form of the brand. If the same word
appears as a domain (`acmebank.com`), as a package (`com.acmebank`),
as a header (`X-AcmeBank-Auth`) and as a deeplink (`acmebank://`),
each occurrence gets its own candidate so the placeholder rewrites
the full token.

### 2. `network`: IPs and hostnames

Real public IPv4 addresses of the customer, the customer's owned
domains, and proprietary hostnames under those domains.

| Original | Placeholder |
|---|---|
| `203.0.113.42` *(real public IP)* | `203.0.113.NN` |
| `api.acme.com` | `api.vendor.example` |
| `*.prod.acme.io` | `*.prod.vendor.example` |
| `keyserver.acmebank.local` | `keyserver.vendor.local` |

Placeholders use the [RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737)
documentation ranges (`203.0.113.0/24`, `198.51.100.0/24`,
`192.0.2.0/24`) and the [RFC 2606](https://datatracker.ietf.org/doc/html/rfc2606)
`.example` TLD so the placeholder is itself valid demo data and
will not collide with anyone's real assets.

**Not flagged**: RFC 5737 ranges, RFC 1918 (`10.x`, `172.16-31.x`,
`192.168.x`), loopback (`127.0.0.1`), well-known public DNS
(`8.8.8.8`, `1.1.1.1`), generic descriptive endpoints
(`/api/v1/login`, `/healthz`, `keyserver/v1/publish`).

### 3. `phones`: E.164 numbers

Any-country phone numbers in any format. The placeholder keeps the
country code and the carrier prefix, then zeroes out the rest with
a sequential index of equal length.

| Original | Placeholder |
|---|---|
| `+39 344 1234567` | `+39 344 0000001` |
| `+1 (415) 867-5309` | `+1 (415) 555-0001` |
| `+44 7700 900123` | `+44 7700 000001` |

**Not flagged**: RFC reserved test ranges (`+393440000001`,
`+1-555-0100`), already-anonymized numbers.

### 4. `emails`: customer-domain emails

Addresses on the customer's domain or addresses of real people
involved in the engagement.

| Original | Placeholder |
|---|---|
| `j.doe@acmebank.com` | `user01@vendor.example` |
| `pentest@contoso.local` | `user02@vendor.example` |

**Not flagged**: `@example.com`, `@test.local`, generic
documentation addresses.

### 5. `credentials`: plaintext user / password / cookie pairs

Human-typed credentials and live session tokens taken from real
dumps. Each identifier is emitted as a separate candidate so the
username and password get independent placeholders.

| Original | Placeholder |
|---|---|
| `j.doe` *(username)* | `u.demo` |
| `svc-backup` | `svc-demo01` |
| `Welcome01!` *(password)* | `Aaaaaaa00!` |
| `Hunter2!` | `Aaaaaa0!` |
| `Authorization: Basic dXNlcjpwYXNz` | new base64 of equal length |
| `JSESSIONID=8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4` | `JSESSIONID=8b9c0d1e000000000000000000000001` |

The placeholder keeps the length and the character classes
(letter / digit / special) of the original so the redacted dump
still parses.

**Not flagged**: documentation placeholders (`user`/`pass`,
`alice`/`bob` in protocol diagrams, `foo`/`bar` in code samples),
variable *names* (`DB_USER`, `DB_PASS_2024`), only their values
are credentials.

### 6. `keys`: hardcoded tokens, hashes, cryptographic material

Hex tokens, base64-encoded keys, JWTs, SAML assertions, OAuth
bearer tokens that come from the real environment.

| Original | Placeholder |
|---|---|
| `nfdddf80a3b1c4e5f6079a8b9c0d1e2f` *(32-hex)* | `nfdddf80000000000000000000000001` |
| `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.…` *(JWT)* | new JWT-shaped string of equal length |
| public key blob in PEM | length-preserving placeholder |

For hex values the placeholder copies the **first 8 source
characters** so two related credentials in the report stay visibly
related (`nfdddf80…0001`, `nfdddf80…0002`), useful when the
report compares two derivations of the same key material.

**Not flagged**: well-known constants (all-zero IV, RFC 4231 test
vectors, `Curve25519` public examples), library names (NaCl,
libsodium, OpenSSL, `Ed25519`, `AES`, `SHA-256`), code variable
names (`sodium_key`, `OWN_KEY`, `DEV_PUB`).

### 7. `headers`: proprietary HTTP headers

Any HTTP header whose name encodes the customer's brand or
vendor. The detector rewrites the full header name; the value
gets its own placeholder according to its own category (key,
cookie, …).

| Original | Placeholder |
|---|---|
| `X-AcmeBank-Auth` | `X-Vendor-Auth` |
| `X-ContosoServer-Token` | `X-VendorServer-Token` |

**Not flagged**: standard headers (`Authorization`, `Content-Type`,
`X-Forwarded-For`, `X-Frame-Options`, `WWW-Authenticate`).

### 8. `app_packages`: App package and bundle identifiers

Reverse-domain identifiers whose suffix encodes the customer's
brand. The same shape covers Android packages, iOS bundle ids,
and desktop-app reverse-domain identifiers (Snap, MSIX, Electron),
so they share one category and one placeholder strategy.

| Original | Placeholder |
|---|---|
| `com.acmebank.app` (Android) | `com.vendor.app` |
| `com.contoso.voice.beta` (Android) | `com.vendor.app.beta` |
| `it.acmebank.mobile` (iOS bundle id) | `com.vendor.app` |
| `com.acmebank.app.watchkit-extension` (iOS WatchKit) | `com.vendor.app000NNN` |

**Not flagged**: SDK / library packages and OS frameworks
(`com.google.firebase.*`, `com.android.*`, `androidx.*`,
`org.bouncycastle.*`, `com.apple.*`, system bundles like
`com.apple.security.codesigning`, `com.google.GooglePlus`,
`io.flutter.plugins.*`, `com.facebook.react.*`).

### 9. `user_agents`: customer-app UA strings

Client User-Agent strings that name the customer's mobile or
desktop app, including iOS-flavoured CFNetwork forms and
custom-stack desktop user-agents.

| Original | Placeholder |
|---|---|
| `AcmeApp/3.4 (Android)` | `VendorApp/1.0-android` |
| `CustomerApp/25.1.2135-android` | `VendorApp/1.0-android` |
| `AcmeApp/3.4 CFNetwork/1220.1 Darwin/22.5.0` (iOS) | `VendorApp/1.0 CFNetwork/0000.0 Darwin/0.0.0` |
| `AcmeApp/2.1 (Macintosh; Intel Mac OS X 14_0)` | `VendorApp/1.0 (Macintosh; Intel Mac OS X 0_0)` |

**Not flagged**: standard browser UAs (`Mozilla/5.0 (…) Chrome/…`),
`curl/7.x`, `Wget/1.x`, generic SDK UAs (`okhttp/4.x`,
`python-requests/2.x`).

### 10. `ids`: internal tracking and advisory IDs

Identifier strings whose prefix encodes the customer (`ACME-…`,
`CONTOSO-…`, `CUST-…`). The placeholder swaps the prefix while
preserving the suffix so cross-references inside the report still
point at the right finding.

| Original | Placeholder |
|---|---|
| `ACME-CHAIN-A` | `VENDOR-CHAIN-A` |
| `CONTOSO-VULN-12` | `VENDOR-VULN-12` |
| `CUST-INC-9001` | `VENDOR-INC-9001` |

**Not flagged**: CVE identifiers (`CVE-2025-1234`), CWE
identifiers, OWASP references (`A03:2021`), CVSS strings.

### 11. `other`: proprietary URI schemes and deeplinks

Anything that doesn't fit the previous categories: proprietary
URI schemes that are not in the IANA standard list, custom
deeplinks, vendor-tied tokens that are still real but don't have
a more specific home.

| Original | Placeholder |
|---|---|
| `acme-app://chat?room=42` | `vapp://chat?room=42` |
| `customerapp://services/provision?token=any` | `app://services/provision?token=any` |

The IANA standard scheme list (kept in sync with the prompt) is
`http`, `https`, `ftp`, `sftp`, `ssh`, `file`, `mailto`, `tel`,
`data`, `blob`, `ws`, `wss`, `sip`, `sips`, `urn`, `about`,
`javascript`. Anything else is treated as a customer-proprietary
deeplink and the **whole URL** is rewritten so scheme + host +
path are anonymized together.

### 12. `infra_ids`: cloud / Active-Directory / infrastructure resource identifiers

Customer-tied resource identifiers that show up in cloud, Active
Directory, network and on-prem infrastructure pentests. The
pipeline keeps the structural prefix (so the placeholder still
parses as the same kind of identifier) and rewrites the
customer-tied tail with a deterministic sequential index.

The Tier-0 regex layer in
[`config/leak_patterns.yml`](https://github.com/nemmusu/report-anonymizer/blob/master/config/leak_patterns.yml)
catches the four most common deterministic shapes: AWS ARN, EC2
instance id, UUID (Azure tenant / subscription / AD ObjectGUID)
and Active-Directory SID. The LLM detector handles the looser
shapes (GCP project ids, branded `DC=…` distinguished-name
fragments, branded Kubernetes namespaces).

| Original | Placeholder |
|---|---|
| `arn:aws:iam::123456789012:role/AdminRole` (AWS ARN) | `arn:aws:iam::000000000001:role/vendor-1` |
| `i-0a1b2c3d4e5f6789a` (EC2 instance id) | `i-0a1b2c3d000000001` |
| `12345678-1234-5678-1234-567812345678` (Azure tenant UUID, AD ObjectGUID) | `12345678-0000-0000-0000-000000000001` |
| `S-1-5-21-1234567890-987654321-111222333-1001` (AD SID) | `S-1-5-21-0000000001` |
| `acme-prod-12345` (GCP project id encoding the customer) | `vendor-prod-0000001` |
| `CN=John Doe,OU=IT,DC=acme,DC=local` (AD distinguished name) | `CN=user01,OU=Sales,DC=vendor,DC=local` |
| `MSSQLSvc/sql01.acme.local:1433` (SPN) | rewritten as `network` (host part) plus `infra_ids` for the service prefix when branded |

The placeholder strategy lives in
[`anonymize/placeholders.py:infra_id_placeholder`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/placeholders.py);
it dispatches by shape so AWS ARNs keep the partition prefix,
EC2 IDs keep the `i-` prefix, UUIDs keep the first 8 hex of the
source, and SIDs keep the well-known authority block.

**Not flagged**: AWS service ARNs that don't carry an account id
(`arn:aws:iam::aws:role/AWSServiceRoleFor…`), Azure built-in
SIDs (`S-1-5-32-…` matches but the placeholder reuses the
canonical `S-1-5-32-…` prefix), Kubernetes namespaces that don't
encode the customer (`default`, `kube-system`, `monitoring`),
generic AD groups that ship with Windows (`Domain Users`,
`Enterprise Admins`, these are role names, not customer
identifiers).

## Embedded images

Text rules cover the prose. **Image content is handled by a parallel
pass** that surfaces every embedded image in the input as a
thumbnail in the **Review &raquo; Images** tab. Each image is
identified by `image_id = "sha256:" + sha256(raw_image_bytes)`, so
the same logo across 12 pages produces a single decision.

**Four tools** are available in the per-image editor, all rendered
into actual baked pixels (the canvas re-renders on every change so
the operator sees the real result, not a translucent overlay):

| Tool | Renders | Use case |
|---|---|---|
| **Blackout** | Solid black rectangle | Customer logo, sensitive name in a screenshot |
| **Blur** | Gaussian blur (configurable radius) | Faces, screenshots whose context matters but identifying details don't |
| **Pixelate** | NEAREST-resampled mosaic | Same as blur but with stronger irreversibility cues |
| **Text overlay** | Coloured background rectangle + centred text | "REDACTED" badges, custom labels with custom font / background colour |

<figure markdown="span">
  ![Image review tab with editor: thumbnail strip on top, blackout rectangle baked over a Burp request](screenshots/review-images.png)
  <figcaption>Per-image editor with the four redaction tools. Live bake means what you draw is what Apply will write.</figcaption>
</figure>

**Identity guarantee.** Apply replaces image bytes IN PLACE at the
same xref (PDF) / shape position (DOCX, PPTX), so the output
keeps the same number of images, in the same files, in the same
positions, with the same dimensions. The verifier post-stage
asserts this via an inventory cross-check; any mismatch is logged
in `verifier_report.md`.

**Out of scope (intentionally).** OCR-assist (auto-detect text
regions) is not implemented; vector-graphics inside PDF pages are
flagged with a warning but not editable from the GUI; ODT and
XLSX images surface a "no editor support yet" notice. None of
these affect the text pipeline, only the image-redaction surface.

## What the pipeline never touches

The following classes of strings are deliberately preserved
because removing them would break the report's technical
narrative or because they are not customer-identifying.

- **Technical content of the report**: descriptions, payloads,
  exploit code, shell commands, tool output snippets, request /
  response bodies (only the values they contain may be flagged,
  not the surrounding code).
- **Standards and well-known libraries**: NaCl, libsodium, OpenSSL,
  OAuth, OAuth2, JWT, SAML, OIDC, WebRTC, FCM, APNS, OneSignal,
  Firebase, Google Play Services, Apple Push, libsignal,
  Curve25519, Ed25519, AES, SHA-256, HMAC, PBKDF2, Argon2.
- **Standard constants and reserved ranges**: RFC 5737, RFC 1918,
  loopback, well-known public DNS, RFC 2606 example domains.
- **Generic OS / SDK / library versions**: `Android 10`, `iOS 17`,
  `Java 17`, `Python 3.12`, `OpenSSL 3.0`.
- **Generic hardware models**: `Samsung SM-A920F`, `Xiaomi Redmi`,
  `Pixel 7`, `iPhone 14`, these are lab-test details, not
  customer-identifying.
- **Dates** in any format: `7 May 2026`, `2026-05-07`,
  `7 maggio 2026`.
- **Generic file names and project paths**: `ADVISORY.md`,
  `README.md`, `exploit_usage.md`, `debug_server.py`,
  `data/dev_keypair.json`, `src/main/java/...`,
  `proof/screenshot.png`, technical artefact names.
- **Generic descriptive endpoints**: `/api/v1/login`, `/healthz`,
  `/metrics`, `keyserver/v1/publish`.
- **Variable / function / class names** found in code (including
  R8 / ProGuard obfuscated names like `pi.a.n`, `zi/a.java`,
  `License.WebServiceURI`, `sodium_key`, `key_id`, `encoded_key`,
  `OWN_KEY`, `DEV_PUB`, `VICTIM_PUB`, `crypto_box`).
- **Test / placeholder identifiers** that already document an
  attack: `VITTIMA`, `VICTIM`, `Lab1`, `Lab2`, `attaccante`,
  `mitm`.
- **Generic security terms**: MITM, CSRF, XSS, RCE, SSRF, CVE-*,
  CVSS, CWE, OWASP.
- **Already-anonymized values**: previous placeholders
  (`+39NNN0000NNN`, `203.0.113.NN`, `vendor.example`,
  `X-Vendor-*`, `VENDOR-CHAIN-*`).

## How the two tiers cooperate

- **Tier-0** ([`anonymize/rules_pass.py`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/rules_pass.py))
  is a deterministic regex pass. It catches phone numbers, IP
  addresses and 32 / 64-hex tokens *without* touching the LLM,
  and assigns a stable index (`+393331111111` always resolves to
  the same placeholder via `decisions_history.jsonl`). Tier-0
  hits auto-promote.
- **Tier-1** ([`anonymize/detector.py`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/detector.py))
  is the LLM detector with the prompt described above. It walks
  the document chunk by chunk via the structure-aware splitter,
  emits candidates with category + suggested placeholder +
  confidence, then a critic pass checks each candidate against the
  "is this *really* a customer-identifying value?" question. High
  confidence + critic-approved candidates auto-promote; the rest
  go to the human Review queue.

## Why the categories matter

The category drives:

- **Placeholder format**: phones get `+CC<carrier>0000NNN`, IPs
  get RFC 5737, hex tokens get the 8-char-prefix-preserving
  rule, etc. Code in [`anonymize/placeholders.py`](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/placeholders.py).
- **Auto-promotion threshold**: some categories (Tier-0 phones,
  Tier-0 IPs) auto-promote on the first hit; others (`brand`,
  `credentials`, `other`) require critic agreement.
- **Per-project review**: in the GUI's Review pane, candidates
  are grouped by category so the operator can blast through
  homogeneous sets quickly (approve all phones, edit
  questionable brand variants by hand).
