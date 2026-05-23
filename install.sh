#!/usr/bin/env bash
# AetherWard installer
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[1;31m'; GRN='\033[1;32m'; YEL='\033[1;33m'
CYN='\033[1;36m'; DIM='\033[2m'; RST='\033[0m'

info()  { echo -e "${CYN}  ▸${RST} $*"; }
ok()    { echo -e "${GRN}  ✔${RST} $*"; }
warn()  { echo -e "${YEL}  ⚠${RST} $*"; }
err()   { echo -e "${RED}  ✘${RST} $*" >&2; }
sep()   { echo -e "\n${DIM}────────────────────────────────────────${RST}"; }
ask()   {   # ask <varname> <prompt> <default>
    local __var="$1" __prompt="$2" __def="$3"
    echo -en "  ${CYN}?${RST} ${__prompt} ${DIM}[${__def}]${RST}: "
    read -r __input || true
    printf -v "$__var" '%s' "${__input:-$__def}"
}
yn() {  # yn <prompt> <default Y|N> → returns 0=yes 1=no
    local __prompt="$1" __def="${2:-Y}"
    local __hint; [[ "$__def" == Y ]] && __hint="Y/n" || __hint="y/N"
    echo -en "  ${CYN}?${RST} ${__prompt} ${DIM}[${__hint}]${RST}: "
    read -r __ans || true
    __ans="${__ans:-$__def}"
    [[ "${__ans,,}" == y* ]]
}

# ── Graceful error trap ───────────────────────────────────────────────────────
trap 'echo; err "Installation failed on line $LINENO — see output above."; exit 1' ERR

# ── Banner ────────────────────────────────────────────────────────────────────
echo
[[ -f "$REPO_DIR/banner.txt" ]] && cat "$REPO_DIR/banner.txt" || echo -e "${CYN}AetherWard${RST}"
echo -e "  ${DIM}AetherWard installer${RST}"
sep

# ── Privilege escalation detection ───────────────────────────────────────────
PRIV_CMD=""
if [[ "$EUID" -eq 0 ]]; then
    PRIV_CMD="direct"
    ok "Running as root"
elif command -v sudo &>/dev/null; then
    PRIV_CMD="sudo"
    ok "Privilege escalation: sudo"
elif command -v doas &>/dev/null; then
    PRIV_CMD="doas"
    ok "Privilege escalation: doas"
else
    warn "No sudo or doas found — system-level operations unavailable."
    warn "Re-run as root if cmake install or system-wide pip install is needed."
fi

run_priv() {
    case "$PRIV_CMD" in
        direct) "$@" ;;
        sudo)   sudo "$@" ;;
        doas)   doas "$@" ;;
        *)
            err "Root privileges required but no sudo/doas available."
            err "Re-run as root:  su -c 'bash ${BASH_SOURCE[0]}'"
            exit 1
            ;;
    esac
}

# ── Python check ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "python3 not found. Install Python 3.11+ and retry."
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(cut -d. -f1 <<<"$PY_VER")
PY_MINOR=$(cut -d. -f2 <<<"$PY_VER")
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 11 ]]; }; then
    err "Python 3.11+ required (found $PY_VER)."
    exit 1
fi
ok "Python $PY_VER"

# ── Questions — C core ────────────────────────────────────────────────────────
sep
echo -e "  ${CYN}C core${RST}"
echo -e "  ${DIM}Builds the optional native TDOA / DSP extension (requires cmake + a C compiler).${RST}"
echo -e "  ${DIM}Python-only mode still works without it.${RST}"
BUILD_C=false
NEED_CMAKE=false
if yn "Build C core?" N; then
    BUILD_C=true
    if ! command -v cmake &>/dev/null; then
        if [[ -z "$PRIV_CMD" ]]; then
            err "cmake not found and no privilege escalation available to install it."
            err "Install cmake manually (apt/dnf/pacman) then re-run, or skip C core."
            exit 1
        fi
        warn "cmake not found — will install via system package manager."
        NEED_CMAKE=true
    else
        ok "cmake $(cmake --version | head -1 | awk '{print $3}')"
    fi
fi

# ── Optional C core features (only shown when building C core) ────────────────
INSTALL_PCAP=false
INSTALL_PPS=false
if $BUILD_C; then
    sep
    echo -e "  ${CYN}Optional C core features${RST}"
    echo -e "  ${DIM}cmake + build-essential + pkg-config are required and will be installed.${RST}"
    echo -e "  ${DIM}The following extend native capture capabilities:${RST}"
    yn "Enable libpcap native capture? (libpcap-dev)" N && INSTALL_PCAP=true || true
    yn "Enable PPS hardware sync? (pps-tools)"        N && INSTALL_PPS=true  || true
fi

# ── Questions — Python dependencies ───────────────────────────────────────────
sep
echo -e "  ${CYN}Python dependencies${RST}"
echo -e "  ${DIM}numpy is always installed. Select optional extras:${RST}"

INSTALL_GPS=false;  INSTALL_YAML=false; INSTALL_SDR=false
INSTALL_WIFI=false; INSTALL_DEV=false

yn "GPS support (gpsd-py3)?"               N && INSTALL_GPS=true  || true
yn "YAML config support (pyyaml)?"         Y && INSTALL_YAML=true || true
yn "RTL-SDR support (pyrtlsdr)?"           N && INSTALL_SDR=true  || true
yn "WiFi capture support (scapy)?"         N && INSTALL_WIFI=true || true
yn "Developer tools (pytest, mypy, ruff)?" N && INSTALL_DEV=true  || true

# ── Questions — install target ────────────────────────────────────────────────
sep
echo -e "  ${CYN}Installation target${RST}"
echo    "    1) Virtual environment  — isolated, recommended"
echo    "    2) User install         — ~/.local  (pip install --user)"
echo    "    3) System install       — /usr/local (requires root)"
echo
echo -en "  ${CYN}?${RST} Choice ${DIM}[1]${RST}: "
read -r INST_CHOICE || true
INST_CHOICE="${INST_CHOICE:-1}"

VENV_DIR=""
case "$INST_CHOICE" in
    1)
        ask VENV_DIR "Virtual environment path" "$REPO_DIR/.venv"
        INSTALL_TARGET="venv"
        ;;
    2)
        INSTALL_TARGET="user"
        ;;
    3)
        INSTALL_TARGET="system"
        if [[ -z "$PRIV_CMD" ]]; then
            err "System install requires root but no sudo/doas is available."
            err "Re-run as root, or choose option 1 (venv) or 2 (user)."
            exit 1
        fi
        ;;
    *)
        warn "Invalid choice — defaulting to virtual environment."
        ask VENV_DIR "Virtual environment path" "$REPO_DIR/.venv"
        INSTALL_TARGET="venv"
        ;;
esac

# ── Summary ───────────────────────────────────────────────────────────────────
sep
echo -e "  ${CYN}Summary${RST}"
echo    "    Build C core    : $BUILD_C"
$BUILD_C && echo "    libpcap capture : $INSTALL_PCAP"
$BUILD_C && echo "    PPS sync        : $INSTALL_PPS"
echo    "    GPS support     : $INSTALL_GPS"
echo    "    YAML support    : $INSTALL_YAML"
echo    "    RTL-SDR support : $INSTALL_SDR"
echo    "    WiFi / scapy    : $INSTALL_WIFI"
echo    "    Dev tools       : $INSTALL_DEV"
echo    "    Install target  : $INSTALL_TARGET${VENV_DIR:+ ($VENV_DIR)}"
sep
yn "Proceed with installation?" Y || { echo "  Aborted."; exit 0; }

# ── System packages (cmake) ───────────────────────────────────────────────────
if $BUILD_C; then
    # Always install pkg-config + optional native deps; cmake only if missing
    APT_PKGS=(build-essential pkg-config)
    DNF_PKGS=(gcc pkgconf-pkg-config)
    PAC_PKGS=(gcc pkgconf)
    $NEED_CMAKE   && APT_PKGS+=(cmake)          && DNF_PKGS+=(cmake)           && PAC_PKGS+=(cmake)
    $INSTALL_PCAP && APT_PKGS+=(libpcap-dev)    && DNF_PKGS+=(libpcap-devel)   && PAC_PKGS+=(libpcap)
    $INSTALL_PPS  && APT_PKGS+=(pps-tools)      && DNF_PKGS+=(pps-tools)       && PAC_PKGS+=(pps-tools)

    info "Installing C build dependencies…"
    if command -v apt-get &>/dev/null; then
        run_priv apt-get install -y "${APT_PKGS[@]}"
    elif command -v dnf &>/dev/null; then
        run_priv dnf install -y "${DNF_PKGS[@]}"
    elif command -v pacman &>/dev/null; then
        run_priv pacman -S --noconfirm "${PAC_PKGS[@]}"
    else
        err "No known package manager found. Install cmake manually and re-run."
        exit 1
    fi
fi

# ── Build C core ──────────────────────────────────────────────────────────────
if $BUILD_C; then
    sep
    info "Building C core…"
    BUILD_DIR="$REPO_DIR/build"
    # Clean stale build dir if not writable (e.g. left from a previous root build)
    if [[ -d "$BUILD_DIR" ]] && [[ ! -w "$BUILD_DIR" ]]; then
        info "Clearing stale build directory (owned by another user)…"
        run_priv rm -rf "$BUILD_DIR"
    fi
    mkdir -p "$BUILD_DIR"
    cmake -S "$REPO_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$BUILD_DIR" --parallel "$(nproc)"
    ok "C core built in $BUILD_DIR"
fi

# ── Install Python packages ────────────────────────────────────────────────────
sep
info "Installing Python packages…"

if [[ "$INSTALL_TARGET" == venv ]]; then
    # ── Venv: fully isolated — pip handles everything ─────────────────────────
    if [[ ! -d "$VENV_DIR" ]]; then
        python3 -m venv "$VENV_DIR"
        ok "Virtual environment created at $VENV_DIR"
    else
        ok "Using existing virtual environment at $VENV_DIR"
    fi
    PIP="$VENV_DIR/bin/pip"
    $PIP install --upgrade pip --quiet || warn "pip upgrade failed"

    EXTRAS=()
    $INSTALL_GPS  && EXTRAS+=("gps")
    $INSTALL_YAML && EXTRAS+=("yaml")
    $INSTALL_SDR  && EXTRAS+=("sdr")
    $INSTALL_DEV  && EXTRAS+=("dev")
    [[ ${#EXTRAS[@]} -gt 0 ]] \
        && PKG="${REPO_DIR}[$(IFS=,; echo "${EXTRAS[*]}")]" \
        || PKG="$REPO_DIR"
    $INSTALL_WIFI && { info "Installing scapy…"; $PIP install -q "scapy>=2.5"; }
    info "Installing AetherWard…"
    $PIP install -q -e "$PKG"

else
    # ── User / system: try pip first, fall back to apt on PEP 668 / Debian ───
    # Packages available in both pip and apt
    PIP_DEPS=(numpy)
    APT_DEPS=(python3-numpy)
    $INSTALL_GPS  && PIP_DEPS+=("gpsd-py3")            && APT_DEPS+=(python3-gps)
    $INSTALL_YAML && PIP_DEPS+=("pyyaml")              && APT_DEPS+=(python3-yaml)
    $INSTALL_WIFI && PIP_DEPS+=("scapy>=2.5")          && APT_DEPS+=(python3-scapy)
    $INSTALL_DEV  && PIP_DEPS+=("pytest" "pytest-cov" "mypy") \
                  && APT_DEPS+=(python3-pytest python3-pytest-cov python3-mypy)
    # These have no apt equivalent — pip only (with --break-system-packages fallback)
    PIP_ONLY=()
    $INSTALL_SDR && PIP_ONLY+=("pyrtlsdr")
    $INSTALL_DEV && PIP_ONLY+=("ruff")

    PIP_ARGS=(); [[ "$INSTALL_TARGET" == user ]] && PIP_ARGS=(--user)

    info "Installing Python dependencies…"
    if python3 -m pip install -q "${PIP_ARGS[@]}" "${PIP_DEPS[@]}" 2>/dev/null; then
        ok "Dependencies installed via pip"
    elif command -v apt-get &>/dev/null; then
        info "pip blocked — falling back to apt…"
        run_priv apt-get install -y "${APT_DEPS[@]}" \
            || { warn "Some apt packages not found — continuing"; true; }
        ok "apt packages installed"
    else
        warn "pip failed and no apt — retrying with --break-system-packages…"
        run_priv python3 -m pip install --break-system-packages -q \
            "${PIP_ARGS[@]}" "${PIP_DEPS[@]}" || true
    fi

    # pip-only packages (not in apt): need --break-system-packages on managed envs
    if [[ ${#PIP_ONLY[@]} -gt 0 ]]; then
        info "Installing via pip (not in apt): ${PIP_ONLY[*]}"
        case "$INSTALL_TARGET" in
            user)   python3 -m pip install --break-system-packages -q --user "${PIP_ONLY[@]}" \
                        || warn "Some pip-only packages failed" ;;
            system) run_priv python3 -m pip install --break-system-packages -q "${PIP_ONLY[@]}" \
                        || warn "Some pip-only packages failed" ;;
        esac
    fi

    # Remove stale egg-info that may be owned by a different user (e.g. previous root install)
    for _ei in "$REPO_DIR"/*.egg-info "$REPO_DIR"/src/*.egg-info; do
        [[ -e "$_ei" ]] || continue
        if [[ ! -w "$_ei" ]]; then
            info "Clearing stale egg-info owned by another user…"
            run_priv rm -rf "$_ei"
        fi
    done

    # Install aetherward itself (not in apt) — deps already satisfied above
    info "Installing AetherWard…"
    case "$INSTALL_TARGET" in
        user)   python3 -m pip install --break-system-packages --no-deps -q --user -e "$REPO_DIR" ;;
        system) run_priv python3 -m pip install --break-system-packages --no-deps -q -e "$REPO_DIR" ;;
    esac
fi

ok "AetherWard installed"

# ── Create aetherward launcher (venv only) ────────────────────────────────────
if [[ "$INSTALL_TARGET" == venv ]]; then
    mkdir -p "$REPO_DIR/bin"
    LAUNCHER="$REPO_DIR/bin/aetherward"
    cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/aetherward" "\$@"
LAUNCHER_EOF
    chmod +x "$LAUNCHER"
    ok "Launcher created: $LAUNCHER"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
sep
ok "AetherWard installed successfully."
echo
case "$INSTALL_TARGET" in
    venv)
        echo -e "  Run:  ${CYN}source ${VENV_DIR}/bin/activate${RST}  then  ${CYN}aetherward${RST}"
        echo -e "  Or:   ${CYN}${REPO_DIR}/bin/aetherward${RST}  (no activation needed)"
        ;;
    user)
        echo -e "  Run:  ${CYN}aetherward${RST}"
        echo -e "  ${DIM}(ensure ~/.local/bin is in your PATH)${RST}"
        # Offer to add ~/.local/bin to PATH if not already there
        if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
            echo
            if yn "Add ~/.local/bin to PATH in your shell rc?" Y; then
                SHELL_RC=""
                if [[ -n "${BASH_VERSION:-}" ]] || [[ "${SHELL##*/}" == bash ]]; then
                    SHELL_RC="$HOME/.bashrc"
                elif [[ -n "${ZSH_VERSION:-}" ]] || [[ "${SHELL##*/}" == zsh ]]; then
                    SHELL_RC="$HOME/.zshrc"
                fi
                if [[ -n "$SHELL_RC" ]]; then
                    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$SHELL_RC"
                    ok "Added to $SHELL_RC — restart your shell or run: source $SHELL_RC"
                else
                    warn "Unknown shell — add manually: export PATH=\"\$HOME/.local/bin:\$PATH\""
                fi
            fi
        fi
        ;;
    system)
        echo -e "  Run:  ${CYN}aetherward${RST}"
        ;;
esac
echo
