"""
Plugin Lifecycle — PluginManager class for loading, unloading, and managing plugins.

Extracted from plugin_manager.py as part of the refactor. Handles:
- load/unload/reload, install/uninstall, enable/disable
- Config management, discovery & metadata
- Event bridging, route registration, dashboard cards
"""

import logging
import os
import re
import sys
import json
import shutil
import types
import zipfile
import tempfile
import importlib.util
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable, Tuple
from concurrent.futures import ThreadPoolExecutor

from backend.plugin_sdk import PluginSDK
from backend.plugin_hooks import (
    _tool_guards, _message_interceptors, _builtin_suppressors,
    _state_handlers, _unload_plugin_state_handlers,
)

_logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(BASE_DIR, 'plugins')

# Supported events
VALID_EVENTS = {
    'turn_complete', 'message_received', 'session_created', 'summary_updated',
    'processing_started', 'llm_thinking', 'llm_response_chunk',
    'tool_executed', 'final_answer', 'message_sent',
    'kanban_task_created', 'kanban_task_updated',
    'schedule_fired', 'schedule_created', 'schedule_cancelled',
    'state_transition',
}


class PluginManager:
    MAX_LOG_ENTRIES = 500

    def __init__(self):
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        self._handlers: Dict[str, List[Tuple[str, Callable]]] = {}  # event -> [(plugin_id, fn)]
        self._modules: Dict[str, Any] = {}  # plugin_id -> loaded module
        self._logs: Dict[str, deque] = {}  # plugin_id -> deque of log entries
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='plugin')
        self._event_bridges: Dict[str, List[tuple]] = {}  # plugin_id -> [(event_name, bridge_fn)]
        self._blueprints: Dict[str, Any] = {}  # plugin_id -> Blueprint
        self._dashboard_cards: Dict[str, List[Tuple[str, Callable]]] = {}  # plugin_id -> [(card_id, fn)]
        self._load_all()

    def _is_plugin_enabled(self, plugin_id: str) -> bool:
        """Check if a plugin is enabled. DB is authoritative; absent = disabled."""
        from models.db import db
        return db.get_setting(f'plugin_enabled:{plugin_id}') == '1'

    def _load_all(self):
        """Load handlers from all enabled plugins at startup."""
        self._handlers.clear()
        self._modules.clear()
        self._event_bridges.clear()
        for plugin in self.list_plugins():
            if self._is_plugin_enabled(plugin['id']):
                self._load_plugin(plugin['id'])

    def _load_plugin(self, plugin_id: str):
        """Load a plugin's handler.py and register its event handlers."""
        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        handler_path = os.path.join(plugin_dir, 'handler.py')

        # Register the plugin directory as a package so relative imports work
        pkg_name = f'plugin_pkg_{plugin_id}_{id(self)}'
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [plugin_dir]
        pkg.__package__ = pkg_name
        pkg.__file__ = os.path.join(plugin_dir, '__init__.py')
        sys.modules[pkg_name] = pkg

        # Read manifest to know which events and slash commands this plugin subscribes to
        manifest_path = os.path.join(plugin_dir, 'plugin.json')
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)
            events = manifest.get('events', [])
            slash_commands = manifest.get('slash_commands', [])
            dashboard_cards = manifest.get('dashboard_cards', [])
        else:
            events = []
            slash_commands = []
            dashboard_cards = []

        module = None

        if os.path.isfile(handler_path):
            try:
                # Load handler.py as a submodule of that package
                module_name = f'{pkg_name}.handler'
                spec = importlib.util.spec_from_file_location(module_name, handler_path)
                module = importlib.util.module_from_spec(spec)
                module.__package__ = pkg_name
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                self._modules[plugin_id] = module

                # Register handler functions and bridge them to the event stream
                from backend.event_stream import event_stream
                bridges = []
                for event_name in events:
                    fn_name = f'on_{event_name}'
                    fn = getattr(module, fn_name, None)
                    if fn and callable(fn):
                        if event_name not in self._handlers:
                            self._handlers[event_name] = []
                        self._handlers[event_name].append((plugin_id, fn))

                        bridge = self._make_event_bridge(plugin_id, fn)
                        event_stream.on(event_name, bridge)
                        bridges.append((event_name, bridge))

                self._event_bridges[plugin_id] = bridges

            except Exception as e:
                _logger.error("Failed to load plugin '%s': %s", plugin_id, e, exc_info=True)
                # Don't return — still try to load routes.py if it exists

        # Register slash commands declared in the manifest
        if slash_commands:
            for sc in slash_commands:
                sc_name = sc.get('id', '')
                sc_desc = sc.get('description', '')
                if sc_name:
                    try:
                        sdk = PluginSDK(plugin_id, manifest, {}, log_callback=self.add_log)
                        sdk.register_slash_command(sc_name, sc_desc)
                        self.add_log(plugin_id, 'info', f"Slash command registered: /{sc_name}")
                    except Exception as e:
                        self.add_log(plugin_id, 'error', f"Failed to register slash command '{sc_name}': {e}")
                        _logger.error("Failed to register slash command '%s' for '%s': %s", sc_name, plugin_id, e, exc_info=True)

        # Check for route registration (create_blueprint function)
        route_module = module

        if route_module is None or not hasattr(route_module, 'create_blueprint') or not callable(route_module.create_blueprint):
            routes_path = os.path.join(plugin_dir, 'routes.py')
            if os.path.isfile(routes_path):
                try:
                    routes_module_name = f'{pkg_name}.routes'
                    routes_spec = importlib.util.spec_from_file_location(routes_module_name, routes_path)
                    routes_module = importlib.util.module_from_spec(routes_spec)
                    routes_module.__package__ = pkg_name
                    sys.modules[routes_module_name] = routes_module
                    routes_spec.loader.exec_module(routes_module)
                    route_module = routes_module
                except Exception as e:
                    self.add_log(plugin_id, 'error', f"Failed to load routes.py: {e}")
                    _logger.error("Failed to load routes.py for '%s': %s", plugin_id, e)
                    traceback.print_exc()

        if route_module and hasattr(route_module, 'create_blueprint') and callable(route_module.create_blueprint):
            try:
                bp = route_module.create_blueprint()
                self._blueprints[plugin_id] = bp
                self.add_log(plugin_id, 'info', f"Route blueprint registered: {bp.name}")
            except Exception as e:
                self.add_log(plugin_id, 'error', f"Failed to create blueprint: {e}")
                _logger.error("Failed to create blueprint for '%s': %s", plugin_id, e)
                traceback.print_exc()

        # Register dashboard card handlers (from handler.py if loaded)
        if dashboard_cards and module:
            self._dashboard_cards[plugin_id] = []
            for card in dashboard_cards:
                fn_name = card.get('handler')
                if fn_name:
                    fn = getattr(module, fn_name, None)
                    if fn and callable(fn):
                        card_id = card.get('id', fn_name)
                        self._dashboard_cards[plugin_id].append((card_id, fn))
                    else:
                        self.add_log(plugin_id, 'error', f"Dashboard card handler '{fn_name}' not found")
                        _logger.error("Dashboard card handler '%s' not found for '%s'", fn_name, plugin_id)
                else:
                    self.add_log(plugin_id, 'warn', f"Dashboard card missing 'handler' field: {card.get('id', '?')}")
            if self._dashboard_cards.get(plugin_id):
                self.add_log(plugin_id, 'info', f"Registered {len(self._dashboard_cards[plugin_id])} dashboard card(s)")

    def _unload_plugin(self, plugin_id: str):
        """Remove all handler registrations for a plugin."""
        self._modules.pop(plugin_id, None)
        prefix = f'plugin_pkg_{plugin_id}_'
        for key in [k for k in sys.modules if k == prefix[:-1] or k.startswith(prefix)]:
            sys.modules.pop(key, None)
        for event_name in list(self._handlers.keys()):
            self._handlers[event_name] = [
                (pid, fn) for pid, fn in self._handlers[event_name]
                if pid != plugin_id
            ]
            if not self._handlers[event_name]:
                del self._handlers[event_name]

        from backend.event_stream import event_stream
        for event_name, bridge in self._event_bridges.pop(plugin_id, []):
            event_stream.off(event_name, bridge)

        # Unregister side-channel hooks
        for registry in (_tool_guards, _message_interceptors, _builtin_suppressors):
            registry[:] = [fn for fn in registry
                           if not getattr(fn, '__module__', '').startswith(prefix)]

        self._blueprints.pop(plugin_id, None)
        self._dashboard_cards.pop(plugin_id, None)

        # Unregister slash commands
        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        manifest_path = os.path.join(plugin_dir, 'plugin.json')
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)
            for sc in manifest.get('slash_commands', []):
                sc_name = sc.get('id', '')
                if sc_name:
                    from backend.slash_commands import command_registry
                    command_registry._commands.pop(sc_name, None)

    def get_dashboard_cards(self) -> List[Dict[str, Any]]:
        """Call all registered dashboard card handlers and collect results."""
        cards = []
        for plugin_id, card_list in self._dashboard_cards.items():
            for card_id, fn in card_list:
                try:
                    config = self.get_plugin_config(plugin_id)
                    sdk = PluginSDK(plugin_id, config, {},
                                    log_callback=self.add_log)
                    result = fn(sdk)
                    if result and isinstance(result, dict):
                        cards.append(result)
                except Exception as e:
                    self.add_log(plugin_id, 'error', f'Dashboard card handler error: {e}')
                    _logger.error("Dashboard card handler error in '%s': %s", plugin_id, e)
        return cards

    def reload_plugin(self, plugin_id: str):
        """Reload a plugin (unload then load if enabled)."""
        self._unload_plugin(plugin_id)
        manifest = self._read_manifest(plugin_id)
        if manifest and self._is_plugin_enabled(plugin_id):
            self._load_plugin(plugin_id)

    # ── Logging ──

    def add_log(self, plugin_id: str, level: str, message: str):
        """Append a log entry to the plugin's ring buffer."""
        if plugin_id not in self._logs:
            self._logs[plugin_id] = deque(maxlen=self.MAX_LOG_ENTRIES)
        self._logs[plugin_id].append({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': level,
            'message': message,
        })

    def get_logs(self, plugin_id: str, limit: int = 100,
                 since: str = None) -> List[Dict[str, str]]:
        """Return log entries for a plugin, optionally filtered by timestamp."""
        entries = list(self._logs.get(plugin_id, []))
        if since:
            entries = [e for e in entries if e['timestamp'] > since]
        return entries[-limit:]

    def clear_logs(self, plugin_id: str):
        """Clear all log entries for a plugin."""
        if plugin_id in self._logs:
            self._logs[plugin_id].clear()

    def _make_event_bridge(self, plugin_id: str, fn: Callable) -> Callable:
        """Create a bridge function that wraps a plugin handler for the event stream."""
        def bridge(event_data: dict):
            from models.db import db
            if db.get_setting('events_dispatch_enabled', '1') != '1':
                return
            preview_parts = []
            for k, v in event_data.items():
                s = str(v)
                preview_parts.append(f"{k}={s[:120]}")
            config = self.get_plugin_config(plugin_id)
            sdk = PluginSDK(plugin_id, config, event_data,
                            log_callback=self.add_log)
            self._executor.submit(self._safe_call, plugin_id, fn, event_data, sdk)
        return bridge

    # ── Route Registration ──

    def get_blueprints(self) -> Dict[str, Any]:
        """Return all registered plugin blueprints."""
        return self._blueprints

    def get_blueprint_names(self) -> List[str]:
        """Return list of registered blueprint names."""
        return list(self._blueprints.keys())

    def dispatch(self, event_name: str, event_data: dict):
        """Dispatch an event via the event stream (non-blocking). Backward compat."""
        from backend.event_stream import event_stream
        event_stream.emit(event_name, event_data)

    def _safe_call(self, plugin_id: str, fn: Callable, event: dict, sdk: PluginSDK):
        """Call a handler function with error isolation."""
        try:
            fn(event, sdk)
        except Exception as e:
            self.add_log(plugin_id, 'error', f"Handler error: {e}")
            _logger.error("Handler error in '%s': %s", plugin_id, e, exc_info=True)

    # ── Discovery & metadata ──

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all installed plugins with metadata."""
        plugins = []
        if not os.path.isdir(PLUGINS_DIR):
            return plugins
        for name in sorted(os.listdir(PLUGINS_DIR)):
            plugin_dir = os.path.join(PLUGINS_DIR, name)
            manifest_path = os.path.join(plugin_dir, 'plugin.json')
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, encoding='utf-8') as f:
                    manifest = json.load(f)
                manifest['_dir'] = plugin_dir
                manifest['event_count'] = len(manifest.get('events', []))
                manifest['enabled'] = self._is_plugin_enabled(name)
                manifest['is_system'] = manifest.get('category') == 'system'
                plugins.append(manifest)
            except (json.JSONDecodeError, KeyError):
                continue
        return plugins

    def get_nav_items(self) -> List[Dict[str, Any]]:
        """Return nav items declared by all enabled plugins."""
        items = []
        for plugin in self.list_plugins():
            if self._is_plugin_enabled(plugin['id']):
                for item in plugin.get('nav_items', []):
                    items.append({
                        'label': item.get('label', ''),
                        'path': item.get('path', ''),
                        'plugin_id': plugin['id'],
                    })
        return items

    def get_cli_commands(self) -> Dict[str, Any]:
        """Return CLI commands declared by all enabled plugins."""
        result = {}
        for plugin in self.list_plugins():
            if not self._is_plugin_enabled(plugin['id']):
                continue
            cli_commands = plugin.get('cli_commands', [])
            plugin_id = plugin['id']
            module = self._modules.get(plugin_id)
            for cmd in cli_commands:
                cmd_name = cmd.get('name', '')
                if not cmd_name:
                    continue
                cmd_info = {
                    'help': cmd.get('help', ''),
                    'description': cmd.get('description', ''),
                    'handler': cmd.get('handler'),
                    'plugin_id': plugin_id,
                }
                subcommands = {}
                for sub in cmd.get('subcommands', []):
                    sub_name = sub.get('name', '')
                    if not sub_name:
                        continue
                    sub_info = {
                        'help': sub.get('help', ''),
                        'description': sub.get('description', ''),
                        'handler': sub.get('handler'),
                        'arguments': sub.get('arguments', []),
                        'plugin_id': plugin_id,
                    }
                    subcommands[sub_name] = sub_info
                cmd_info['subcommands'] = subcommands
                result[cmd_name] = cmd_info
        return result

    def get_plugin(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Get a single plugin's metadata, events, variables, config, and README."""
        manifest = self._read_manifest(plugin_id)
        if not manifest:
            return None
        manifest['enabled'] = self._is_plugin_enabled(plugin_id)
        manifest['events'] = manifest.get('events', [])
        manifest['event_count'] = len(manifest['events'])
        manifest['variables'] = manifest.get('variables', [])
        manifest['config'] = self.get_plugin_config(plugin_id)

        # Read README.md if it exists
        readme_path = os.path.join(PLUGINS_DIR, plugin_id, 'README.md')
        if os.path.isfile(readme_path):
            try:
                with open(readme_path, encoding='utf-8') as f:
                    manifest['readme'] = f.read()
            except (IOError, UnicodeDecodeError):
                manifest['readme'] = None
        else:
            manifest['readme'] = None

        return manifest

    def _read_manifest(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Read a plugin's manifest file."""
        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        manifest_path = os.path.join(plugin_dir, 'plugin.json')
        if not os.path.isfile(manifest_path):
            return None
        try:
            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)
            manifest['_dir'] = plugin_dir
            return manifest
        except (json.JSONDecodeError, IOError):
            return None

    # ── Install / Uninstall ──

    def install_plugin(self, zip_path: str, force: bool = False) -> Dict[str, Any]:
        """Install a plugin from a zip file."""
        if not zipfile.is_zipfile(zip_path):
            return {'error': 'Not a valid zip file'}
        
        # Validate zip file including signature verification
        from backend.zip_validator import validate_upload_zip
        ok, error_msg = validate_upload_zip(zip_path, verify_signature=True)
        if not ok:
            _logger.error("Plugin zip validation failed: %s", error_msg)
            return {'error': error_msg}

        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for entry in zf.namelist():
                    if entry.startswith('/') or '..' in entry:
                        return {'error': f'Unsafe path in zip: {entry}'}
                zf.extractall(tmp_dir)

            manifest_path = self._find_manifest(tmp_dir)
            if not manifest_path:
                return {'error': 'No plugin.json found in zip'}

            with open(manifest_path, encoding='utf-8') as f:
                manifest = json.load(f)

            plugin_id = manifest.get('id', '')
            if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
                return {'error': f'Invalid plugin id: {plugin_id}'}

            plugin_src = os.path.dirname(manifest_path)
            plugin_dest = os.path.join(PLUGINS_DIR, plugin_id)

            if os.path.exists(plugin_dest) and not force:
                return {'error': f'Plugin "{plugin_id}" is already installed. Uninstall it first or use force to overwrite.'}

            if os.path.exists(plugin_dest):
                shutil.rmtree(plugin_dest)
            shutil.copytree(plugin_src, plugin_dest)

            self.reload_plugin(plugin_id)
            return manifest

    def install_plugin_from_dir(self, source_dir: str, force: bool = False) -> Dict[str, Any]:
        """Install a plugin from a directory path (for CLI usage)."""
        manifest_path = os.path.join(source_dir, 'plugin.json')
        if not os.path.isfile(manifest_path):
            return {'error': f'No plugin.json found in {source_dir}'}

        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)

        plugin_id = manifest.get('id', '')
        if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
            return {'error': f'Invalid plugin id: {plugin_id}'}

        plugin_dest = os.path.join(PLUGINS_DIR, plugin_id)
        source_norm = os.path.normpath(os.path.abspath(source_dir))
        dest_norm = os.path.normpath(os.path.abspath(plugin_dest))

        if source_norm != dest_norm:
            if os.path.exists(plugin_dest) and not force:
                return {'error': f'Plugin "{plugin_id}" is already installed. Uninstall it first or use force to overwrite.'}
            if os.path.exists(plugin_dest):
                shutil.rmtree(plugin_dest)
            shutil.copytree(source_dir, plugin_dest)

        self.reload_plugin(plugin_id)
        return manifest

    def uninstall_plugin(self, plugin_id: str) -> Dict[str, Any]:
        """Uninstall a plugin: unload handlers then delete directory."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
            return {'error': 'Invalid plugin id'}

        manifest = self._read_manifest(plugin_id)
        if not manifest:
            return {'error': f'Plugin not found: {plugin_id}'}

        if manifest.get('category') == 'system':
            return {'error': f'Cannot delete system plugin: {plugin_id}'}

        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        if not os.path.isdir(plugin_dir):
            return {'error': f'Plugin not found: {plugin_id}'}

        self._unload_plugin(plugin_id)
        shutil.rmtree(plugin_dir)
        return {'success': True}

    # ── Enable / Disable ──

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> Dict[str, Any]:
        """Enable or disable a plugin. State is stored in DB, not in plugin.json."""
        if not self._read_manifest(plugin_id):
            return {'error': f'Plugin not found: {plugin_id}'}

        from models.db import db
        db.set_setting(f'plugin_enabled:{plugin_id}', '1' if enabled else '0')
        self.reload_plugin(plugin_id)

        manifest = self._read_manifest(plugin_id) or {}
        manifest['enabled'] = enabled
        manifest.pop('_dir', None)
        return manifest

    # ── Configuration ──

    def get_plugin_variables(self, plugin_id: str) -> List[Dict[str, Any]]:
        """Read the variables schema from plugin.json."""
        manifest = self._read_manifest(plugin_id)
        if not manifest:
            return []
        return manifest.get('variables', [])

    def get_plugin_config(self, plugin_id: str) -> Dict[str, Any]:
        """Load config from DB merged with defaults from variables schema."""
        variables = self.get_plugin_variables(plugin_id)
        config = {}
        for v in variables:
            config[v['name']] = v.get('default', '')

        from models.db import db

        has_db_values = variables and any(
            db.get_setting(f'plugin_config:{plugin_id}:{v["name"]}') is not None
            for v in variables
        )

        if not has_db_values:
            config_path = os.path.join(PLUGINS_DIR, plugin_id, 'config.json')
            if os.path.isfile(config_path):
                try:
                    with open(config_path, encoding='utf-8') as f:
                        file_config = json.load(f)
                    var_names = {v['name'] for v in variables}
                    for name, val in file_config.items():
                        if name in var_names:
                            db.set_setting(f'plugin_config:{plugin_id}:{name}', str(val))
                except (json.JSONDecodeError, IOError):
                    pass

        for v in variables:
            key = f'plugin_config:{plugin_id}:{v["name"]}'
            stored = db.get_setting(key)
            if stored is not None:
                # Mask secret values in API responses
                if v.get('secret', False):
                    config[v['name']] = '••••••••'
                else:
                    var_type = v.get('type', 'string')
                    if var_type == 'boolean':
                        config[v['name']] = stored in ('1', 'true', 'True')
                    elif var_type == 'number':
                        try:
                            config[v['name']] = float(stored) if '.' in stored else int(stored)
                        except ValueError:
                            pass
                    else:
                        config[v['name']] = stored

        return config

    def set_plugin_config(self, plugin_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and save config values to DB."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
            return {'error': 'Invalid plugin id'}
        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        if not os.path.isdir(plugin_dir):
            return {'error': f'Plugin not found: {plugin_id}'}

        variables = self.get_plugin_variables(plugin_id)
        var_map = {v['name']: v for v in variables}

        clean = {}
        for name, val in values.items():
            if name not in var_map:
                continue
            var_def = var_map[name]
            var_type = var_def.get('type', 'string')
            try:
                if var_type == 'number':
                    clean[name] = float(val) if '.' in str(val) else int(val)
                elif var_type == 'boolean':
                    clean[name] = bool(val)
                else:
                    clean[name] = str(val)
            except (ValueError, TypeError):
                return {'error': f'Invalid value for {var_def.get("label", name)}: expected {var_type}'}

        from models.db import db
        for name, val in clean.items():
            db.set_setting(f'plugin_config:{plugin_id}:{name}', str(val))

        self.reload_plugin(plugin_id)
        return {'success': True, 'config': self.get_plugin_config(plugin_id)}

    # ── Helpers ──

    def _find_manifest(self, directory: str) -> Optional[str]:
        """Find plugin.json at root or one level deep in extracted zip."""
        root_manifest = os.path.join(directory, 'plugin.json')
        if os.path.isfile(root_manifest):
            return root_manifest
        for name in os.listdir(directory):
            sub = os.path.join(directory, name)
            if os.path.isdir(sub):
                sub_manifest = os.path.join(sub, 'plugin.json')
                if os.path.isfile(sub_manifest):
                    return sub_manifest
        return None
