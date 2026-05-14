#!/usr/bin/env bash
#
# Build both the .deb and the .AppImage in one shot.
#
#     ./packaging/build-all.sh                 # build both, default version
#     ./packaging/build-all.sh --version 0.2.0
#     ./packaging/build-all.sh --only deb
#     ./packaging/build-all.sh --only appimage
#     ./packaging/build-all.sh --clean         # forwarded to both inner builds
#
# Output:
#     packaging/deb/dist/report-anonymizer_<version>_amd64.deb
#     packaging/appimage/dist/Report-Anonymizer-x86_64.AppImage
#
# Each inner build is independent and idempotent. This wrapper just
# sequences them and surfaces a single pass / fail at the end.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ONLY=""
VERSION="${VERSION:-}"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)     shift; ONLY="${1:-}" ;;
        --version)  shift; VERSION="${1:-}" ;;
        --clean)    EXTRA_ARGS+=(--clean) ;;
        -h|--help)
            sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if [[ -n "$VERSION" ]]; then
    DEB_VERSION_ARGS=(--version "$VERSION")
else
    DEB_VERSION_ARGS=()
fi

if [[ -t 1 ]]; then
    BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
    RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else
    BOLD=""; GREEN=""; RED=""; RESET=""
fi

step() { printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
fail() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

DO_DEB=1
DO_APP=1
case "$ONLY" in
    "")             ;;
    deb)            DO_APP=0 ;;
    appimage|app)   DO_DEB=0 ;;
    *) fail "unknown --only target: $ONLY (use deb / appimage)" ;;
esac

if (( DO_DEB )); then
    step "deb"
    "$SCRIPT_DIR/deb/build.sh" "${DEB_VERSION_ARGS[@]}" "${EXTRA_ARGS[@]}"
    ok "deb done"
fi

if (( DO_APP )); then
    step "appimage"
    "$SCRIPT_DIR/appimage/build.sh" "${EXTRA_ARGS[@]}"
    ok "appimage done"
fi

step "All artefacts"
ls -lh "$SCRIPT_DIR/deb/dist/"*.deb 2>/dev/null || true
ls -lh "$SCRIPT_DIR/appimage/dist/"*.AppImage 2>/dev/null || true
