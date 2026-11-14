"""Updating an installed plugin from a newer zip (see ``package_update`` for the flow)."""

import os
import tempfile

from backend.package_update import PackageUpdater, diff_trees  # noqa: F401 - diff_trees re-exported
from backend.plugin_lifecycle import PLUGINS_DIR
from backend.plugin_manager import plugin_manager

_stage_dir = os.path.join(tempfile.gettempdir(), 'evonic_plugin_updates')

_updater = PackageUpdater(
    'plugin',
    base_dir=lambda: PLUGINS_DIR,
    stage_dir=lambda: _stage_dir,
    read_manifest=lambda pid: plugin_manager._read_manifest(pid),
    find_manifest=lambda d: plugin_manager._find_manifest(d),
    install=lambda zip_path: plugin_manager.install_plugin(zip_path, force=True),
    after_restore=lambda pid: plugin_manager.reload_plugin(pid),
)

stage_update = _updater.stage
apply_update = _updater.apply
discard_update = _updater.discard
