#!/bin/sh
# =============================================================================
# Evonic Platform — Public Install Script
# Served at: https://evonic.dev/install
# Usage:    curl -fsSL https://evonic.dev/install | bash
#
# Installs: Evonic → evonic setup (terminal or browser) → evonic start -d
#
# Unattended: EVONIC_YES=1 skips every prompt, runs `evonic setup
# --non-interactive` and starts the platform (the admin password is generated).
# =============================================================================

set -e

# ── Configuration ────────────────────────────────────────────────────────────
EVONIC_HOME="${EVONIC_HOME:-$HOME/.evonic}"
REPO_URL="https://github.com/anvie/evonic.git"
VENV_DIR="$EVONIC_HOME/.venv"
BIN_DIR="$EVONIC_HOME/bin"
WRAPPER="$BIN_DIR/evonic"

# ── Color helpers ────────────────────────────────────────────────────────────
bold=""; dim=""; red=""; green=""; yellow=""; blue=""; cyan=""; reset=""
if [ -t 1 ] && [ -n "$TERM" ] && [ "$TERM" != "dumb" ]; then
    bold="$(printf '\033[1m')"
    dim="$(printf '\033[2m')"
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

# Ask a question and put the answer in $REPLY. Works under `curl | bash` (stdin
# is the script) by reading from the terminal; with no terminal, or with
# EVONIC_YES=1, the answer is empty so the caller's default applies.
ask() {
    REPLY=""
    [ -n "$EVONIC_YES" ] && return 0
    printf '%s' "$1"
    if [ -t 0 ]; then
        read -r REPLY || REPLY=""
    elif [ -r /dev/tty ]; then
        read -r REPLY < /dev/tty || REPLY=""
    fi
}

# Port the platform listens on (from .env, default 8080).
evonic_port() {
    [ -n "$PORT" ] && { printf '%s' "$PORT"; return; }
    p=$(sed -n 's/^PORT=//p' "$EVONIC_HOME/.env" 2>/dev/null | tail -1 | tr -d '"' | tr -d "'")
    printf '%s' "${p:-8080}"
}

open_url() {
    if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
    fi
}

# ── Step 1: Prerequisite checks ─────────────────────────────────────────────
check_prereqs() {
    step "Step 1/6: Checking prerequisites"

    missing=""
    for cmd in git python3; do
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

    # The installer needs the venv module (with ensurepip), not a system pip3.
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        die "Python 'venv' module is missing. Install it (e.g. sudo apt install python3-venv) and re-run."
    fi
    ok "Python venv module available"
}

# --- Fix remote fetch config so branches and tags are tracked ---
# Shallow clones (--depth 1 --branch TAG) lock the remote to a single
# tag refspec (fetch = +refs/tags/X:refs/tags/X).  This permanently
# breaks `git fetch`, `git pull`, and the supervisor auto-updater.
# Reconfigure to fetch all branches + tags so the repo stays alive.
fix_remote_fetch() {
    rc=$(git -C "$EVONIC_HOME" config remote.origin.fetch 2>/dev/null)
    case "$rc" in
        *refs/tags/*:refs/tags/*)  # locked to a single tag -- fix it
            git -C "$EVONIC_HOME" config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
            git -C "$EVONIC_HOME" config --add remote.origin.fetch "+refs/tags/*:refs/tags/*"
            git -C "$EVONIC_HOME" fetch origin 2>/dev/null || true ;;
    esac
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
        fix_remote_fetch
        git -C "$EVONIC_HOME" fetch --tags origin 2>/dev/null
        if [ "$LATEST_TAG" != "main" ]; then
            git -c advice.detachedHead=false -C "$EVONIC_HOME" checkout "tags/$LATEST_TAG" 2>/dev/null || \
                git -c advice.detachedHead=false -C "$EVONIC_HOME" checkout "$LATEST_TAG" 2>/dev/null || \
                warn "Could not checkout $LATEST_TAG; continuing with existing code."
        else
            git -C "$EVONIC_HOME" pull --ff-only origin main 2>/dev/null || \
                git -C "$EVONIC_HOME" pull origin main 2>/dev/null || \
                warn "Could not pull; continuing with existing code."
        fi
        ok "Repository updated"
    else
        if [ -d "$EVONIC_HOME" ]; then
            # Never delete what we did not create: it may hold a .env or a
            # database from an earlier install.
            backup="$EVONIC_HOME.bak-$(date +%Y%m%d-%H%M%S)"
            warn "$EVONIC_HOME exists but is not a git repo."
            mv "$EVONIC_HOME" "$backup" || die "Could not move $EVONIC_HOME aside. Move or remove it and re-run."
            info "Moved it to $backup (nothing was deleted)"
        fi
        info "Downloading Evonic $LATEST_TAG..."
        if [ "$LATEST_TAG" != "main" ]; then
            git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$LATEST_TAG" "$REPO_URL" "$EVONIC_HOME" \
                || die "Could not download Evonic. Check your network connection and re-run."
        else
            git clone --quiet --depth 1 "$REPO_URL" "$EVONIC_HOME" \
                || die "Could not download Evonic. Check your network connection and re-run."
        fi
        ok "Repository cloned"
    fi
    fix_remote_fetch

    # Ensure we have local tracking branches for main and dev
    # (shallow tag clones leave us in detached HEAD with no local branches)
    for branch in main dev; do
        git -c advice.detachedHead=false -C "$EVONIC_HOME" checkout --quiet "$branch" 2>/dev/null || \
            git -C "$EVONIC_HOME" checkout --quiet -b "$branch" "origin/$branch" 2>/dev/null || \
            warn "Could not create local $branch branch."
    done
}
# ── Step 3: Create Python virtual environment ────────────────────────────────
create_venv() {
    step "Step 3/6: Creating Python virtual environment"

    # Check whether a usable venv already exists (must have both python and pip)
    if [ -f "$VENV_DIR/bin/python" ] || [ -f "$VENV_DIR/bin/python3" ]; then
        py="$VENV_DIR/bin/python"
        [ -x "$py" ] || py="$VENV_DIR/bin/python3"
        if "$py" -m pip --version >/dev/null 2>&1; then
            ok "Virtual environment already exists — skipping"
            return
        fi
        warn "Virtual environment exists but pip is missing — recreating..."
        rm -rf "$VENV_DIR"
    fi

    python3 -m venv "$VENV_DIR" || die "Virtual environment creation failed. Install the venv module (e.g. sudo apt install python3-venv) and re-run."

    # Some distros / Python 3.12+ create the venv without bootstrapping pip.
    py="$VENV_DIR/bin/python"
    [ -x "$py" ] || py="$VENV_DIR/bin/python3"

    if ! "$py" -m pip --version >/dev/null 2>&1; then
        "$py" -m ensurepip --upgrade >/dev/null 2>&1 || \
            die "Virtual environment has no pip. Install the venv module (e.g. sudo apt install python3-venv) and re-run."
    fi
    ok "Virtual environment created at $VENV_DIR"
}

# ── Step 4: Install Python dependencies ─────────────────────────────────────
install_deps() {
    step "Step 4/6: Installing Python dependencies"

    py="$VENV_DIR/bin/python"
    [ -x "$py" ] || py="$VENV_DIR/bin/python3"

    # Keep the terminal readable: pip's output goes to a log that is only shown
    # when the install fails.
    pip_log="${TMPDIR:-/tmp}/evonic-pip-$$.log"
    info "Installing packages (the first run can take a minute)..."
    "$py" -m pip install --disable-pip-version-check --upgrade pip --quiet >"$pip_log" 2>&1 || true
    if ! "$py" -m pip install --disable-pip-version-check --progress-bar off --quiet \
            -r "$EVONIC_HOME/requirements.txt" >>"$pip_log" 2>&1; then
        err "Installing dependencies failed. Last lines of the log:"
        tail -n 25 "$pip_log"
        die "Full log: $pip_log"
    fi
    rm -f "$pip_log"
    count=$("$py" -m pip list --format=freeze 2>/dev/null | wc -l | tr -d ' ')
    ok "Dependencies installed${count:+ ($count packages)}"

    # Provision the evomem memory-engine binary (best-effort: unsupported
    # platforms or network issues fall back to FTS5, so never fail the install).
    if (cd "$EVONIC_HOME" && "$py" -m backend.evomem_provision) >/dev/null 2>&1; then
        ok "Evomem memory engine installed"
    else
        warn "Evomem binary not installed (unsupported platform or network issue); using FTS5 fallback"
    fi
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
elif [ -f "\$EVONIC_HOME/venv/bin/activate" ]; then
    . "\$EVONIC_HOME/venv/bin/activate"
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

    printf '%sThis will install Evonic to: %s%s%s\n' "$bold" "$reset" "$cyan$EVONIC_HOME" "$reset"
    ask "${bold}Continue? [Y/n]${reset} "
    case "$REPLY" in
        [nN]|[nN][oO]) die "Installation cancelled." ;;
        *) info "Starting installation..." ;;
    esac

    check_prereqs
    clone_repo
    create_venv
    install_deps
    create_wrapper
    prompt_path

    # ── Installed ───────────────────────────────────────────────────────────
    printf '\n%s' "$bold$green"
    cat << 'EODONE'
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║                         ✅  Evonic installed!                                ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
EODONE
    printf '%s' "$reset"
    printf '  %sVersion %s · %s%s\n' "$dim" "$LATEST_TAG" "$EVONIC_HOME" "$reset"

    # ── Setup ───────────────────────────────────────────────────────────────
    # The super agent has to exist before the dashboard is usable. Do it here
    # in the terminal, or hand over to the browser wizard.
    mode=1
    if [ -z "$EVONIC_YES" ]; then
        printf '\n%s  ──  Set up Evonic%s\n' "$bold" "$reset"
        printf '      %s1)%s Terminal wizard  %s(a few questions, ~1 minute)%s\n' "$bold$cyan" "$reset" "$dim" "$reset"
        printf '      %s2)%s In your browser  %s(starts Evonic and opens the setup page)%s\n' "$bold$cyan" "$reset" "$dim" "$reset"
        ask "${bold}Choose [1/2]${reset} (default 1): "
        [ "$REPLY" = "2" ] && mode=2
    fi

    started=""
    if [ "$mode" = "2" ]; then
        "$WRAPPER" start -d && started=1
        url="http://localhost:$(evonic_port)/setup"
        printf '\n  %sFinish setup at:%s %s%s%s\n' "$bold" "$reset" "$cyan" "$url" "$reset"
        [ -n "$started" ] && open_url "$url"
    else
        # `set -e` would stop the installer on a failed setup, so capture the code.
        rc=0
        if [ -n "$EVONIC_YES" ]; then
            "$WRAPPER" setup --non-interactive || rc=$?
        else
            "$WRAPPER" setup || rc=$?
        fi
        if [ "$rc" -eq 0 ]; then
            ask "
${bold}Start Evonic now? [Y/n]${reset} "
            case "$REPLY" in
                [nN]|[nN][oO]) ;;
                *) "$WRAPPER" start -d && started=1 ;;
            esac
            if [ -n "$started" ]; then
                url="http://localhost:$(evonic_port)"
                printf '\n  %sEvonic is running at%s %s%s%s\n' "$bold$green" "$reset" "$cyan" "$url" "$reset"
                printf '  %sSign in with the admin password shown above.%s\n' "$dim" "$reset"
                [ -z "$EVONIC_YES" ] && open_url "$url"
            fi
        else
            warn "Setup did not finish. Run 'evonic setup' to try again."
        fi
    fi

    # ── Next steps ──────────────────────────────────────────────────────────
    printf '\n%s  ──  Next steps%s\n' "$bold" "$reset"
    if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
        printf '     %s1.%s Add evonic to your PATH: %ssource %s%s %s(or restart your terminal)%s\n' \
            "$bold$yellow" "$reset" "$cyan" "$profile" "$reset" "$dim" "$reset"
    fi
    if [ -z "$started" ]; then
        printf '     %sevonic start -d%s     %s# start the platform as a daemon%s\n' "$cyan" "$reset" "$dim" "$reset"
    fi
    printf '     %sevonic pass%s         %s# change the admin password%s\n' "$cyan" "$reset" "$dim" "$reset"
    printf '     %sevonic --help%s       %s# all commands%s\n\n' "$cyan" "$reset" "$dim" "$reset"
}

main
