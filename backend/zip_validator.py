"""
Shared zip upload validation — enforces size limits, zip bomb protection,
path traversal checks, and allowed file types for plugin/skill uploads.
"""

import os
import re
import zipfile
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 50 * 1024 * 1024         # 50 MB
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024   # 200 MB (4:1 max compression ratio)
MAX_ENTRY_COUNT = 500                        # prevent zip bombs via many tiny files
MAX_ENTRY_SIZE = 50 * 1024 * 1024            # single entry cap
# Tool caches and OS leftovers that sneak into hand-made zips; ignored on upload and never installed.
JUNK_DIRS = {'.git', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.idea', '.vscode', '.venv',
             '__pycache__', 'node_modules', '__MACOSX'}
JUNK_FILES = {'.DS_Store', 'Thumbs.db'}


# Backup / editor leftovers: guides.md.bak-20261104, a.py.orig, a.py~, .a.py.swp, a.tmp
JUNK_FILE_RE = re.compile(r'(\.(bak|orig|rej|swp|swo|tmp)([-_.].*)?|~)$', re.IGNORECASE)


def is_junk_name(name: str) -> bool:
    return name in JUNK_DIRS or name in JUNK_FILES or bool(JUNK_FILE_RE.search(name))


def is_junk_path(path: str) -> bool:
    parts = path.replace('\\', '/').split('/')
    return any(p in JUNK_DIRS for p in parts[:-1]) or is_junk_name(parts[-1])


def copytree_ignore(_dir, names):
    """``shutil.copytree`` ignore callback that drops the junk above."""
    return [n for n in names if is_junk_name(n)]


ALLOWED_EXTENSIONS = {
    '.py', '.pyc', '.pyd',
    '.json', '.yaml', '.yml',
    '.md', '.txt', '.rst',
    '.cfg', '.ini', '.toml', '.conf',
    '.html', '.css', '.js', '.svg',
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp',
    '.ttf', '.woff', '.woff2',
    '.sql', '.csv',
    '.whl',
}


def validate_upload_zip(file_path: str, expected_filename: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validate a zip file before extraction for plugin/skill uploads.

    Returns (ok, error_message).  ok=True means the zip is safe to extract.
    """
    # --- 1. File exists + size check ----------------------------------------
    try:
        stat = os.stat(file_path)
    except OSError as exc:
        return False, f"Cannot read uploaded file: {exc}"

    if stat.st_size > MAX_UPLOAD_BYTES:
        return False, (
            f"Upload too large ({stat.st_size / 1024 / 1024:.1f} MB). "
            f"Maximum is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    # --- 2. Valid zip check -------------------------------------------------
    if not zipfile.is_zipfile(file_path):
        return False, "Not a valid zip file."

    # --- 3. Inspect entries -------------------------------------------------
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            entries = zf.namelist()

            if not entries:
                return False, "Zip file is empty."

            if len(entries) > MAX_ENTRY_COUNT:
                return False, f"Too many entries ({len(entries)}). Maximum is {MAX_ENTRY_COUNT}."

            # --- 3a. Path traversal check -----------------------------------
            for entry in entries:
                # Absolute paths
                if entry.startswith('/') or entry.startswith('\\'):
                    return False, f"Unsafe path in zip (absolute): {entry}"
                # Parent directory escapes
                parts = os.path.normpath(entry).split(os.sep)
                if '..' in parts:
                    return False, f"Unsafe path in zip (traversal): {entry}"

            # --- 3b. Zip bomb — total uncompressed size ---------------------
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if info.file_size > MAX_ENTRY_SIZE:
                    return False, (
                        f"Entry too large ({info.file_size / 1024 / 1024:.1f} MB): "
                        f"{info.filename}. Maximum single entry is "
                        f"{MAX_ENTRY_SIZE // 1024 // 1024} MB."
                    )

            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                return False, (
                    f"Total uncompressed size too large "
                    f"({total_uncompressed / 1024 / 1024:.1f} MB). "
                    f"Maximum is {MAX_UNCOMPRESSED_BYTES // 1024 // 1024} MB."
                )

            # --- 3c. File type whitelist ------------------------------------
            for entry in entries:
                if entry.endswith('/'):   # directory marker
                    continue
                if is_junk_path(entry):
                    continue
                ext = os.path.splitext(entry)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return False, f"Disallowed file type in zip: {entry} (extension '{ext}' not permitted)"

    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        return False, f"Corrupt or unreadable zip: {exc}"

    return True, ""
