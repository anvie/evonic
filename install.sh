#!/bin/sh
# =============================================================================
# Evonic Platform — Public Install Script
# Served at: https://evonic.dev/install
# Usage:    curl -fsSL https://evonic.dev/install | bash
#
# Installs: Evonic → evonic setup (terminal or browser) → evonic start -d
#
# Environment:
#   EVONIC_HOME   install directory            (default: ~/.evonic)
#   EVONIC_YES=1  unattended: no prompts, `evonic setup --non-interactive`,
#                 then start the platform (the admin password is generated)
#   EVONIC_REF    install this tag or branch instead of the latest release
#                 (e.g. EVONIC_REF=dev)
#   EVONIC_REPO   clone from another URL or path (for testing a fork)
# =============================================================================

set -e

# ── Configuration ────────────────────────────────────────────────────────────
EVONIC_HOME="${EVONIC_HOME:-$HOME/.evonic}"
REPO_URL="${EVONIC_REPO:-https://github.com/anvie/evonic.git}"
VENV_DIR="$EVONIC_HOME/.venv"
BIN_DIR="$EVONIC_HOME/bin"
WRAPPER="$BIN_DIR/evonic"

# ── Terminal styling ─────────────────────────────────────────────────────────
# Colour and the live spinner are used only on a real terminal; piped output
# (CI, logs) stays plain.
bold=""; dim=""; red=""; green=""; yellow=""; accent=""; reset=""
FANCY=""
if [ -t 1 ] && [ -n "$TERM" ] && [ "$TERM" != "dumb" ] && [ -z "$NO_COLOR" ]; then
    FANCY=1
    bold="$(printf '\033[1m')"
    dim="$(printf '\033[2m')"
    red="$(printf '\033[31m')"
    green="$(printf '\033[32m')"
    yellow="$(printf '\033[33m')"
    reset="$(printf '\033[0m')"
    if [ "$(tput colors 2>/dev/null || echo 8)" -ge 256 ] 2>/dev/null; then
        accent="$(printf '\033[38;5;105m')"   # indigo, like the dashboard
    else
        accent="$(printf '\033[36m')"
    fi
fi

# Put the cursor back however the installer ends (Ctrl-C included).
cleanup() {
    if [ -n "$FANCY" ]; then printf '\033[?25h'; fi
    if [ -n "$TASK_LOG" ]; then rm -f "$TASK_LOG"; fi
    return 0
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

spaces() { n=$1; s=""; while [ "$n" -gt 0 ]; do s="$s "; n=$((n - 1)); done; printf '%s' "$s"; }
hline()  { n=$1; s=""; while [ "$n" -gt 0 ]; do s="$s─"; n=$((n - 1)); done; printf '%s' "$s"; }

# $HOME → ~, and keep only the tail of very long paths.
short_path() {
    p="$1"
    case "$p" in "$HOME"*) p="~${p#"$HOME"}" ;; esac
    max="${2:-60}"
    if [ "${#p}" -gt "$max" ]; then
        p="…$(printf '%s' "$p" | awk -v n="$max" '{ print substr($0, length($0) - n + 2) }')"
    fi
    printf '%s' "$p"
}

# ── Output primitives ────────────────────────────────────────────────────────
# One line per finished step: "  ✓ Label       detail".
row_ok()   { printf '  %s✓%s %-13s %s%s%s\n' "$green" "$reset" "$1" "$dim" "$2" "$reset"; }
row_warn() { printf '  %s!%s %-13s %s%s%s\n' "$yellow" "$reset" "$1" "$dim" "$2" "$reset"; }
row_fail() { printf '  %s✗%s %-13s %s\n' "$red" "$reset" "$1" "$2"; }
note()     { printf '    %s%s%s\n' "$dim" "$*" "$reset"; }
heading()  { printf '\n  %s%s%s\n' "$bold" "$*" "$reset"; }

die() {
    printf '\n  %s✗ %s%s\n\n' "$red" "$*" "$reset" >&2
    exit 1
}

banner() {
    printf '%s' "$accent"
    cat << 'EOBANNER'

  ___________                  .__.
  \_   _____/__  ______   ____ |__| ____
   |    __)_\  \/ /    \ /    \|  |/ ___\
   |        \\   (   O  )   |  \  \  \____
  /_______  / \_/ \____/|___|  /__|\___  /
          \/                 \/        \/
EOBANNER
    printf '%s\n' "$reset"
    printf '  %sEvonic Platform Installer%s  %s·  https://evonic.dev%s\n\n' "$bold" "$reset" "$dim" "$reset"
}

# Run a long command with a spinner. Output goes to $TASK_LOG so the terminal
# stays readable; the caller shows it only when the command fails.
#   task "Label" "what is happening" command args...
TASK_LOG=""
task() {
    t_label="$1"; t_msg="$2"; shift 2
    TASK_LOG="${TMPDIR:-/tmp}/evonic-install-$$.log"
    : > "$TASK_LOG"
    "$@" > "$TASK_LOG" 2>&1 &
    t_pid=$!
    if [ -n "$FANCY" ]; then
        printf '\033[?25l'
        t_i=0
        while kill -0 "$t_pid" 2>/dev/null; do
            case $((t_i % 10)) in
                0) f='⠋' ;; 1) f='⠙' ;; 2) f='⠹' ;; 3) f='⠸' ;; 4) f='⠼' ;;
                5) f='⠴' ;; 6) f='⠦' ;; 7) f='⠧' ;; 8) f='⠇' ;; *) f='⠏' ;;
            esac
            printf '\r\033[2K  %s%s%s %-13s %s%s%s' "$accent" "$f" "$reset" "$t_label" "$dim" "$t_msg" "$reset"
            t_i=$((t_i + 1))
            sleep 0.1 2>/dev/null || sleep 1
        done
        printf '\r\033[2K\033[?25h'
    else
        printf '  … %s %s\n' "$t_label" "$t_msg"
    fi
    if wait "$t_pid"; then
        return 0
    fi
    return 1
}

# Show the tail of the failed task's log, then stop.
task_failed() {
    row_fail "$1" "$2"
    if [ -s "$TASK_LOG" ]; then
        printf '\n'
        tail -n 20 "$TASK_LOG" | sed 's/^/    /'
        keep="${TMPDIR:-/tmp}/evonic-install-failed.log"
        cp "$TASK_LOG" "$keep" 2>/dev/null && note "Full log: $keep"
    fi
    die "Installation failed."
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

# Port the platform listens on (env, then .env, default 8080).
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

# ── Step 1: prerequisites ────────────────────────────────────────────────────
check_prereqs() {
    missing=""
    for cmd in git python3; do
        command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
    done
    if [ -n "$missing" ]; then
        row_fail "Prerequisites" "missing:$missing"
        die "Install$missing and re-run."
    fi

    # The codebase uses Python 3.10+ type union syntax (X | Y).
    # Python 3.9 is the absolute minimum supported version.
    pyver=$(python3 --version 2>&1 | awk '{print $2}')
    major=$(echo "$pyver" | cut -d. -f1)
    minor=$(echo "$pyver" | cut -d. -f2)
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 9 ]; }; then
        row_fail "Prerequisites" "Python $pyver"
        die "Python 3.9+ is required."
    fi

    # The installer needs the venv module (with ensurepip), not a system pip3.
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        row_fail "Prerequisites" "Python venv module is missing"
        die "Install it (e.g. sudo apt install python3-venv) and re-run."
    fi
    row_ok "Prerequisites" "git · Python $pyver · venv"
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

# ── Step 2: source code ──────────────────────────────────────────────────────
clone_repo() {
    # The version to install: an explicit EVONIC_REF, else the latest stable tag.
    if [ -n "$EVONIC_REF" ]; then
        LATEST_TAG="$EVONIC_REF"
    else
        task "Release" "looking up the latest version" git ls-remote --tags "$REPO_URL" \
            || task_failed "Release" "could not reach $REPO_URL"
        LATEST_TAG=$(grep -E 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' "$TASK_LOG" \
            | sed 's/.*refs\/tags\///' \
            | sort -t. -k1,1 -k2,2 -k3,3 -V \
            | tail -1)
        if [ -z "$LATEST_TAG" ]; then
            row_warn "Release" "no stable tag found, using main"
            LATEST_TAG="main"
        fi
    fi

    if [ -d "$EVONIC_HOME/.git" ]; then
        task "Source" "updating to $LATEST_TAG" sh -c '
            set -e
            git -C "$1" fetch --tags origin
            if [ "$2" = main ]; then
                git -C "$1" pull --ff-only origin main || git -C "$1" pull origin main
            else
                git -c advice.detachedHead=false -C "$1" checkout "tags/$2" 2>/dev/null \
                    || git -c advice.detachedHead=false -C "$1" checkout "$2"
            fi' sh "$EVONIC_HOME" "$LATEST_TAG" \
            || row_warn "Source" "could not update; continuing with the existing code"
        row_ok "Source" "$LATEST_TAG ($(short_path "$EVONIC_HOME"))"
    else
        if [ -d "$EVONIC_HOME" ]; then
            # Never delete what we did not create: it may hold a .env or a
            # database from an earlier install.
            backup="$EVONIC_HOME.bak-$(date +%Y%m%d-%H%M%S)"
            mv "$EVONIC_HOME" "$backup" || die "Could not move $EVONIC_HOME aside. Move or remove it and re-run."
            row_warn "Existing dir" "not a git repo, moved to $(short_path "$backup")"
        fi
        task "Source" "downloading Evonic $LATEST_TAG" \
            git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$LATEST_TAG" "$REPO_URL" "$EVONIC_HOME" \
            || task_failed "Source" "could not download Evonic $LATEST_TAG"
        row_ok "Source" "$LATEST_TAG ($(short_path "$EVONIC_HOME"))"
    fi
    fix_remote_fetch

    # Ensure we have local tracking branches for main and dev
    # (shallow tag clones leave us in detached HEAD with no local branches)
    for branch in main dev; do
        git -c advice.detachedHead=false -C "$EVONIC_HOME" checkout --quiet "$branch" 2>/dev/null || \
            git -C "$EVONIC_HOME" checkout --quiet -b "$branch" "origin/$branch" 2>/dev/null || true
    done
}

# ── Step 3: virtual environment ──────────────────────────────────────────────
create_venv() {
    # Check whether a usable venv already exists (must have both python and pip)
    if [ -f "$VENV_DIR/bin/python" ] || [ -f "$VENV_DIR/bin/python3" ]; then
        py="$VENV_DIR/bin/python"
        [ -x "$py" ] || py="$VENV_DIR/bin/python3"
        if "$py" -m pip --version >/dev/null 2>&1; then
            row_ok "Environment" "virtualenv already set up"
            return
        fi
        rm -rf "$VENV_DIR"
    fi

    task "Environment" "creating a virtualenv" python3 -m venv "$VENV_DIR" \
        || task_failed "Environment" "virtualenv creation failed (is python3-venv installed?)"

    # Some distros / Python 3.12+ create the venv without bootstrapping pip.
    py="$VENV_DIR/bin/python"
    [ -x "$py" ] || py="$VENV_DIR/bin/python3"
    if ! "$py" -m pip --version >/dev/null 2>&1; then
        task "Environment" "bootstrapping pip" "$py" -m ensurepip --upgrade \
            || task_failed "Environment" "the virtualenv has no pip (install python3-venv)"
    fi
    row_ok "Environment" "virtualenv ready"
}

# ── Step 4: dependencies ─────────────────────────────────────────────────────
install_deps() {
    py="$VENV_DIR/bin/python"
    [ -x "$py" ] || py="$VENV_DIR/bin/python3"

    # pip's own output goes to the task log and is shown only on failure.
    task "Dependencies" "installing packages (the first run takes a minute)" sh -c '
        "$1" -m pip install --disable-pip-version-check --upgrade pip --quiet || true
        "$1" -m pip install --disable-pip-version-check --progress-bar off --quiet -r "$2"
    ' sh "$py" "$EVONIC_HOME/requirements.txt" \
        || task_failed "Dependencies" "could not install the Python packages"
    count=$("$py" -m pip list --format=freeze 2>/dev/null | wc -l | tr -d ' ')
    row_ok "Dependencies" "${count:-?} Python packages"

    # Provision the evomem memory-engine binary (best-effort: unsupported
    # platforms or network issues fall back to FTS5, so never fail the install).
    if task "Memory engine" "downloading evomem" sh -c 'cd "$1" && "$2" -m backend.evomem_provision' sh "$EVONIC_HOME" "$py"; then
        row_ok "Memory engine" "evomem installed"
    else
        row_warn "Memory engine" "evomem not installed (using the FTS5 fallback)"
    fi
}

# ── Step 5: CLI wrapper ──────────────────────────────────────────────────────
create_wrapper() {
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
}

# ── Step 6: PATH ─────────────────────────────────────────────────────────────
prompt_path() {
    # Detect shell and profile file
    shell_name="$(basename "${SHELL:-/bin/sh}")"
    case "$shell_name" in
        zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) profile="$HOME/.bashrc"
              [ ! -f "$profile" ] && profile="$HOME/.bash_profile" ;;
        *)    profile="$HOME/.profile" ;;
    esac

    if echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
        row_ok "Command" "evonic is on your PATH"
        PATH_ADDED=""
        return
    fi

    printf '\n# Added by Evonic installer\nexport PATH="$PATH:%s"\n' "$BIN_DIR" >> "$profile"
    row_ok "Command" "evonic added to PATH in $(short_path "$profile")"
    PATH_ADDED=1
}

# ── Result card ──────────────────────────────────────────────────────────────
CARD_W=58
card_top()    { printf '  %s╭%s╮%s\n' "$dim" "$(hline $((CARD_W + 2)))" "$reset"; }
card_bottom() { printf '  %s╰%s╯%s\n' "$dim" "$(hline $((CARD_W + 2)))" "$reset"; }
card_blank()  { printf '  %s│%s %s %s│%s\n' "$dim" "$reset" "$(spaces "$CARD_W")" "$dim" "$reset"; }
# card_line "plain text" [style]  — text is padded before it is coloured.
card_line() {
    text="$1"; style="$2"
    pad=$((CARD_W - ${#text}))
    [ "$pad" -lt 0 ] && pad=0
    printf '  %s│%s %s%s%s%s %s│%s\n' "$dim" "$reset" "$style" "$text" "$reset" "$(spaces "$pad")" "$dim" "$reset"
}
# card_kv "Label" "value" [value-style]
card_kv() {
    label="$(printf '%-10s' "$1")"
    val="$2"; vstyle="$3"
    room=$((CARD_W - 10))
    pad=$((room - ${#val}))
    [ "$pad" -lt 0 ] && pad=0
    printf '  %s│%s %s%s%s%s%s%s%s %s│%s\n' "$dim" "$reset" "$dim" "$label" "$reset" "$vstyle" "$val" "$reset" "$(spaces "$pad")" "$dim" "$reset"
}

show_result() {
    url="$1"; started="$2"; setup_url="$3"
    pw=""
    [ -f "$EVONIC_HOME/data/initial_admin_password" ] && pw="$(cat "$EVONIC_HOME/data/initial_admin_password" 2>/dev/null)"
    printf '\n'
    card_top
    if [ -n "$started" ]; then
        card_line "✓ Evonic is running" "$bold$green"
    else
        card_line "✓ Evonic is installed" "$bold$green"
    fi
    card_blank
    if [ -n "$setup_url" ]; then
        card_kv "Finish at" "$setup_url" "$accent"
    elif [ -n "$started" ]; then
        card_kv "Open" "$url" "$accent"
    fi
    if [ -n "$pw" ]; then
        card_kv "Password" "$pw" "$bold$accent"
        card_kv "Saved in" "$(short_path "$EVONIC_HOME/data/initial_admin_password" $((CARD_W - 10)))" "$dim"
    fi
    card_blank
    if [ -z "$started" ]; then card_kv "Start" "evonic start -d" "$accent"; fi
    card_kv "Stop" "evonic stop" "$accent"
    card_kv "Change pw" "evonic pass" "$accent"
    card_kv "Help" "evonic --help" "$accent"
    card_bottom
    if [ -n "$PATH_ADDED" ]; then
        printf '\n  %s!%s Open a new terminal, or run %ssource %s%s, to use %sevonic%s.\n' \
            "$yellow" "$reset" "$accent" "$(short_path "$profile")" "$reset" "$bold" "$reset"
    fi
    printf '\n'
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
    banner

    printf '  %sInstall to%s %s%s%s\n' "$dim" "$reset" "$accent" "$(short_path "$EVONIC_HOME")" "$reset"
    ask "  ${bold}?${reset} Continue? ${dim}[Y/n]${reset} "
    case "$REPLY" in
        [nN]|[nN][oO]) die "Installation cancelled." ;;
    esac
    printf '\n'

    check_prereqs
    clone_repo
    create_venv
    install_deps
    create_wrapper
    prompt_path

    # ── Setup ───────────────────────────────────────────────────────────────
    # The super agent has to exist before the dashboard is usable. Do it here
    # in the terminal, or hand over to the browser wizard.
    mode=1
    if [ -z "$EVONIC_YES" ]; then
        heading "Set up Evonic"
        printf '   %s1%s  Terminal wizard   %sa few questions, about a minute%s\n' "$accent" "$reset" "$dim" "$reset"
        printf '   %s2%s  In your browser   %sstarts Evonic and opens the setup page%s\n\n' "$accent" "$reset" "$dim" "$reset"
        ask "  ${bold}›${reset} Choose ${dim}[1/2] (1)${reset}: "
        [ "$REPLY" = "2" ] && mode=2
    fi

    started=""
    setup_url=""
    port="$(evonic_port)"
    url="http://localhost:$port"

    if [ "$mode" = "2" ]; then
        printf '\n'
        if "$WRAPPER" start -d >/dev/null 2>&1; then
            started=1
            setup_url="$url/setup"
            open_url "$setup_url"
        else
            warn_msg="Could not start Evonic. Run 'evonic start -d', then open $url/setup"
            printf '  %s!%s %s\n' "$yellow" "$reset" "$warn_msg"
        fi
    else
        # `set -e` would stop the installer on a failed setup, so capture the code.
        rc=0
        export EVONIC_INSTALLER=1
        printf '\n'
        if [ -n "$EVONIC_YES" ]; then
            "$WRAPPER" setup --non-interactive || rc=$?
        else
            "$WRAPPER" setup || rc=$?
        fi
        if [ "$rc" -eq 0 ]; then
            ask "  ${bold}?${reset} Start Evonic now? ${dim}[Y/n]${reset} "
            case "$REPLY" in
                [nN]|[nN][oO]) ;;
                *)
                    if "$WRAPPER" start -d >/dev/null 2>&1; then
                        started=1
                        [ -z "$EVONIC_YES" ] && open_url "$url"
                    else
                        printf '  %s!%s Could not start Evonic. Run: evonic start -d\n' "$yellow" "$reset"
                    fi ;;
            esac
        else
            printf '\n  %s!%s Setup did not finish. Run %sevonic setup%s to try again.\n' "$yellow" "$reset" "$accent" "$reset"
        fi
    fi

    show_result "$url" "$started" "$setup_url"
}

main
