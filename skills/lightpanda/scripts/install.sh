#!/usr/bin/env bash
# Install the local `lightpanda` headless-browser binary this skill depends on.
#
#   bash skills/lightpanda/scripts/install.sh            # idempotent install
#   LIGHTPANDA_VERSION=nightly bash .../install.sh       # pick a release tag
#   LIGHTPANDA_INSTALL_DIR=/opt/bin bash .../install.sh  # force target dir
#   bash .../install.sh --force                          # reinstall
#
# Used by `evonic doctor --fix` (declared as requirements.binaries[].fix_script)
# and by the skill's own doctor hook. Downloads the single-file release asset
# from github.com/lightpanda-io/browser, verifies it actually executes on this
# host, then installs it atomically. No sudo, no Docker, no package manager.
set -euo pipefail

REPO="${LIGHTPANDA_RELEASE_REPO:-lightpanda-io/browser}"
MIN_VERSION="${LIGHTPANDA_MIN_VERSION:-1.0.0}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "[install_lightpanda] $*"; }
die() { log "ERROR: $*"; exit 1; }

# ── existing install ────────────────────────────────────────────────────────
existing=""
if command -v lightpanda >/dev/null 2>&1; then
  existing="$(command -v lightpanda)"
elif [ -x "$(dirname "$0")/../bin/lightpanda" ]; then
  existing="$(cd "$(dirname "$0")/.." && pwd)/bin/lightpanda"
fi

if [ -n "$existing" ] && [ "$FORCE" -eq 0 ]; then
  cur="$("$existing" version 2>/dev/null | head -n1 || true)"
  if [ -n "$cur" ]; then
    # Require cur >= MIN_VERSION (lexicographic compare of numeric dot parts).
    ok="$(printf '%s\n%s\n' "$MIN_VERSION" "$cur" | sort -V | head -n1)"
    if [ "$ok" = "$MIN_VERSION" ]; then
      log "lightpanda $cur already installed at $existing (>= $MIN_VERSION) — nothing to do"
      exit 0
    fi
    log "lightpanda $cur at $existing is older than $MIN_VERSION — upgrading"
  else
    log "lightpanda at $existing does not run — reinstalling"
  fi
fi

# ── platform detection ──────────────────────────────────────────────────────
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
  Linux)  asset_os="linux" ;;
  Darwin) asset_os="macos" ;;
  *) die "unsupported OS '$os'. Build from source: https://github.com/$REPO" ;;
esac
case "$arch" in
  x86_64|amd64)  asset_arch="x86_64" ;;
  arm64|aarch64) asset_arch="aarch64" ;;
  *) die "unsupported architecture '$arch'. Build from source: https://github.com/$REPO" ;;
esac
log "host: $os/$arch -> $asset_arch-$asset_os"

# musl/Alpine has no glibc loader; the release binary cannot run there at all.
if [ "$asset_os" = "linux" ] && [ ! -e /lib64/ld-linux-x86-64.so.2 ] \
   && [ ! -e /lib/ld-linux-aarch64.so.1 ] && [ ! -e /lib/ld-linux.so.2 ]; then
  log "WARNING: no glibc dynamic loader found — the Linux release binary needs glibc"
  log "         (musl/Alpine is unsupported). If this is a musl host, build from source."
fi

# ── resolve release tag ─────────────────────────────────────────────────────
tag="${LIGHTPANDA_VERSION:-}"
if [ -z "$tag" ]; then
  log "resolving latest stable release..."
  tag="$(curl -fsSL --max-time 20 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -n1 | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  if [ -z "$tag" ]; then
    log "could not resolve latest stable tag (offline or rate-limited); falling back to 'nightly'"
    tag="nightly"
  fi
fi
url="https://github.com/$REPO/releases/download/$tag/lightpanda-$asset_arch-$asset_os"
log "release: $tag"

# ── download + verify ───────────────────────────────────────────────────────
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bin="$tmp/lightpanda"
log "downloading $url"
curl -fsSL --show-error --retry 3 --retry-delay 2 --connect-timeout 20 -o "$bin" "$url" \
  || die "download failed: $url"
chmod 0755 "$bin"
[ -s "$bin" ] || die "downloaded file is empty"

if ! out="$("$bin" version 2>&1)"; then
  log "binary does not execute on this host:"
  log "$out"
  case "$out" in
    *"required file not found"*|*GLIBC*|*"version GLIBC"*)
      die "this build needs glibc >= 2.35 (x86_64) / 2.38 (arm64); musl/Alpine is unsupported. Build from source: https://github.com/$REPO" ;;
    *)
      die "verification failed (see output above)" ;;
  esac
fi
log "verified executable: lightpanda $out"

# ── choose install dir ──────────────────────────────────────────────────────
skill_dir="$(cd "$(dirname "$0")/.." && pwd)"
dest_dir=""
if [ -n "${LIGHTPANDA_INSTALL_DIR:-}" ]; then
  mkdir -p "$LIGHTPANDA_INSTALL_DIR" 2>/dev/null || true
  [ -w "$LIGHTPANDA_INSTALL_DIR" ] || die "LIGHTPANDA_INSTALL_DIR is not writable: $LIGHTPANDA_INSTALL_DIR"
  dest_dir="$LIGHTPANDA_INSTALL_DIR"
else
  for cand in /usr/local/bin "$HOME/.local/bin" "$skill_dir/bin"; do
    if mkdir -p "$cand" 2>/dev/null && [ -w "$cand" ]; then
      dest_dir="$cand"
      break
    fi
  done
fi
[ -n "$dest_dir" ] || die "no writable install directory found (set LIGHTPANDA_INSTALL_DIR)"
log "install directory: $dest_dir"

# Atomic replace: write a sibling temp file, then rename over the target.
tmp_dest="$dest_dir/.lightpanda.$$"
install -m 0755 "$bin" "$tmp_dest"
mv -f "$tmp_dest" "$dest_dir/lightpanda"
log "installed $dest_dir/lightpanda ($("$dest_dir/lightpanda" version 2>/dev/null | head -n1))"

case ":$PATH:" in
  *":$dest_dir:"*) ;;
  *) log "NOTE: $dest_dir is not on PATH; the skill resolves it directly from this location." ;;
esac
