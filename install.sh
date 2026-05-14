#!/usr/bin/env bash
#
# Report Anonymizer, one-line installer.
#
# Usage:
#     curl -fsSL https://raw.githubusercontent.com/nemmusu/report-anonymizer/master/install.sh | bash
#
# What it does, in order:
#   1. Verifies Python >= 3.10, git and pip on the host (and offers to
#      install missing OS-level tools, pandoc, wkhtmltopdf,
#      pdftotext, via the system package manager when available).
#   2. Clones (or updates) the repo into
#      ${REPORT_ANONYMIZER_HOME:-$HOME/.local/share/report-anonymizer}.
#   3. Creates a per-install virtualenv at $APP_HOME/.venv.
#   4. Installs the runtime dependencies from requirements.txt.
#   5. Writes a launcher to ~/.local/bin/report-anonymizer that
#      activates the venv and forwards arguments. The launcher
#      exposes:
#          report-anonymizer            -> launch the GUI
#          report-anonymizer cli ARGS   -> CLI passthrough
#          report-anonymizer update     -> git pull + reinstall deps
#          report-anonymizer uninstall  -> see scripts/uninstall.sh
#          report-anonymizer version    -> short build info
#
# Idempotent: re-running ``curl | bash`` updates the install in place.
# Quiet flag: pass --yes via env var ``ASSUME_YES=1`` to skip the
# confirmation prompt (useful for CI / containers).
#
# This script never runs as root and never touches /usr; everything
# lives under $HOME so ``uninstall`` (or ``rm -rf`` of the directories
# below) is a clean undo. The OS-deps prompt is the one place we use
# sudo, and only after explicit y/N confirmation.

set -euo pipefail

# ----------------------------------------------------------------------
# Pretty output helpers (no colour / box-drawing when stdout is not a
# TTY, keeps logs and CI clean).
# ----------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD="$(printf '\033[1m')"
    DIM="$(printf '\033[2m')"
    RED="$(printf '\033[31m')"
    GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"
    BLUE="$(printf '\033[34m')"
    CYAN="$(printf '\033[36m')"
    RESET="$(printf '\033[0m')"
    BOX_TL="╭"; BOX_TR="╮"; BOX_BL="╰"; BOX_BR="╯"
    BOX_H="─"; BOX_V="│"
    GLYPH_OK="✓"; GLYPH_ERR="✗"; GLYPH_WARN="!"; GLYPH_DOT="·"
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RESET=""
    BOX_TL="+"; BOX_TR="+"; BOX_BL="+"; BOX_BR="+"
    BOX_H="-"; BOX_V="|"
    GLYPH_OK="OK"; GLYPH_ERR="X"; GLYPH_WARN="!"; GLYPH_DOT="."
fi

say()  { printf '%s\n' "${BOLD}$*${RESET}"; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }
ok()   { printf '%s\n' "${GREEN}${GLYPH_OK}${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}${GLYPH_WARN}${RESET} $*"; }
fail() { printf '%s\n' "${RED}${GLYPH_ERR}${RESET} $*" >&2; exit 1; }

# Counted step header. Falls back to a plain numeric prefix when the
# terminal is too narrow for the bold/dim accents to land cleanly.
STEP_TOTAL=5
STEP_NUM=0
step() {
    STEP_NUM=$((STEP_NUM + 1))
    printf '\n%s[%d/%d]%s %s%s%s\n' \
        "$CYAN" "$STEP_NUM" "$STEP_TOTAL" "$RESET" \
        "$BOLD" "$*" "$RESET"
}

# Boxed banner, ASCII fallback already wired into BOX_* above.
banner() {
    local title="$1"
    local subtitle="${2:-}"
    local width=58
    local top mid_t mid_s bot
    top=""; bot=""
    local i
    for ((i = 0; i < width; i++)); do top+="$BOX_H"; bot+="$BOX_H"; done
    mid_t=$(printf '%s  %-*s  %s' "$BOX_V" $((width - 4)) "$title" "$BOX_V")
    printf '\n%s%s%s%s%s\n' "$BOLD" "$BOX_TL" "$top" "$BOX_TR" "$RESET"
    printf '%s%s%s\n' "$BOLD" "$mid_t" "$RESET"
    if [[ -n "$subtitle" ]]; then
        mid_s=$(printf '%s  %-*s  %s' "$BOX_V" $((width - 4)) "$subtitle" "$BOX_V")
        printf '%s%s%s\n' "$DIM" "$mid_s" "$RESET"
    fi
    printf '%s%s%s%s%s\n' "$BOLD" "$BOX_BL" "$bot" "$BOX_BR" "$RESET"
}

trap 'fail "installer aborted on line $LINENO"' ERR

# Optional non-interactive flag (env var). 1 = skip every y/N prompt.
ASSUME_YES="${ASSUME_YES:-0}"

# ----------------------------------------------------------------------
# Paths.
# ----------------------------------------------------------------------
APP_HOME="${REPORT_ANONYMIZER_HOME:-$HOME/.local/share/report-anonymizer}"
LAUNCHER_DIR="${REPORT_ANONYMIZER_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER="$LAUNCHER_DIR/report-anonymizer"
REPO_URL="${REPORT_ANONYMIZER_REPO:-https://github.com/nemmusu/report-anonymizer.git}"
BRANCH="${REPORT_ANONYMIZER_BRANCH:-master}"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

banner "Report Anonymizer · installer" \
       "${APP_HOME//$HOME/~}  ($BRANCH)"

# ----------------------------------------------------------------------
# 0. Refuse to run as root: every directory we touch lives under $HOME.
# ----------------------------------------------------------------------
if [[ "$(id -u)" -eq 0 ]]; then
    fail "Run as a regular user, not root. Everything installs under \$HOME."
fi

# ----------------------------------------------------------------------
# 1. Pre-requisites: python >= 3.10, git, pip module, and the
#    OS-level binaries the engine shells out to (pandoc, wkhtmltopdf,
#    pdftotext). When the latter are missing AND a known package
#    manager is on PATH, offer to install them via that PM, only
#    after explicit y/N confirmation. Never silently sudo.
# ----------------------------------------------------------------------
step "Pre-requisites"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "missing dependency: $1 (install it with your package manager and re-run)"
}

# ---- OS package manager detection ------------------------------------
# Returns the PM name on stdout (apt-get / dnf / pacman / zypper /
# brew), empty string on unknown.
_detect_pm() {
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        echo "brew"; return
    fi
    if command -v apt-get >/dev/null 2>&1; then echo "apt-get"; return; fi
    if command -v dnf     >/dev/null 2>&1; then echo "dnf";     return; fi
    if command -v pacman  >/dev/null 2>&1; then echo "pacman";  return; fi
    if command -v zypper  >/dev/null 2>&1; then echo "zypper";  return; fi
    echo ""
}

# Map a logical dep ("pandoc", "wkhtmltopdf", "pdftotext") to the right
# package name on the active PM. ``pdftotext`` is shipped by different
# packages depending on the distro (poppler-utils on apt/dnf,
# poppler on pacman/brew).
_pkg_for() {
    local pm="$1" dep="$2"
    case "$pm:$dep" in
        apt-get:pandoc)        echo "pandoc" ;;
        apt-get:wkhtmltopdf)   echo "wkhtmltopdf" ;;
        apt-get:pdftotext)     echo "poppler-utils" ;;
        dnf:pandoc)            echo "pandoc" ;;
        dnf:wkhtmltopdf)       echo "wkhtmltopdf" ;;
        dnf:pdftotext)         echo "poppler-utils" ;;
        pacman:pandoc)         echo "pandoc-cli" ;;
        pacman:wkhtmltopdf)    echo "wkhtmltopdf" ;;
        pacman:pdftotext)      echo "poppler" ;;
        zypper:pandoc)         echo "pandoc" ;;
        zypper:wkhtmltopdf)    echo "wkhtmltopdf" ;;
        zypper:pdftotext)      echo "poppler-tools" ;;
        brew:pandoc)           echo "pandoc" ;;
        brew:wkhtmltopdf)      echo "wkhtmltopdf" ;;
        brew:pdftotext)        echo "poppler" ;;
        *) echo "" ;;
    esac
}

# Run the install command for ``$pm`` against ``$pkgs`` (space-sep).
_pm_install() {
    local pm="$1"; shift
    local pkgs="$*"
    case "$pm" in
        apt-get) sudo apt-get update -qq && sudo apt-get install -y $pkgs ;;
        dnf)     sudo dnf install -y $pkgs ;;
        pacman)  sudo pacman -S --needed --noconfirm $pkgs ;;
        zypper)  sudo zypper -n install $pkgs ;;
        brew)    brew install $pkgs ;;
        *)       return 1 ;;
    esac
}

OPTIONAL_OS_DEPS=(pandoc wkhtmltopdf pdftotext)
missing_os=()
for dep in "${OPTIONAL_OS_DEPS[@]}"; do
    command -v "$dep" >/dev/null 2>&1 || missing_os+=("$dep")
done

if (( ${#missing_os[@]} > 0 )); then
    pm="$(_detect_pm)"
    if [[ -n "$pm" ]]; then
        # Build the package list for the active PM.
        pkgs=""
        for d in "${missing_os[@]}"; do
            p="$(_pkg_for "$pm" "$d")"
            [[ -n "$p" ]] && pkgs+="$p "
        done
        pkgs="${pkgs% }"
        warn "missing system tools: ${missing_os[*]}"
        info "they're optional at install time but required at run time:"
        info "  pandoc      , Markdown / HTML conversion"
        info "  wkhtmltopdf , HTML → PDF for the Build / Export stages"
        info "  pdftotext   , PDF text extraction (poppler)"
        if (( ASSUME_YES )); then
            reply="y"
        elif [[ -t 0 ]]; then
            printf '\n%sInstall via %s?%s [Y/n] ' "$BOLD" "$pm" "$RESET"
            read -r reply
            reply="${reply:-Y}"
        else
            warn "non-interactive shell, skipping. Install manually: $pm install $pkgs"
            reply="n"
        fi
        case "$reply" in
            y|Y|yes|YES)
                say "running: $pm install $pkgs"
                if _pm_install "$pm" $pkgs; then
                    ok "system tools installed"
                else
                    warn "package manager returned an error, continue anyway, install them by hand later"
                fi
                ;;
            *)
                info "skipping. Install manually: $pm install $pkgs"
                ;;
        esac
    else
        warn "missing system tools: ${missing_os[*]}"
        warn "no known package manager detected; install them manually before first run"
    fi
fi

need_cmd git

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    done
fi
[[ -n "${PYTHON_BIN:-}" ]] || fail "no python interpreter found (need python >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR})"

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION##*.}"
if (( PY_MAJOR < PY_MIN_MAJOR )) || (( PY_MAJOR == PY_MIN_MAJOR && PY_MINOR < PY_MIN_MINOR )); then
    fail "python ${PY_VERSION} is too old; need >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR}"
fi
ok "python ${PY_VERSION} at ${PYTHON_BIN}"

if ! "$PYTHON_BIN" -c 'import venv' >/dev/null 2>&1; then
    fail "the 'venv' module is missing in this python, install python3-venv (Debian/Ubuntu) or the equivalent package"
fi
if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    fail "the 'pip' module is missing in this python, install python3-pip or rerun ensurepip"
fi
ok "venv + pip present"

# ----------------------------------------------------------------------
# 2. Clone or update the repo.
# ----------------------------------------------------------------------
step "Source"

mkdir -p "$(dirname "$APP_HOME")"
if [[ -d "$APP_HOME/.git" ]]; then
    info "updating existing checkout in-place"
    git -C "$APP_HOME" fetch --depth=1 origin "$BRANCH"
    git -C "$APP_HOME" reset --hard "origin/$BRANCH"
else
    info "cloning $REPO_URL"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$APP_HOME"
fi
ok  "source ready at $APP_HOME"

# ----------------------------------------------------------------------
# 3. Virtualenv + dependencies.
# ----------------------------------------------------------------------
step "Python virtualenv + dependencies"

VENV="$APP_HOME/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    info "creating virtualenv at ${VENV//$HOME/~}"
    "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade --quiet pip
info "installing dependencies (this is the slow step, ~1-2 min) …"
"$VENV/bin/python" -m pip install --quiet -r "$APP_HOME/requirements.txt"
ok "dependencies installed"

# ----------------------------------------------------------------------
# 4. Launcher in ~/.local/bin.
# ----------------------------------------------------------------------
step "Launcher"

mkdir -p "$LAUNCHER_DIR"
cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/usr/bin/env bash
# Report Anonymizer launcher (auto-generated by install.sh).
set -euo pipefail
APP_HOME="${REPORT_ANONYMIZER_HOME:-$HOME/.local/share/report-anonymizer}"
VENV="$APP_HOME/.venv"
PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "Report Anonymizer is not installed (missing $PY)." >&2
    echo "Run the installer:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/nemmusu/report-anonymizer/master/install.sh | bash" >&2
    exit 127
fi

cmd="${1:-gui}"
case "$cmd" in
    ""|gui)
        cd "$APP_HOME"
        exec "$PY" -m gui.main "${@:2}"
        ;;
    cli)
        shift
        cd "$APP_HOME"
        exec "$PY" "$APP_HOME/bin/anonymize-dossier" "$@"
        ;;
    update)
        echo "[update] git pull + pip install -r requirements.txt"
        git -C "$APP_HOME" pull --ff-only
        "$PY" -m pip install --upgrade --quiet pip
        "$PY" -m pip install --quiet -r "$APP_HOME/requirements.txt"
        echo "[update] done, relaunch the GUI to pick up the changes"
        ;;
    uninstall)
        shift
        exec bash "$APP_HOME/scripts/uninstall.sh" "$@"
        ;;
    version|--version|-V)
        cd "$APP_HOME"
        commit=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
        date=$(git show -s --format=%ci HEAD 2>/dev/null || echo "?")
        echo "Report Anonymizer  commit $commit ($date)"
        ;;
    -h|--help|help)
        cat <<HELP_EOF
Report Anonymizer

Usage:
  report-anonymizer [gui]            launch the GUI (default)
  report-anonymizer cli ARGS         run the CLI (anonymize-dossier ARGS)
  report-anonymizer update           pull latest source + reinstall deps
  report-anonymizer uninstall [--all]
                                     remove the install (default keeps
                                     models, config, Docker image; pass
                                     --all to remove everything)
  report-anonymizer version          show commit + date

Environment:
  REPORT_ANONYMIZER_HOME             override the install directory
                                     (default: ~/.local/share/report-anonymizer)
HELP_EOF
        ;;
    *)
        # Anything else is treated as a CLI argument forwarded to
        # ``anonymize-dossier`` so power users can do
        # ``report-anonymizer all <input> -o <output>`` directly.
        cd "$APP_HOME"
        exec "$PY" "$APP_HOME/bin/anonymize-dossier" "$cmd" "${@:2}"
        ;;
esac
LAUNCHER_EOF
chmod +x "$LAUNCHER"
ok "launcher installed at $LAUNCHER"

# ----------------------------------------------------------------------
# 5. PATH hint (only when the launcher dir isn't already on PATH).
# ----------------------------------------------------------------------
step "Final checks"

case ":$PATH:" in
    *":$LAUNCHER_DIR:"*)
        ok "${LAUNCHER_DIR//$HOME/~} already on \$PATH"
        ;;
    *)
        warn "${LAUNCHER_DIR//$HOME/~} is not on your \$PATH, add this to your shell rc:"
        printf '\n    export PATH="%s:$PATH"\n\n' "$LAUNCHER_DIR"
        ;;
esac

# Friendly final summary card. Mirrors the opening banner so the
# session ends visually closed, with a one-glance recap of every
# command the user is likely to run next.
banner "All set." "Run \`report-anonymizer\` to launch the GUI"

printf '  %sLaunch GUI%s\n' "$BOLD" "$RESET"
printf '    report-anonymizer\n\n'
printf '  %sCLI%s\n' "$BOLD" "$RESET"
printf '    report-anonymizer cli all <input_folder> -o <output_folder>\n\n'
printf '  %sUpdate / version%s\n' "$BOLD" "$RESET"
printf '    report-anonymizer update      # git pull + reinstall deps\n'
printf '    report-anonymizer version     # commit + date\n\n'
printf '  %sUninstall%s\n' "$BOLD" "$RESET"
printf '    report-anonymizer uninstall          # keeps models + config\n'
printf '    report-anonymizer uninstall --all    # wipes models, config, Docker image\n\n'
