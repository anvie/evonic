#!/bin/sh
# Evonet installer: download the binary, join an Evonic server, install a service.
#
#   curl -fsSL https://raw.githubusercontent.com/anvie/evonic/main/evonet/scripts/install.sh \
#     | sh -s -- --server https://evonic.example.com --token ejt_xxx
#
# Missing values are prompted for when a terminal is available. For automation
# (Ansible, cloud-init) pass --yes and set EVONET_SERVER / EVONET_JOIN_TOKEN so
# the token never appears in the process list. Safe to re-run: it upgrades the
# binary and re-joins the same workplace.
#
# Options (env equivalent in parentheses):
#   --server URL    Evonic server URL        (EVONET_SERVER)
#   --token TOKEN   Join token               (EVONET_JOIN_TOKEN)
#   --name NAME     Workplace name           (EVONET_NAME, default: hostname)
#   --workdir PATH  Working directory        (EVONET_WORKDIR)
#   --version TAG   Release tag, e.g. v1.3.0 (EVONET_VERSION, default: latest)
#   --no-service    Do not install/start a service
#   --yes           Never prompt; fail if a required value is missing
set -eu

REPO="anvie/evonic"
SERVER="${EVONET_SERVER:-}"
TOKEN="${EVONET_JOIN_TOKEN:-}"
NAME="${EVONET_NAME:-}"
WORKDIR="${EVONET_WORKDIR:-}"
VERSION="${EVONET_VERSION:-}"
SERVICE=1
YES=0

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server)  [ $# -ge 2 ] || die "--server needs a value";  SERVER="$2"; shift 2 ;;
    --token)   [ $# -ge 2 ] || die "--token needs a value";   TOKEN="$2"; shift 2 ;;
    --name)    [ $# -ge 2 ] || die "--name needs a value";    NAME="$2"; shift 2 ;;
    --workdir) [ $# -ge 2 ] || die "--workdir needs a value"; WORKDIR="$2"; shift 2 ;;
    --version) [ $# -ge 2 ] || die "--version needs a value"; VERSION="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --yes|-y)  YES=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

# Prompt for a missing value when interactive; otherwise fail clearly.
ask() { # var_name label secret
  eval "cur=\${$1}"
  [ -z "$cur" ] || return 0
  if [ "$YES" = 1 ] || [ ! -r /dev/tty ]; then
    die "$2 is required (pass the flag or set the environment variable)"
  fi
  printf '%s: ' "$2" >/dev/tty
  if [ "${3:-}" = secret ]; then stty -echo </dev/tty 2>/dev/null || true; fi
  read -r val </dev/tty || die "could not read $2"
  if [ "${3:-}" = secret ]; then stty echo </dev/tty 2>/dev/null || true; printf '\n' >/dev/tty; fi
  eval "$1=\$val"
}
ask SERVER "Evonic server URL (https://...)"
ask TOKEN "Join token" secret
[ -n "$SERVER" ] && [ -n "$TOKEN" ] || die "server and token are required"

# --- platform ---------------------------------------------------------------
case "$(uname -s)" in
  Linux)  OS=linux;  BIN_OS=linux;  ASSET_OS=linux ;;
  Darwin) OS=darwin; BIN_OS=darwin; ASSET_OS=macos ;;
  *) die "unsupported OS: $(uname -s) (use install on Windows manually)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
ASSET="evonet-$ASSET_OS-$ARCH.zip"
INNER="evonet-$BIN_OS-$ARCH"

if [ -n "${EVONET_DOWNLOAD_BASE:-}" ]; then   # test hook: local mirror of a release
  BASE="$EVONET_DOWNLOAD_BASE"
elif [ -n "$VERSION" ]; then
  BASE="https://github.com/$REPO/releases/download/$VERSION"
else
  BASE="https://github.com/$REPO/releases/latest/download"
fi

# --- tools ------------------------------------------------------------------
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
else
  die "curl or wget is required"
fi
command -v unzip >/dev/null 2>&1 || die "unzip is required"
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  die "sha256sum or shasum is required"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

say "Downloading $ASSET ..."
fetch "$BASE/$ASSET" "$TMP/$ASSET" || die "download failed: $BASE/$ASSET"
fetch "$BASE/checksums.txt" "$TMP/checksums.txt" || die "download failed: $BASE/checksums.txt"
want="$(awk -v f="$ASSET" '$2 == f {print $1}' "$TMP/checksums.txt")"
[ -n "$want" ] || die "no checksum listed for $ASSET"
[ "$(sha256 "$TMP/$ASSET")" = "$want" ] || die "checksum mismatch for $ASSET"

unzip -qo "$TMP/$ASSET" -d "$TMP/x"
[ -f "$TMP/x/$INNER" ] || die "archive does not contain $INNER"

# --- install binary ---------------------------------------------------------
if [ "$(id -u)" = 0 ]; then
  BIN_DIR=/usr/local/bin
else
  BIN_DIR="$HOME/.local/bin"
fi
mkdir -p "$BIN_DIR"
install -m 0755 "$TMP/x/$INNER" "$BIN_DIR/evonet.new"
mv -f "$BIN_DIR/evonet.new" "$BIN_DIR/evonet"
say "Installed $BIN_DIR/evonet ($("$BIN_DIR/evonet" version))"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "Note: add $BIN_DIR to your PATH." ;; esac

# --- join -------------------------------------------------------------------
say "Joining $SERVER ..."
EVONET_SERVER="$SERVER" EVONET_JOIN_TOKEN="$TOKEN" EVONET_NAME="$NAME" EVONET_WORKDIR="$WORKDIR" \
  "$BIN_DIR/evonet" join

# --- service ----------------------------------------------------------------
[ "$SERVICE" = 1 ] || { say "Skipping service. Run '$BIN_DIR/evonet run' to connect."; exit 0; }

if [ "$OS" = linux ]; then
  command -v systemctl >/dev/null 2>&1 || { say "systemd not found. Run '$BIN_DIR/evonet run' to connect."; exit 0; }
  if [ "$(id -u)" = 0 ]; then
    UNIT=/etc/systemd/system/evonet.service
    CTL="systemctl"
    WANTED=multi-user.target
  else
    mkdir -p "$HOME/.config/systemd/user"
    UNIT="$HOME/.config/systemd/user/evonet.service"
    CTL="systemctl --user"
    WANTED=default.target
  fi
  cat >"$UNIT" <<EOF
[Unit]
Description=Evonet Workplace Connector
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$BIN_DIR/evonet run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=$WANTED
EOF
  $CTL daemon-reload
  $CTL enable evonet >/dev/null 2>&1 || true
  $CTL restart evonet
  say "Service evonet started."
  [ "$(id -u)" = 0 ] || say "Tip: run 'loginctl enable-linger $(id -un)' so it keeps running after logout."
else
  PLIST="$HOME/Library/LaunchAgents/com.evonic.evonet.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.evonic.evonet</string>
  <key>ProgramArguments</key>
  <array><string>$BIN_DIR/evonet</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load -w "$PLIST"
  say "LaunchAgent com.evonic.evonet loaded."
fi
