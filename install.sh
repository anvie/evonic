#!/bin/sh
# =============================================================================
# Evonic Platform — Public Install Script
# Served at: https://evonic.dev/install
# Usage:    curl -fsSL https://evonic.dev/install | bash
#
# Installs: Evonic → evonic setup → evonic start -d
# =============================================================================

set -e

# ── Configuration ────────────────────────────────────────────────────────────
EVONIC_HOME="${EVONIC_HOME:-$HOME/.evonic}"
REPO_URL="https://github.com/anvie/evonic.git"
VENV_DIR="$EVONIC_HOME/.venv"
BIN_DIR="$EVONIC_HOME/bin"
WRAPPER="$BIN_DIR/evonic"

# ── Color helpers ────────────────────────────────────────────────────────────
bold=""; red=""; green=""; yellow=""; blue=""; cyan=""; reset=""
if [ -t 1 ] && [ -n "$TERM" ] && [ "$TERM" != "dumb" ]; then
    bold="$(printf '\033[1m')"
    red="$(printf '\033[31m')"
    green="$(printf '\033[32m')"
    yellow="$(printf '\033[33m')"
    blue="$(printf '\033[34m')"
    cyan="$(printf '\033[36m')"
    reset="$(printf '\033[0m')"
fi

info()    { printf '%s' "$bold$blue"; printf '[INFO]    '; printf '%s' "$reset"; printf '%s\n' "$*"; }
ok()      { printf '%s' "$bold$green"; printf '[OK]      '; printf '%s' "$reset"; printf '%s\n' "$*"; }
warn()    { printf '%s' "$bold$yellow"; printf '[WARN]    '; printf '%s' "$reset"; printf '%s\n' "$*"; }
err()     { printf '%s' "$bold$red"; printf '[ERROR]   '; printf '%s' "$reset"; printf '%s\n' "$*"; }
step()    { printf '\n%s' "$bold$cyan"; printf '▶ %s' "$*"; printf '%s\n\n' "$reset"; }
banner() {
    printf '%s' "$cyan"
    cat << 'EOBANNER'

___________                  .__.
\_   _____/__  ______   ____ |__| ____
 |    __)_\  \/ /    \ /    \|  |/ ___\
 |        \\   (   O  )   |  \  \  \____
/_______  / \_/ \____/|___|  /__|\___  /
        \/                 \/        \/

EOBANNER
    printf '%s' "$reset"
    printf '  %sEvonic Platform Installer%s\n' "$bold" "$reset"
    printf '  %shttps://evonic.dev%s\n\n' "$blue" "$reset"
}

die() {
    err "$*"
    exit 1
}

# ── Step 1: Prerequisite checks ─────────────────────────────────────────────
check_prereqs() {
    step "Step 1/6: Checking prerequisites"

    missing=""
    for cmd in git python3 pip3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ok "$cmd found"
        else
            err "$cmd not found"
            missing="$missing $cmd"
        fi
    done

    if [ -n "$missing" ]; then
        die "Missing prerequisites: $missing. Please install them and re-run."
    fi

    # The codebase uses Python 3.10+ type union syntax (X | Y).
    # Python 3.9 is the absolute minimum supported version.
    pyver=$(python3 --version 2>&1 | awk '{print $2}')
    major=$(echo "$pyver" | cut -d. -f1)
    minor=$(echo "$pyver" | cut -d. -f2)
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 9 ]; }; then
        die "Python 3.9+ is required. Found: $(python3 --version 2>&1)"
    fi
    ok "Python version $(python3 --version 2>&1) meets minimum requirement (3.9+)"
}

# ── Step 2: Clone or update repository ──────────────────────────────────────
clone_repo() {
    step "Step 2/6: Getting Evonic source code"

    # Determine the latest stable tagged release
    info "Determining latest stable release..."
    LATEST_TAG=$(git ls-remote --tags "$REPO_URL" 2>/dev/null \
        | grep -E 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sed 's/.*refs\/tags\///' \
        | sort -t. -k1,1 -k2,2 -k3,3 -V \
        | tail -1)

    if [ -z "$LATEST_TAG" ]; then
        warn "No stable release tags found; falling back to main branch"
        LATEST_TAG="main"
    else
        ok "Latest stable release: $LATEST_TAG"
    fi

    if [ -d "$EVONIC_HOME/.git" ]; then
        info "Repository exists — updating to $LATEST_TAG..."
        git -C "$EVONIC_HOME" fetch --tags origin 2>/dev/null
        if [ "$LATEST_TAG" != "main" ]; then
            git -C "$EVONIC_HOME" checkout "tags/$LATEST_TAG" 2>/dev/null || \
                git -C "$EVONIC_HOME" checkout "$LATEST_TAG" 2>/dev/null || \
                warn "Could not checkout $LATEST_TAG; continuing with existing code."
        else
            git -C "$EVONIC_HOME" pull --ff-only origin main 2>/dev/null || \
                git -C "$EVONIC_HOME" pull origin main 2>/dev/null || \
                warn "Could not pull; continuing with existing code."
        fi
        ok "Repository updated"
    elif [ -d "$EVONIC_HOME" ]; then
        warn "$EVONIC_HOME exists but is not a git repo. Removing and re-cloning..."
        rm -rf "$EVONIC_HOME"
        if [ "$LATEST_TAG" != "main" ]; then
            git clone --depth 1 --branch "$LATEST_TAG" "$REPO_URL" "$EVONIC_HOME"
        else
            git clone --depth 1 "$REPO_URL" "$EVONIC_HOME"
        fi
        ok "Repository cloned"
    else
        if [ "$LATEST_TAG" != "main" ]; then
            git clone --depth 1 --branch "$LATEST_TAG" "$REPO_URL" "$EVONIC_HOME"
        else
            git clone --depth 1 "$REPO_URL" "$EVONIC_HOME"
        fi
        ok "Repository cloned"
    fi

    # Ensure we're on the main branch so users can git pull manually
    git -C "$EVONIC_HOME" checkout main 2>/dev/null || \
        git -C "$EVONIC_HOME" checkout -b main origin/main 2>/dev/null || \
        warn "Could not switch to main branch."
}
# ── Step 3: Create Python virtual environment ────────────────────────────────
create_venv() {
    step "Step 3/6: Creating Python virtual environment"

    if [ -f "$VENV_DIR/bin/python" ] || [ -f "$VENV_DIR/bin/python3" ]; then
        ok "Virtual environment already exists — skipping"
        return
    fi

    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created at $VENV_DIR"
}

# ── Step 4: Install Python dependencies ─────────────────────────────────────
install_deps() {
    step "Step 4/6: Installing Python dependencies"

    pip="$VENV_DIR/bin/pip"
    if [ ! -f "$pip" ]; then
        pip="$VENV_DIR/bin/pip3"
    fi

    "$pip" install --upgrade pip --quiet
    "$pip" install -r "$EVONIC_HOME/requirements.txt"
    ok "Dependencies installed"
}

# ── Step 5: Create CLI wrapper script ────────────────────────────────────────
create_wrapper() {
    step "Step 5/6: Creating evonic CLI wrapper"

    mkdir -p "$BIN_DIR"

    cat > "$WRAPPER" << EOF
#!/bin/sh
# ── evonic CLI wrapper — auto-generated by install.sh ──
EVONIC_HOME="\${EVONIC_HOME:-$EVONIC_HOME}"

# Activate venv and run
if [ -f "\$EVONIC_HOME/.venv/bin/activate" ]; then
    . "\$EVONIC_HOME/.venv/bin/activate"
fi

cd "\$EVONIC_HOME"
exec python3 -m cli "\$@"
EOF

    chmod +x "$WRAPPER"
    ok "Wrapper script created at $WRAPPER"
}

# ── Step 6: PATH prompt ─────────────────────────────────────────────────────
prompt_path() {
    step "Step 6/6: Adding evonic to your PATH"

    # Detect shell and profile file
    shell_name="$(basename "${SHELL:-/bin/sh}")"
    case "$shell_name" in
        zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) profile="$HOME/.bashrc"
              [ ! -f "$profile" ] && profile="$HOME/.bash_profile" ;;
        *)    profile="$HOME/.profile" ;;
    esac

    if echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
        ok "evonic is already in your PATH"
        return
    fi

    info "Adding evonic to your PATH in $profile"
    printf '\n# Added by Evonic installer\nexport PATH="$PATH:%s"\n' "$BIN_DIR" >> "$profile"
    ok "PATH updated. Restart your shell or run: source $profile"
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    banner

    # Quick confirmation — force prompt even when piped via curl | bash
    printf '%sThis will install Evonic to: %s%s\n' "$bold" "$reset" "$EVONIC_HOME"
    printf '%sContinue? [Y/n]%s ' "$bold" "$reset"
    if [ -t 0 ]; then
        read -r reply
    else
        read -r reply < /dev/tty
    fi

    case "$reply" in
        [nN]|[nN][oO]) die "Installation cancelled." ;;
        *) info "Starting installation..." ;;
    esac

    check_prereqs
    clone_repo
    create_venv
    install_deps
    create_wrapper
    prompt_path

    # ── Done ────────────────────────────────────────────────────────────────
    printf '\n%s' "$bold$green"
    cat << 'EODONE'
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║                         ✅  Evonic installed!                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
EODONE
    printf '%s' "$reset"

    if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
        printf '%s  ⚡  %sApply PATH now:  %ssource %s%s\n'             "$bold$yellow" "$reset" "$bold" "$profile" "$reset"
    else
        printf '%s  ⚡  %sReady to use:%s\n' "$bold$green" "$reset" "$reset"
    fi

    printf '\n%s  ──  Running evonic setup...%s\n\n' "$bold" "$reset"
    "$WRAPPER" setup

    printf '\n%s  ──  Next step:%s\n' "$bold" "$reset"
    if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
        printf '%s     %sFirst, add evonic to your PATH:%s\n' "$bold" "$yellow" "$reset"
        printf '%s     %ssource %s%s\n' "$bold" "$cyan" "$profile" "$reset"
        printf '%s     %s(or restart your terminal)%s\n\n' "$bold" "$blue" "$reset"
    fi
    printf '%s     %sevonic start -d    %s%s# start the platform as a daemon%s\n'         "$bold" "$cyan" "$reset" "$blue" "$reset"
    printf '\n'
}

main
