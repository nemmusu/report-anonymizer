#!/usr/bin/env bash
#
# Report Anonymizer, uninstaller.
#
# Reachable via the launcher:
#     report-anonymizer uninstall            # keeps models + config
#     report-anonymizer uninstall --all      # also drops models, config, Docker image
#     report-anonymizer uninstall --yes      # skip the confirmation prompt
#
# What gets removed by default:
#   * $REPORT_ANONYMIZER_HOME (the cloned repo + the venv)
#   * the launcher in ~/.local/bin/report-anonymizer
#
# What ``--all`` adds:
#   * ~/.local/share/document-anonymizer/        (downloaded GGUF models)
#   * ~/.config/document-anonymizer/             (presets, HF token, prefs)
#   * /tmp/anondiff/                             (diff render cache)
#   * the configured Docker image (default ghcr.io/ggml-org/llama.cpp:server-cuda)
#     when the docker CLI is available.
#
# Idempotent: re-running it is safe; missing paths are skipped quietly.

set -euo pipefail

# --------------------------------------------------------------------
# Output helpers (no colour when not on a TTY).
# --------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD="$(printf '\033[1m')"
    DIM="$(printf '\033[2m')"
    RED="$(printf '\033[31m')"
    GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()  { printf '%s\n' "${BOLD}$*${RESET}"; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
fail() { printf '%s\n' "${RED}✗${RESET} $*" >&2; exit 1; }

# --------------------------------------------------------------------
# Args.
# --------------------------------------------------------------------
WIPE_ALL=0
ASSUME_YES=0
DOCKER_IMAGE_DEFAULT="ghcr.io/ggml-org/llama.cpp:server-cuda"
DOCKER_IMAGE="${LLAMA_DOCKER_IMAGE:-$DOCKER_IMAGE_DEFAULT}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)         WIPE_ALL=1 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        --image)       shift; DOCKER_IMAGE="${1:-$DOCKER_IMAGE_DEFAULT}" ;;
        -h|--help)
            cat <<HELP_EOF
Report Anonymizer uninstaller

Usage:
  report-anonymizer uninstall            keeps models + config + Docker image
  report-anonymizer uninstall --all      also wipes models, config, Docker image
  report-anonymizer uninstall --yes      skip the confirmation prompt
  report-anonymizer uninstall --image NAME
                                         override the Docker image to remove
HELP_EOF
            exit 0
            ;;
        *) fail "unknown argument: $1" ;;
    esac
    shift
done

APP_HOME="${REPORT_ANONYMIZER_HOME:-$HOME/.local/share/report-anonymizer}"
LAUNCHER_DIR="${REPORT_ANONYMIZER_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER="$LAUNCHER_DIR/report-anonymizer"
MODELS_DIR="$HOME/.local/share/document-anonymizer"
CONFIG_DIR="$HOME/.config/document-anonymizer"
DIFF_CACHE="/tmp/anondiff"

say  "Report Anonymizer · uninstaller"
info "Install directory : $APP_HOME"
info "Launcher          : $LAUNCHER"
if (( WIPE_ALL )); then
    info "Mode              : ${BOLD}wipe everything${RESET}${DIM} (--all)"
    info "  · models       : $MODELS_DIR"
    info "  · config       : $CONFIG_DIR"
    info "  · diff cache   : $DIFF_CACHE"
    info "  · Docker image : $DOCKER_IMAGE (if cached)"
else
    info "Mode              : remove app + launcher only (models + config kept)"
fi

# --------------------------------------------------------------------
# Confirmation. Skipped with --yes / ASSUME_YES=1 (CI / scripts).
# --------------------------------------------------------------------
if (( ! ASSUME_YES )); then
    if [[ -t 0 ]]; then
        printf '\n%sProceed?%s [y/N] ' "$BOLD" "$RESET"
        read -r reply
        case "${reply:-N}" in
            y|Y|yes|YES) : ;;
            *) info "aborted"; exit 0 ;;
        esac
    else
        warn "non-interactive shell detected; pass --yes to confirm"
        exit 1
    fi
fi
echo

# --------------------------------------------------------------------
# 1. Stop any GUI / llama-server / Docker container we own.
# --------------------------------------------------------------------
if command -v pgrep >/dev/null 2>&1; then
    if pgrep -f "$APP_HOME/.venv/bin/python" >/dev/null 2>&1; then
        warn "killing running Report Anonymizer processes"
        pkill -f "$APP_HOME/.venv/bin/python" || true
    fi
fi

if command -v docker >/dev/null 2>&1; then
    # Containers spawned by ``ServerManager._start_docker`` use the
    # deterministic name prefix ``report-anonymizer-<preset>``.
    stale=$(docker ps -aq --filter "name=^report-anonymizer-" 2>/dev/null || true)
    if [[ -n "$stale" ]]; then
        info "removing $(echo "$stale" | wc -l | tr -d ' ') Docker container(s) we spawned"
        docker rm -f $stale >/dev/null 2>&1 || true
    fi
fi

# --------------------------------------------------------------------
# 2. Always-removed targets (the install itself).
# --------------------------------------------------------------------
if [[ -d "$APP_HOME" ]]; then
    rm -rf "$APP_HOME"
    ok "removed $APP_HOME"
else
    info "$APP_HOME already gone"
fi

if [[ -e "$LAUNCHER" || -L "$LAUNCHER" ]]; then
    rm -f "$LAUNCHER"
    ok "removed launcher $LAUNCHER"
else
    info "$LAUNCHER already gone"
fi

# --------------------------------------------------------------------
# 3. Optional --all: models, config, diff cache, Docker image.
# --------------------------------------------------------------------
if (( WIPE_ALL )); then
    for path in "$MODELS_DIR" "$CONFIG_DIR" "$DIFF_CACHE"; do
        if [[ -d "$path" ]]; then
            rm -rf "$path"
            ok "removed $path"
        else
            info "$path already gone"
        fi
    done
    if command -v docker >/dev/null 2>&1; then
        if docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
            docker rmi -f "$DOCKER_IMAGE" >/dev/null 2>&1 || warn "could not remove Docker image $DOCKER_IMAGE"
            ok "removed Docker image $DOCKER_IMAGE"
        else
            info "Docker image $DOCKER_IMAGE not cached"
        fi
    else
        info "docker CLI not available, skipping image cleanup"
    fi
fi

echo
say  "Done."
if (( WIPE_ALL )); then
    info "All Report Anonymizer state has been removed."
else
    info "Models and config were kept. Pass --all to drop them too."
fi
