#!/usr/bin/env bash
#
# Build the Report Anonymizer ``.deb`` for Debian / Ubuntu / Mint.
#
# What you get
#     packaging/deb/dist/report-anonymizer_<version>_amd64.deb
#
# What's inside
#     /usr/lib/report-anonymizer/                       project tree (read-only at install time)
#         anonymize/, gui/, bin/, config/, prompts/, templates/, assets/, requirements.txt
#         .venv/                                    populated by postinst on install
#     /usr/bin/report-anonymizer                    launcher symlink
#     /usr/share/applications/report-anonymizer.desktop
#     /usr/share/icons/hicolor/256x256/apps/report-anonymizer.png  (when ImageMagick is on PATH)
#
# Why ``/opt`` and not ``/usr``
#     The venv is per-install (postinst runs ``python3 -m venv``);
#     putting it under ``/opt`` keeps it explicitly out of the FHS
#     tree managed by dpkg, which means ``apt remove`` cleans the
#     code while ``apt purge`` also wipes any leftover venv state.
#
# Dependencies declared in ``control``
#     python3 (>= 3.10), python3-venv, python3-pip, pandoc,
#     poppler-utils, libpango-1.0-0, libpangoft2-1.0-0,
#     libcairo2, fontconfig, fonts-inter | fonts-liberation
#
# Why no system PySide6 / WeasyPrint
#     Distro PySide6 packages lag 6+ months and we pin specific
#     versions in requirements.txt. The postinst pip-installs
#     everything into the per-install venv from the bundled
#     wheels (offline-friendly) or from PyPI as a fallback.
#
# Usage
#     ./packaging/deb/build.sh                       # full build
#     ./packaging/deb/build.sh --version 0.2.0       # override version
#     ./packaging/deb/build.sh --clean               # nuke build cache
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORK="$SCRIPT_DIR/build-cache"
DIST="$SCRIPT_DIR/dist"
PKG="report-anonymizer"
# Read the canonical version from ``pyproject.toml`` so the produced
# deb tracks the project release number out of the box. ``--version
# X.Y.Z`` or an explicit ``VERSION=`` env var still wins, which is
# what the release workflow relies on when it builds from a tag that
# hasn't bumped pyproject yet. Falls back to ``0.1.0`` only when
# pyproject is missing or unparsable.
_PYPROJECT_VERSION=$(awk -F'"' '
    /^\[project\]/ { in_project=1; next }
    /^\[/          { in_project=0 }
    in_project && /^version[[:space:]]*=/ { print $2; exit }
' "$REPO_ROOT/pyproject.toml" 2>/dev/null || true)
VERSION="${VERSION:-${_PYPROJECT_VERSION:-0.1.0}}"
ARCH="amd64"

# --- Args -------------------------------------------------------------
# ``while``-loop parsing (was ``for`` previously, which captures $@
# at iteration start so an inner ``shift`` to consume ``--version``'s
# value didn't actually advance the iterator, the value was then
# re-iterated and rejected as an unknown arg).
CLEAN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) shift; VERSION="${1:-$VERSION}" ;;
        --clean)   CLEAN=1 ;;
        -h|--help)
            sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

# --- Output helpers ---------------------------------------------------
if [[ -t 1 ]]; then
    BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"
    GREEN="$(printf '\033[32m')"; RED="$(printf '\033[31m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; GREEN=""; RED=""; RESET=""
fi
say()  { printf '%s\n' "${BOLD}$*${RESET}"; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
fail() { printf '%s\n' "${RED}✗${RESET} $*" >&2; exit 1; }

command -v dpkg-deb >/dev/null 2>&1 || fail "dpkg-deb is required (sudo apt-get install -y dpkg)"

if (( CLEAN )); then
    info "wiping $WORK"
    rm -rf "$WORK"
fi
mkdir -p "$WORK" "$DIST"

ROOT="$WORK/${PKG}_${VERSION}_${ARCH}"
rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/lib/$PKG" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$ROOT/usr/share/doc/$PKG"

# --- Project payload -------------------------------------------------
say "[1/5] Project payload"
APP_PAYLOAD="$ROOT/usr/lib/$PKG"
for d in anonymize gui bin config prompts templates assets; do
    if [[ -d "$REPO_ROOT/$d" ]]; then
        cp -r "$REPO_ROOT/$d" "$APP_PAYLOAD/"
    fi
done
cp "$REPO_ROOT/requirements.txt" "$APP_PAYLOAD/"
cp "$REPO_ROOT/README.md" "$APP_PAYLOAD/"
# Strip __pycache__/ that sneaks in from the developer's checkout -
# lintian complains, and the venv would regenerate it on first run
# anyway.
find "$APP_PAYLOAD" -type d -name __pycache__ -prune -exec rm -rf {} +
ok "project tree copied to /opt/$PKG"

# --- Desktop entry + launcher --------------------------------------------
say "[2/5] Desktop entry + launcher"
cat > "$ROOT/usr/bin/$PKG" <<'LAUNCHER_EOF'
#!/usr/bin/env bash
# Report Anonymizer launcher (deb-installed copy).
# Activates /usr/lib/report-anonymizer/.venv and forwards arguments.
set -euo pipefail
APP_HOME="/usr/lib/report-anonymizer"
PY="$APP_HOME/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    echo "report-anonymizer venv missing at $PY" >&2
    echo "Try: sudo dpkg-reconfigure report-anonymizer" >&2
    exit 127
fi
cmd="${1:-gui}"
case "$cmd" in
    ""|gui)
        cd "$APP_HOME"; exec "$PY" -m gui.main "${@:2}" ;;
    cli)
        shift; cd "$APP_HOME"; exec "$PY" "$APP_HOME/bin/anonymize-dossier" "$@" ;;
    version|--version|-V)
        echo "Report Anonymizer (deb) version __VERSION__" ;;
    -h|--help|help)
        cat <<HELP
Report Anonymizer (deb-installed)
Usage:
  report-anonymizer [gui]            launch the GUI (default)
  report-anonymizer cli ARGS         run the CLI
  report-anonymizer version          show package version

Install / remove the package via apt:
  sudo apt install ./report-anonymizer_<version>_amd64.deb
  sudo apt remove report-anonymizer
HELP
        ;;
    *)
        cd "$APP_HOME"; exec "$PY" "$APP_HOME/bin/anonymize-dossier" "$cmd" "${@:2}" ;;
esac
LAUNCHER_EOF
sed -i "s/__VERSION__/$VERSION/" "$ROOT/usr/bin/$PKG"
chmod 755 "$ROOT/usr/bin/$PKG"

cat > "$ROOT/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Report Anonymizer
GenericName=Document Anonymizer
Comment=Local LLM-driven anonymizer for penetration-test reports
Exec=$PKG %F
Icon=$PKG
Categories=Office;Utility;
Terminal=false
StartupNotify=true
MimeType=application/pdf;text/markdown;text/html;application/vnd.openxmlformats-officedocument.wordprocessingml.document;
Keywords=anonymize;redact;pentest;security;report;
EOF

# Icon: rasterise from SVG if ImageMagick is on PATH; else fall back to SVG
if command -v convert >/dev/null 2>&1; then
    convert -background none -resize 256x256 \
        "$REPO_ROOT/assets/app_icon.svg" \
        "$ROOT/usr/share/icons/hicolor/256x256/apps/$PKG.png" 2>/dev/null \
        && ok "icon rasterised (PNG)" \
        || cp "$REPO_ROOT/assets/app_icon.svg" \
              "$ROOT/usr/share/icons/hicolor/256x256/apps/$PKG.png"
else
    cp "$REPO_ROOT/assets/app_icon.svg" \
       "$ROOT/usr/share/icons/hicolor/256x256/apps/$PKG.png"
fi

cp "$REPO_ROOT/README.md" "$ROOT/usr/share/doc/$PKG/README.md"

# Minimal native-package changelog (gzipped). lintian errors out on
# `no-changelog` for any non-trivial Debian package.
cat > "$WORK/changelog" <<EOF
$PKG ($VERSION) unstable; urgency=low

  * Initial Debian package.

 -- Cristian Steri <steri.cristian@gmail.com>  $(date -R)
EOF
gzip -9n -c "$WORK/changelog" > "$ROOT/usr/share/doc/$PKG/changelog.gz"
rm "$WORK/changelog"

# Minimal Debian-format copyright file. The full source license
# travels with the project tree at /usr/lib/report-anonymizer/.
cat > "$ROOT/usr/share/doc/$PKG/copyright" <<'COPY_EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: report-anonymizer
Source: https://github.com/nemmusu/report-anonymizer

Files: *
Copyright: 2026 Cristian Steri <steri.cristian@gmail.com>
License: GPL-3.0+
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <https://www.gnu.org/licenses/>.
 On Debian systems, the full text of the GNU General Public License
 version 3 can be found in '/usr/share/common-licenses/GPL-3'.
COPY_EOF

ok "launcher + desktop entry written"

# --- DEBIAN/ control + maintainer scripts ---------------------------
say "[3/5] DEBIAN metadata"

# Computed installed size in KB (excluding DEBIAN/).
ISIZE=$(du -sk --exclude=DEBIAN "$ROOT" | awk '{print $1}')

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $ISIZE
Depends: python3 (>= 3.10), python3-venv, python3-pip, pandoc, poppler-utils, libpango-1.0-0, libpangoft2-1.0-0, libcairo2, fontconfig, fonts-liberation
Recommends: docker.io | docker-ce
Maintainer: Cristian Steri <steri.cristian@gmail.com>
Homepage: https://github.com/nemmusu/report-anonymizer
Description: LLM-driven anonymizer for penetration-test reports
 Local LLM-driven redaction for PDF, Office, Markdown and source code
 documents. Detects vendor names, IPs, hostnames, credentials, etc.,
 swaps them with stable placeholders and produces an anonymised copy
 plus a verifier report.
 .
 The package installs to /usr/lib/report-anonymizer/. On install the
 postinst creates a per-install Python venv and pip-installs the
 runtime dependencies (PySide6, WeasyPrint, PyMuPDF, ...) from PyPI.
 Network access is required during install only; the app runs
 fully offline afterwards.
EOF

cat > "$ROOT/DEBIAN/postinst" <<'POSTINST_EOF'
#!/bin/bash
# postinst: build the per-install Python venv at first install /
# upgrade. Idempotent: re-running ``apt install --reinstall`` keeps
# the venv if requirements.txt didn't change.
set -e
APP_HOME="/usr/lib/report-anonymizer"
VENV="$APP_HOME/.venv"

case "$1" in
    configure)
        if [ ! -x "$VENV/bin/python" ]; then
            echo "report-anonymizer: creating venv at $VENV"
            python3 -m venv "$VENV"
        fi
        echo "report-anonymizer: installing Python deps (this can take a few minutes)..."
        "$VENV/bin/python" -m pip install --quiet --upgrade pip
        "$VENV/bin/python" -m pip install --quiet -r "$APP_HOME/requirements.txt"
        # Refresh the GTK / desktop / icon caches so the launcher
        # appears in the menu without a re-login.
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
        fi
        echo "report-anonymizer: install complete. Run 'report-anonymizer' to launch."
        ;;
    abort-upgrade|abort-remove|abort-deconfigure) ;;
    *) echo "postinst called with unknown arg: $1" >&2; exit 1 ;;
esac
exit 0
POSTINST_EOF

cat > "$ROOT/DEBIAN/prerm" <<'PRERM_EOF'
#!/bin/bash
# prerm: tear the venv down on remove / purge so subsequent
# installs always start clean. Models / config under
# ~/.local/share/document-anonymizer/ live in the user's HOME,
# never managed by the package.
set -e
APP_HOME="/usr/lib/report-anonymizer"
case "$1" in
    remove|purge|upgrade|deconfigure)
        if [ -d "$APP_HOME/.venv" ]; then
            rm -rf "$APP_HOME/.venv"
        fi
        ;;
    failed-upgrade) ;;
    *) echo "prerm called with unknown arg: $1" >&2; exit 1 ;;
esac
exit 0
PRERM_EOF

cat > "$ROOT/DEBIAN/postrm" <<'POSTRM_EOF'
#!/bin/bash
set -e
case "$1" in
    purge)
        # Belt-and-braces: nuke /usr/lib/report-anonymizer if dpkg
        # somehow left it behind (the venv is gone via prerm; the
        # rest of the project tree is owned by dpkg).
        rm -rf /usr/lib/report-anonymizer 2>/dev/null || true
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
        fi
        ;;
    remove|upgrade|failed-upgrade|abort-install|abort-upgrade|disappear) ;;
    *) echo "postrm called with unknown arg: $1" >&2; exit 1 ;;
esac
exit 0
POSTRM_EOF

chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postrm"

# Permissions: lintian wants 755 dirs / 644 files everywhere and
# refuses 775/664 (developer umask 002 leaks in otherwise). Apply
# the canonical pair to the entire packaged tree, then restore
# executable bits on the things that actually need them.
find "$ROOT" -path "$ROOT/DEBIAN" -prune -o -type d -exec chmod 0755 {} +
find "$ROOT" -path "$ROOT/DEBIAN" -prune -o -type f -exec chmod 0644 {} +
chmod 0755 "$ROOT/usr/bin/$PKG"
chmod 0755 "$ROOT/usr/lib/$PKG/bin/anonymize-dossier" 2>/dev/null || true
chmod 0755 "$ROOT/usr/lib/$PKG/bin/anonymize-dossier-gui" 2>/dev/null || true

ok "DEBIAN metadata written"

# --- Build the .deb -------------------------------------------------
say "[4/5] dpkg-deb"
OUT="$DIST/${PKG}_${VERSION}_${ARCH}.deb"
rm -f "$OUT"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT"
ok "built: $OUT  ($(du -h "$OUT" | awk '{print $1}'))"

# --- Lint with lintian (best-effort) -------------------------------
say "[5/5] lintian (optional)"
if command -v lintian >/dev/null 2>&1; then
    lintian --no-tag-display-limit "$OUT" || true
else
    info "lintian not installed; skip. (sudo apt-get install lintian)"
fi

echo
say "Install:"
printf '    %ssudo apt install %s%s\n' "$BOLD" "$OUT" "$RESET"
say "Remove:"
printf '    %ssudo apt remove %s%s\n' "$BOLD" "$PKG" "$RESET"
