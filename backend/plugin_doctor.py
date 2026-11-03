"""
Plugin hooks for ``evonic doctor``.

A plugin takes part by defining either or both of these in its ``handler.py``:

    def doctor_check(ctx) -> list[Finding]
    def doctor_fix(ctx, findings) -> list[str]

``doctor_check`` runs on every ``evonic doctor`` and reports what it sees. With ``--fix``, ``doctor_fix`` receives
the findings that were marked ``fixable`` and returns one line per change it made. Only enabled plugins are asked.
``ctx`` carries ``plugin_id``, ``config`` (the plugin's saved settings) and ``fix`` (whether this is a fix run).
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from backend.logging_config import get_logger

_logger = get_logger(__name__)

LEVELS = ('ok', 'warn', 'fail')


@dataclass
class Finding:
    level: str
    message: str
    fixable: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # for the plugin's own doctor_fix; never shown

    @classmethod
    def ok(cls, message: str) -> 'Finding':
        return cls('ok', message)

    @classmethod
    def warn(cls, message: str, fixable: bool = False, **data) -> 'Finding':
        return cls('warn', message, fixable, data)

    @classmethod
    def fail(cls, message: str, fixable: bool = False, **data) -> 'Finding':
        return cls('fail', message, fixable, data)


def _enabled_plugins() -> List[tuple]:
    from backend.plugin_manager import plugin_manager
    found = []
    for plugin in plugin_manager.list_plugins():
        if not plugin.get('enabled'):
            continue
        plugin_id = plugin.get('id', '')
        module = plugin_manager._modules.get(plugin_id)
        if module is not None:
            found.append((plugin_id, plugin.get('name') or plugin_id, module,
                          plugin_manager.get_plugin_config(plugin_id)))
    return found


def run_plugin_doctor(fix: bool = False, plugins: Optional[List[tuple]] = None) -> List[dict]:
    """Ask each enabled plugin that has a doctor hook. ``plugins`` (for tests) is a list of
    ``(plugin_id, name, module, config)``. Returns one report per plugin:
    ``{'id', 'name', 'findings': [Finding], 'fixes': [str]}``. A hook that raises becomes a failed finding."""
    reports = []
    for plugin_id, name, module, config in (_enabled_plugins() if plugins is None else plugins):
        check = getattr(module, 'doctor_check', None)
        if not callable(check):
            continue
        ctx = SimpleNamespace(plugin_id=plugin_id, config=config or {}, fix=fix)
        report = {'id': plugin_id, 'name': name, 'findings': [], 'fixes': []}
        try:
            report['findings'] = [f for f in (check(ctx) or []) if isinstance(f, Finding) and f.level in LEVELS]
        except Exception as e:
            _logger.error("doctor_check failed for plugin '%s': %s", plugin_id, e, exc_info=True)
            report['findings'] = [Finding.fail(f"doctor_check raised {type(e).__name__}: {e}")]
        fixable = [f for f in report['findings'] if f.fixable and f.level != 'ok']
        repair = getattr(module, 'doctor_fix', None)
        if fix and fixable and callable(repair):
            try:
                report['fixes'] = [str(line) for line in (repair(ctx, fixable) or [])]
            except Exception as e:
                _logger.error("doctor_fix failed for plugin '%s': %s", plugin_id, e, exc_info=True)
                report['findings'].append(Finding.fail(f"doctor_fix raised {type(e).__name__}: {e}"))
        reports.append(report)
    return reports
