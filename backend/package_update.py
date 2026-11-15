"""Updating an installed plugin or skill from a newer zip: stage it, diff it against the installed files, then apply it.

Two steps, so the user sees exactly what changes before anything is touched:

1. ``stage`` validates the zip, checks it is the same package and returns a diff plus a one-time token.
2. ``apply`` installs the staged zip over the installed package, restoring the old files if that fails.
"""

import difflib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.zip_validator import JUNK_DIRS, is_junk_name, validate_upload_zip

STAGE_TTL_SECONDS = 30 * 60
MAX_DIFF_FILE_BYTES = 200 * 1024     # bigger text files are listed as changed, without a line diff
MAX_DIFF_LINES = 400                 # per file; the rest is cut


def _collect(root: str, ignore: set) -> Dict[str, str]:
    """Relative path -> absolute path for every file worth comparing."""
    files = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
        for name in names:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            if name.endswith('.pyc') or name.startswith('.') or is_junk_name(name) or rel in ignore:
                continue
            files[rel] = path
    return files


def _read_text(path: str) -> Optional[List[str]]:
    """The file's lines, or None when it is binary or too big to diff."""
    try:
        if os.path.getsize(path) > MAX_DIFF_FILE_BYTES:
            return None
        with open(path, 'rb') as f:
            raw = f.read()
        if b'\0' in raw:
            return None
        return raw.decode('utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _same(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def diff_trees(old_root: str, new_root: str, ignore: set = frozenset()) -> List[Dict[str, Any]]:
    """Per-file changes from ``old_root`` to ``new_root``, sorted by path (unchanged files are left out)."""
    old, new = _collect(old_root, ignore), _collect(new_root, ignore)
    changes = []
    for rel in sorted(set(old) | set(new)):
        if rel in old and rel in new:
            if _same(old[rel], new[rel]):
                continue
            status = 'modified'
        else:
            status = 'added' if rel in new else 'removed'
        old_lines = _read_text(old[rel]) if rel in old else []
        new_lines = _read_text(new[rel]) if rel in new else []
        entry = {'path': rel, 'status': status, 'binary': old_lines is None or new_lines is None,
                 'added': 0, 'removed': 0, 'diff': ''}
        if not entry['binary']:
            lines = list(difflib.unified_diff(old_lines, new_lines, f'a/{rel}', f'b/{rel}', lineterm='', n=3))
            body = lines[2:]       # the ---/+++ header lines repeat the path
            entry['added'] = sum(1 for l in body if l.startswith('+'))
            entry['removed'] = sum(1 for l in body if l.startswith('-'))
            if len(body) > MAX_DIFF_LINES:
                body = body[:MAX_DIFF_LINES] + [f'... ({len(body) - MAX_DIFF_LINES} more lines)']
            entry['diff'] = '\n'.join(body)
        changes.append(entry)
    return changes


def _version_key(version: Any) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r'\d+', str(version or '')))


class PackageUpdater:
    """Update flow for one kind of package. Everything that differs between kinds is passed in as a callable
    so it is looked up at call time (and can be swapped in tests)."""

    def __init__(self, kind: str, base_dir: Callable[[], str], stage_dir: Callable[[], str],
                 read_manifest: Callable[[str], Optional[dict]], find_manifest: Callable[[str], Optional[str]],
                 install: Callable[[str], dict], after_restore: Callable[[str], None] = lambda _id: None,
                 ignore: set = frozenset()):
        self.kind, self.base_dir, self.stage_dir = kind, base_dir, stage_dir
        self.read_manifest, self.find_manifest = read_manifest, find_manifest
        self.install, self.after_restore, self.ignore = install, after_restore, set(ignore)
        self._staged: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _purge_expired(self):
        cutoff = time.time() - STAGE_TTL_SECONDS
        for token, item in list(self._staged.items()):
            if item['created'] < cutoff:
                self._staged.pop(token, None)
                try:
                    os.unlink(item['path'])
                except OSError:
                    pass

    def stage(self, pkg_id: str, zip_path: str, filename: str) -> Dict[str, Any]:
        """Validate ``zip_path`` as a new version of ``pkg_id`` and return the preview.

        The result has ``error`` on failure, otherwise ``token`` (for ``apply``), ``old``/``new`` manifests,
        the ``version_relation`` (``newer``, ``same`` or ``older``) and the file ``changes``.
        """
        installed = self.read_manifest(pkg_id)
        if not installed:
            return {'error': f'{self.kind.title()} not found: {pkg_id}'}

        ok, err = validate_upload_zip(zip_path, expected_filename=filename)
        if not ok:
            return {'error': err}

        with tempfile.TemporaryDirectory() as tmp:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(tmp)
            except (zipfile.BadZipFile, OSError) as e:
                return {'error': f'Could not read the zip: {e}'}
            manifest_path = self.find_manifest(tmp)
            if not manifest_path:
                return {'error': f'No {self.kind}.json found in the zip'}
            try:
                with open(manifest_path, encoding='utf-8') as f:
                    new_manifest = json.load(f)
            except (ValueError, OSError):
                return {'error': f'{self.kind}.json in the zip is not valid JSON'}
            if new_manifest.get('id') != pkg_id:
                return {'error': f'This zip is for {self.kind} "{new_manifest.get("id")}", not "{pkg_id}".'}

            changes = diff_trees(os.path.join(self.base_dir(), pkg_id), os.path.dirname(manifest_path), self.ignore)

        old_v, new_v = _version_key(installed.get('version')), _version_key(new_manifest.get('version'))
        relation = 'newer' if new_v > old_v else ('older' if new_v < old_v else 'same')

        stage_dir = self.stage_dir()
        os.makedirs(stage_dir, exist_ok=True)
        token = secrets.token_urlsafe(16)
        staged_path = os.path.join(stage_dir, token + '.zip')
        shutil.copyfile(zip_path, staged_path)
        with self._lock:
            self._purge_expired()
            self._staged[token] = {'id': pkg_id, 'path': staged_path, 'created': time.time()}

        pick = lambda m: {k: m.get(k) for k in ('name', 'version', 'author', 'description')}
        return {'token': token, 'old': pick(installed), 'new': pick(new_manifest), 'version_relation': relation,
                'changes': changes,
                'summary': {'added': sum(c['status'] == 'added' for c in changes),
                            'modified': sum(c['status'] == 'modified' for c in changes),
                            'removed': sum(c['status'] == 'removed' for c in changes)}}

    def discard(self, token: str):
        with self._lock:
            item = self._staged.pop(token, None)
        if item:
            try:
                os.unlink(item['path'])
            except OSError:
                pass

    def apply(self, pkg_id: str, token: str) -> Dict[str, Any]:
        """Install the staged zip over ``pkg_id``. The old files come back if the install fails."""
        with self._lock:
            item = self._staged.get(token)
            if not item or item['id'] != pkg_id or not os.path.isfile(item['path']):
                return {'error': 'This update has expired. Drop the file again.'}
            self._staged.pop(token)

        pkg_dir = os.path.join(self.base_dir(), pkg_id)
        with tempfile.TemporaryDirectory() as backup_root:
            backup = os.path.join(backup_root, pkg_id)
            shutil.copytree(pkg_dir, backup)
            try:
                result = self.install(item['path'])
                if 'error' not in result and result.get('id') != pkg_id:
                    result = {'error': f'Zip contains {self.kind} "{result.get("id")}", not "{pkg_id}".'}
            except Exception as e:      # noqa: BLE001 - any failure must roll back
                result = {'error': f'Update failed: {e}'}
            if 'error' in result:
                if os.path.exists(pkg_dir):
                    shutil.rmtree(pkg_dir, ignore_errors=True)
                shutil.copytree(backup, pkg_dir)
                self.after_restore(pkg_id)
        try:
            os.unlink(item['path'])
        except OSError:
            pass
        return result
