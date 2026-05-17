#!/usr/bin/env bash
# AetherWard uninstaller — returns repo to release state
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[1;31m'; GRN='\033[1;32m'; YEL='\033[1;33m'
CYN='\033[1;36m'; DIM='\033[2m'; RST='\033[0m'

info()  { echo -e "${CYN}  ▸${RST} $*"; }
ok()    { echo -e "${GRN}  ✔${RST} $*"; }
warn()  { echo -e "${YEL}  ⚠${RST} $*"; }
err()   { echo -e "${RED}  ✘${RST} $*" >&2; }
sep()   { echo -e "\n${DIM}────────────────────────────────────────${RST}"; }

trap 'echo; err "Uninstall error on line $LINENO — see output above."; exit 1' ERR
yn() {
    local __prompt="$1" __def="${2:-Y}"
    local __hint; [[ "$__def" == Y ]] && __hint="Y/n" || __hint="y/N"
    echo -en "  ${CYN}?${RST} ${__prompt} ${DIM}[${__hint}]${RST}: "
    read -r __ans || true
    __ans="${__ans:-$__def}"
    [[ "${__ans,,}" == y* ]]
}

echo
[[ -f "$REPO_DIR/banner.txt" ]] && cat "$REPO_DIR/banner.txt" || echo -e "${CYN}AetherWard${RST}"
echo -e "  ${DIM}AetherWard uninstaller${RST}"
sep
warn "This will remove all build artifacts, virtual environments, and"
warn "uninstall the Python package from pip (user and venv installs)."
warn "Source files and configuration will NOT be touched."
sep
yn "Continue with uninstall?" N || { echo "  Aborted."; exit 0; }

# ── Privilege escalation ──────────────────────────────────────────────────────
PRIV_CMD=""
if   [[ "$EUID" -eq 0 ]];               then PRIV_CMD="direct"
elif command -v sudo &>/dev/null;        then PRIV_CMD="sudo"
elif command -v doas &>/dev/null;        then PRIV_CMD="doas"
fi

run_priv() {
    case "$PRIV_CMD" in
        direct) "$@" ;;
        sudo)   sudo "$@" ;;
        doas)   doas "$@" ;;
        *)      warn "Skipping (no root access): $*"; return 1 ;;
    esac
}

# ── Pip uninstall (user / current environment) ────────────────────────────────
sep
info "Removing pip package (user install)…"
if python3 -m pip show aetherward &>/dev/null 2>&1; then
    python3 -m pip uninstall --break-system-packages -y aetherward \
        && ok "Removed from user pip" \
        || warn "pip uninstall failed"
else
    ok "Not installed in user pip"
fi

# ── Pip uninstall (system, if privilege escalation available) ─────────────────
if [[ -n "$PRIV_CMD" ]] && run_priv python3 -m pip show aetherward &>/dev/null 2>&1; then
    info "Removing system pip install…"
    run_priv python3 -m pip uninstall --break-system-packages -y aetherward \
        && ok "Removed from system pip" \
        || warn "System pip uninstall failed"
fi

# ── Virtual environments ───────────────────────────────────────────────────────
sep
info "Removing virtual environments…"
REMOVED_VENV=false
for candidate in "$REPO_DIR/.venv" "$REPO_DIR/venv" "$REPO_DIR/env"; do
    if [[ -d "$candidate" ]]; then
        rm -rf "$candidate"
        ok "Removed $candidate"
        REMOVED_VENV=true
    fi
done
$REMOVED_VENV || ok "No virtual environment found"

# ── Build directory ────────────────────────────────────────────────────────────
sep
info "Removing C build directory…"
if [[ -d "$REPO_DIR/build" ]]; then
    rm -rf "$REPO_DIR/build"
    ok "Removed $REPO_DIR/build"
else
    ok "No build directory found"
fi

# ── Python egg/dist artefacts ─────────────────────────────────────────────────
sep
info "Removing Python packaging artefacts…"
find "$REPO_DIR" -maxdepth 3 \( \
    -name "*.egg-info" -o \
    -name "*.dist-info" -o \
    -name "dist" -o \
    -name "*.egg" \
\) -exec rm -rf {} + 2>/dev/null || true
ok "Egg / dist artefacts removed"

# ── Compiled Python / C extensions ───────────────────────────────────────────
sep
info "Removing compiled files…"
find "$REPO_DIR" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$REPO_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$REPO_DIR" -name "*.pyo" -delete 2>/dev/null || true
find "$REPO_DIR" -name "*.so"  -delete 2>/dev/null || true
find "$REPO_DIR" -name "*.pyd" -delete 2>/dev/null || true
ok "Compiled files removed"

# ── Launcher wrapper ──────────────────────────────────────────────────────────
sep
info "Removing launcher wrapper…"
if [[ -f "$REPO_DIR/aetherward" ]]; then
    rm -f "$REPO_DIR/aetherward"
    ok "Removed $REPO_DIR/aetherward"
else
    ok "No launcher wrapper found"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
sep
ok "Uninstall complete — repository is back to release state."
echo
