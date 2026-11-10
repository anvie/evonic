"""
Backup-aware persistence for ``agents/<id>/SYSTEM.md``.

Every write keeps a small rotating backup of the previous content so a manual
edit that gets overwritten by another code path (setup, tools, UI) can still be
recovered from ``agents/<id>/.backup/``.
"""

import os
import shutil
from datetime import datetime, timezone

MAX_BACKUPS = 5
BACKUP_DIRNAME = '.backup'
PROMPT_FILENAME = 'SYSTEM.md'


def prompt_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, PROMPT_FILENAME)


def _read(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def _rotate(backup_dir: str) -> None:
    try:
        names = sorted(
            n for n in os.listdir(backup_dir)
            if n.startswith('SYSTEM.') and n.endswith('.md')
        )
    except OSError:
        return
    for name in names[:-MAX_BACKUPS]:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass


def backup_existing(agent_dir: str):
    """Copy the current SYSTEM.md into .backup/. Returns the backup path or None."""
    path = prompt_path(agent_dir)
    if not os.path.isfile(path):
        return None
    backup_dir = os.path.join(agent_dir, BACKUP_DIRNAME)
    try:
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        dest = os.path.join(backup_dir, f'SYSTEM.{stamp}.md')
        shutil.copy2(path, dest)
        _rotate(backup_dir)
        return dest
    except OSError:
        return None


def write_system_prompt(agent_dir: str, content: str):
    """Write SYSTEM.md, backing up a differing previous version first."""
    os.makedirs(agent_dir, exist_ok=True)
    path = prompt_path(agent_dir)
    previous = _read(path) if os.path.isfile(path) else None
    if previous is not None and previous != content:
        backup_existing(agent_dir)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
