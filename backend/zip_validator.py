"""
Shared zip upload validation — enforces size limits, zip bomb protection,
path traversal checks, allowed file types, and optional signature verification
for plugin/skill uploads.
"""

import os
import zipfile
from typing import Dict, Optional, Tuple
import logging

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 50 * 1024 * 1024         # 50 MB
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024   # 200 MB (4:1 max compression ratio)
MAX_ENTRY_COUNT = 500                        # prevent zip bombs via many tiny files
MAX_ENTRY_SIZE = 50 * 1024 * 1024            # single entry cap
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


def validate_upload_zip(file_path: str, expected_filename: Optional[str] = None, verify_signature: bool = False) -> Tuple[bool, str]:
    """
    Validate a zip file before extraction for plugin/skill uploads.
    
    Args:
        file_path: Path to the zip file to validate
        expected_filename: Optional expected filename (unused, for future use)
        verify_signature: If True, verify GPG signature for plugins
    
    Returns (ok, error_message).  ok=True means the zip is safe to extract.
    """
    # --- 0. Signature verification (if requested) ---------------------------
    if verify_signature:
        try:
            from backend.plugin_signer import verify_plugin_signature, is_signature_verification_enabled
            
            if is_signature_verification_enabled():
                sig_ok, sig_msg = verify_plugin_signature(file_path)
                if not sig_ok:
                    _logger.error("Signature verification failed for %s: %s", file_path, sig_msg)
                    return False, f"Signature verification failed: {sig_msg}"
                else:
                    _logger.info("Signature verified for %s: %s", file_path, sig_msg)
        except ImportError:
            _logger.warning("plugin_signer module not available, skipping signature verification")
        except Exception as e:
            _logger.error("Signature verification error for %s: %s", file_path, e, exc_info=True)
            return False, f"Signature verification error: {e}"
    
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
                ext = os.path.splitext(entry)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    return False, f"Disallowed file type in zip: {entry} (extension '{ext}' not permitted)"

    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        return False, f"Corrupt or unreadable zip: {exc}"

    return True, ""
