#!/usr/bin/env bash
# Archive and disable the access-sync bare repository on rigel2.
# Run as root on rigel2. The default mode is a read-only dry-run.
set -Eeuo pipefail

REPO_PATH="${ACCESS_SYNC_REPO:-/home/git/access-sync.git}"
ARCHIVE_ROOT="${ACCESS_SYNC_ARCHIVE_ROOT:-/root/access-sync-archives}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_DIR="$ARCHIVE_ROOT/$STAMP"
EXECUTE=0

usage() {
    cat <<'EOF'
Usage: disable-access-sync-rigel2.sh [--dry-run] [--execute]

Archives the bare access-sync repository and, with --execute, disables:
  * the repository path (renamed to *.disabled-<timestamp>, mode 000)
  * Nginx Git HTTP configurations that expose this repository path
  * authorized_keys entries explicitly labelled access-sync

Environment overrides:
  ACCESS_SYNC_REPO          default: /home/git/access-sync.git
  ACCESS_SYNC_ARCHIVE_ROOT  default: /root/access-sync-archives

The script never deletes the repository or its archives.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) EXECUTE=0 ;;
        --execute) EXECUTE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: run this script as root." >&2
    exit 1
fi
if [[ ! -d "$REPO_PATH" ]]; then
    echo "ERROR: bare repository does not exist: $REPO_PATH" >&2
    exit 1
fi
if [[ ! -f "$REPO_PATH/HEAD" || ! -d "$REPO_PATH/objects" ]]; then
    echo "ERROR: not a bare Git repository: $REPO_PATH" >&2
    exit 1
fi

mapfile -t NGINX_MATCHES < <(grep -RIl --exclude='*.log' -- "$REPO_PATH" /etc/nginx 2>/dev/null || true)
mapfile -t AUTH_FILES < <(find /root /home -path '*/.ssh/authorized_keys' -type f -print 2>/dev/null || true)

cat <<EOF
Repository: $REPO_PATH
Archive:    $ARCHIVE_DIR
Nginx files containing repository path: ${#NGINX_MATCHES[@]}
SSH authorized_keys files to inspect:   ${#AUTH_FILES[@]}
Mode:       $([[ $EXECUTE -eq 1 ]] && echo EXECUTE || echo DRY-RUN)
EOF

if [[ $EXECUTE -eq 0 ]]; then
    echo
    echo "Dry-run only; no files were changed. Re-run with --execute to proceed."
    printf '  nginx candidate: %s\n' "${NGINX_MATCHES[@]:-none}"
    printf '  authorized_keys: %s\n' "${AUTH_FILES[@]:-none}"
    exit 0
fi

mkdir -p "$ARCHIVE_DIR"
chmod 700 "$ARCHIVE_ROOT" "$ARCHIVE_DIR"

# Archive the complete bare repository before changing its path or permissions.
tar --numeric-owner --xattrs --acls -czf "$ARCHIVE_DIR/access-sync.git.tar.gz" \
    -C "$(dirname "$REPO_PATH")" "$(basename "$REPO_PATH")"
sha256sum "$ARCHIVE_DIR/access-sync.git.tar.gz" > "$ARCHIVE_DIR/SHA256SUMS"
{
    echo "archived_at=$STAMP"
    echo "source=$REPO_PATH"
    echo "repository_refs:"
    git --git-dir="$REPO_PATH" show-ref || true
} > "$ARCHIVE_DIR/METADATA.txt"
chmod 600 "$ARCHIVE_DIR"/*

DISABLED_PATH="${REPO_PATH}.disabled-${STAMP}"
if [[ -e "$DISABLED_PATH" ]]; then
    echo "ERROR: target already exists: $DISABLED_PATH" >&2
    exit 1
fi
mv "$REPO_PATH" "$DISABLED_PATH"
chmod 000 "$DISABLED_PATH"

# Disable only enabled Nginx files that explicitly reference this repository.
for file in "${NGINX_MATCHES[@]}"; do
    [[ -e "$file" ]] || continue
    if [[ "$file" == /etc/nginx/sites-enabled/* ]]; then
        mv "$file" "$file.disabled-${STAMP}"
    else
        cp -a "$file" "$file.disabled-${STAMP}"
        chmod 000 "$file"
    fi
done

# Preserve authorized_keys files and comment only entries explicitly labelled access-sync.
for file in "${AUTH_FILES[@]}"; do
    [[ -r "$file" ]] || continue
    if grep -qi 'access-sync' "$file"; then
        cp -a "$file" "$file.disabled-${STAMP}"
        sed -i '/access-sync/I s/^/# disabled by disable-access-sync-rigel2 /' "$file"
        chmod 600 "$file"
    fi
done

if command -v nginx >/dev/null 2>&1; then
    nginx -t
    if command -v systemctl >/dev/null 2>&1; then
        systemctl reload nginx 2>/dev/null || true
    fi
fi

cat <<EOF
Completed.
Archive:       $ARCHIVE_DIR
Disabled repo: $DISABLED_PATH
Nginx was tested and reloaded when available.
No repository or archive was deleted.
EOF
