#!/usr/bin/env bash
#
# Build the Report Anonymizer AppImage.
#
# The build is fully reproducible from a clean machine: it downloads
# a portable Python interpreter (python-build-standalone), pandoc
# and pdftotext binaries from upstream releases, drops them into an
# AppDir alongside the project's site-packages, and packages the
# whole thing with appimagetool.
#
# Output:
#     packaging/appimage/dist/Report-Anonymizer-x86_64.AppImage
#
# Requirements on the build host:
#     bash, curl, tar, file, fuse2 (for running the resulting
#     AppImage at the end), and a writable /tmp.
#
# Usage:
#     ./packaging/appimage/build.sh                # full build
#     ./packaging/appimage/build.sh --no-pandoc    # rely on system pandoc
#     ./packaging/appimage/build.sh --clean        # nuke the build cache
#
# Idempotent: re-running reuses the downloaded interpreter / tool
# tarballs unless ``--clean`` is passed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORK="$SCRIPT_DIR/build-cache"
DIST="$SCRIPT_DIR/dist"
APPDIR="$WORK/AppDir"

# --- Pinned upstream artefacts ----------------------------------------
# Pinning by URL keeps reruns reproducible. Bump the constants below
# in lockstep with their checksums when refreshing the AppImage build.
PY_VERSION="3.12.13"
PY_DATE="20260508"
PY_TARBALL="cpython-${PY_VERSION}+${PY_DATE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
# python-build-standalone URL-encodes the ``+`` in filenames as %2B.
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_DATE}/cpython-${PY_VERSION}%2B${PY_DATE}-x86_64-unknown-linux-gnu-install_only.tar.gz"

PANDOC_VERSION="3.5"
PANDOC_TARBALL="pandoc-${PANDOC_VERSION}-linux-amd64.tar.gz"
PANDOC_URL="https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/${PANDOC_TARBALL}"

APPIMAGETOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"

# --- Args -------------------------------------------------------------
WANT_PANDOC=1
CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --no-pandoc) WANT_PANDOC=0 ;;
        --clean)     CLEAN=1 ;;
        -h|--help)
            sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# --- Output helpers ---------------------------------------------------
if [[ -t 1 ]]; then
    BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"
    GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RESET=""
fi
say()  { printf '%s\n' "${BOLD}$*${RESET}"; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
fail() { printf '%s\n' "${YELLOW}✗${RESET} $*" >&2; exit 1; }

# --- 0. Sanity ---------------------------------------------------------
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v tar  >/dev/null 2>&1 || fail "tar is required"

if (( CLEAN )); then
    info "wiping $WORK"
    rm -rf "$WORK"
fi
mkdir -p "$WORK" "$DIST"

# --- 1. Portable Python -----------------------------------------------
say "[1/6] Portable Python interpreter"
if [[ ! -d "$WORK/python" ]]; then
    info "downloading $PY_URL"
    curl -fsSL "$PY_URL" -o "$WORK/$PY_TARBALL"
    mkdir -p "$WORK/python"
    tar -xzf "$WORK/$PY_TARBALL" -C "$WORK/python" --strip-components=1
fi
PY="$WORK/python/bin/python3"
[[ -x "$PY" ]] || fail "portable python interpreter missing at $PY"
ok "python $($PY --version 2>&1 | awk '{print $2}') ready"

# --- 2. Pandoc (optional) ---------------------------------------------
say "[2/6] Pandoc"
if (( WANT_PANDOC )); then
    if [[ ! -x "$WORK/pandoc/bin/pandoc" ]]; then
        info "downloading $PANDOC_URL"
        curl -fsSL "$PANDOC_URL" -o "$WORK/$PANDOC_TARBALL"
        mkdir -p "$WORK/pandoc"
        tar -xzf "$WORK/$PANDOC_TARBALL" -C "$WORK/pandoc" --strip-components=1
    fi
    ok "pandoc $($WORK/pandoc/bin/pandoc --version | head -1 | awk '{print $2}') ready"
else
    info "skipping pandoc bundle (--no-pandoc); the AppImage will rely on system pandoc"
fi

# --- 3. pdftotext (poppler-utils) -------------------------------------
say "[3/6] pdftotext"
# Poppler isn't shipped as a tarball release; copy from the host if
# present. Fall back to a hint when missing, bundling poppler from
# source is too heavy for this script.
HOST_PDFTOTEXT="$(command -v pdftotext || true)"
if [[ -z "$HOST_PDFTOTEXT" ]]; then
    fail "pdftotext not found on the build host. Install poppler-utils first:\n   sudo apt-get install -y poppler-utils\n   sudo dnf install -y poppler-utils\n   sudo pacman -S poppler"
fi
ok "pdftotext at $HOST_PDFTOTEXT"

# --- 4. AppDir layout --------------------------------------------------
say "[4/6] AppDir scaffolding"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/document-anonymizer"

cp -r "$WORK/python/." "$APPDIR/usr/"
if (( WANT_PANDOC )); then
    install -m755 "$WORK/pandoc/bin/pandoc" "$APPDIR/usr/bin/pandoc"
fi
install -m755 "$HOST_PDFTOTEXT" "$APPDIR/usr/bin/pdftotext"

install -m755 "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
install -m644 "$SCRIPT_DIR/document-anonymizer.desktop" "$APPDIR/document-anonymizer.desktop"
install -m644 "$REPO_ROOT/assets/app_icon.svg" "$APPDIR/document-anonymizer.svg"
# AppImage spec requires a top-level icon symlink-or-png. Many file
# managers prefer a PNG; render one from the SVG via ImageMagick when
# available, else just keep the SVG (newer file managers handle it).
if command -v convert >/dev/null 2>&1; then
    convert -background none -resize 256x256 \
        "$APPDIR/document-anonymizer.svg" "$APPDIR/document-anonymizer.png" 2>/dev/null \
        && ok "PNG icon rasterised" \
        || warn "icon conversion failed; SVG-only icon"
fi

# --- 5. Drop the repo + install Python deps ---------------------------
say "[5/6] Project tree + Python dependencies"
APP_PAYLOAD="$APPDIR/usr/share/document-anonymizer"
# Copy only the bits the runtime needs. Everything else (tests, .git,
# benchmark logs) is omitted to keep the AppImage small.
for d in anonymize gui bin config prompts templates assets; do
    if [[ -d "$REPO_ROOT/$d" ]]; then
        cp -r "$REPO_ROOT/$d" "$APP_PAYLOAD/"
    fi
done
cp "$REPO_ROOT/requirements.txt" "$APP_PAYLOAD/"

# Install requirements directly into the bundled interpreter's
# site-packages. ``--no-user`` and ``--target`` keep things isolated
# from any host pip user-base; we use the bundled python directly.
info "pip install (this is the slow step, ~2-3 min) …"
"$APPDIR/usr/bin/python3" -m pip install --quiet --no-cache-dir --upgrade pip
"$APPDIR/usr/bin/python3" -m pip install --quiet --no-cache-dir \
    -r "$APP_PAYLOAD/requirements.txt"

# Strip pip's pyc/dist-info caches to shave a few MB.
find "$APPDIR/usr/lib/python"*/site-packages \
    -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR/usr/lib/python"*/site-packages \
    -name "*.dist-info" -type d -prune -exec rm -rf {} + 2>/dev/null || true

ok "AppDir size: $(du -sh "$APPDIR" | awk '{print $1}')"

# --- 6. appimagetool ---------------------------------------------------
say "[6/6] appimagetool"
if [[ ! -x "$WORK/appimagetool" ]]; then
    info "downloading appimagetool"
    curl -fsSL "$APPIMAGETOOL_URL" -o "$WORK/appimagetool"
    chmod +x "$WORK/appimagetool"
fi

OUT="$DIST/Report-Anonymizer-x86_64.AppImage"
rm -f "$OUT"
ARCH=x86_64 "$WORK/appimagetool" --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"

ok "built: $OUT  ($(du -h "$OUT" | awk '{print $1}'))"
echo
say "Smoke-test it:"
printf '    %s --version\n' "$OUT"
printf '    %s            # launches the GUI\n' "$OUT"
