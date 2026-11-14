"""Updating an installed skill from a newer zip (see ``package_update`` for the flow)."""

import os
import tempfile

from backend.package_update import PackageUpdater
from backend.skills_manager import SKILLS_DIR, skills_manager

_stage_dir = os.path.join(tempfile.gettempdir(), 'evonic_skill_updates')

_updater = PackageUpdater(
    'skill',
    base_dir=lambda: SKILLS_DIR,
    stage_dir=lambda: _stage_dir,
    read_manifest=lambda sid: skills_manager.get_skill(sid),
    find_manifest=lambda d: skills_manager._find_manifest(d),
    install=lambda zip_path: skills_manager.install_skill(zip_path, force=True),
    ignore={'config.json'},     # the skill's saved settings are kept across an update, so they are not a change
)

stage_update = _updater.stage
apply_update = _updater.apply
discard_update = _updater.discard
