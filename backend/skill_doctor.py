"""
Skill hooks for ``evonic doctor``.

A skill takes part by shipping ``backend/doctor.py`` exporting either or both:

    def doctor_check(ctx) -> list[Finding]
    def doctor_fix(ctx, findings) -> list[str]

``doctor_check`` runs on every ``evonic doctor`` and reports what it sees. With
``--fix``, ``doctor_fix`` receives the findings marked ``fixable`` and returns
one line per change it made. Only enabled skills are asked.

``ctx`` carries ``skill_id``, ``name``, ``dir`` (the skill's directory),
``config`` (its saved settings) and ``fix`` (whether this is a fix run).

This mirrors :mod:`backend.plugin_doctor` so skills can diagnose and repair
their own external dependencies (binaries, local services, caches) instead of
relying only on the generic ``requirements.binaries`` check.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from backend.logging_config import get_logger
from backend.plugin_doctor import LEVELS, Finding

_logger = get_logger(__name__)

DOCTOR_FILE = "doctor.py"


def _skill_dir(skill_id: str) -> str:
    from backend.skills_manager import SKILLS_DIR
    return os.path.join(SKILLS_DIR, skill_id)


def _load_hook_module(skill_id: str, skill_dir: str) -> tuple:
    """Import ``<skill_dir>/backend/doctor.py``.

    Returns ``(module, error)``. ``module`` is None when the skill ships no
    hook; ``error`` is set when the hook exists but could not be imported so
    the doctor can report the failure instead of silently skipping it.
    """
    path = os.path.join(skill_dir, "backend", DOCTOR_FILE)
    if not os.path.isfile(path):
        return None, None
    mod_name = f"evonic_skill_doctor_{skill_id}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module, None
    except Exception as e:  # noqa: BLE001 — a broken hook must not break doctor
        sys.modules.pop(mod_name, None)
        _logger.error("failed to import doctor hook for skill '%s': %s", skill_id, e, exc_info=True)
        return None, f"{type(e).__name__}: {e}"


def _enabled_skills() -> List[tuple]:
    """Return ``(skill_id, name, dir, config)`` for every enabled skill."""
    from backend.skills_manager import SKILLS_DIR, SkillsManager

    found: List[tuple] = []
    try:
        manager = SkillsManager()
        names = sorted(os.listdir(SKILLS_DIR)) if os.path.isdir(SKILLS_DIR) else []
    except Exception as e:  # noqa: BLE001 — best-effort, like the plugin scan
        _logger.error("cannot enumerate skills: %s", e)
        return found

    for skill_id in names:
        skill_dir = os.path.join(SKILLS_DIR, skill_id)
        if not os.path.isfile(os.path.join(skill_dir, "skill.json")):
            continue
        try:
            if not manager.is_skill_enabled(skill_id):
                continue
            config = manager.get_skill_config(skill_id) or {}
        except Exception as e:  # noqa: BLE001
            _logger.error("cannot read skill '%s': %s", skill_id, e)
            continue
        found.append((skill_id, manager.get_skill_name(skill_id) or skill_id, skill_dir, config))
    return found


def run_skill_doctor(fix: bool = False, skills: Optional[List[tuple]] = None) -> List[dict]:
    """Ask each enabled skill that has a doctor hook.

    ``skills`` (for tests) is a list of ``(skill_id, name, dir, config)``. A
    module may be supplied instead of a directory by passing the module object
    as the third element. Returns one report per skill:
    ``{'id', 'name', 'findings': [Finding], 'fixes': [str]}``. A hook that
    raises becomes a failed finding.
    """
    reports: List[dict] = []
    candidates = _enabled_skills() if skills is None else skills

    for skill_id, name, location, config in candidates:
        module: Any = None
        load_error: Optional[str] = None
        skill_dir: Optional[str] = None

        if hasattr(location, "doctor_check") or hasattr(location, "doctor_fix"):
            module = location
        else:
            skill_dir = str(location)
            module, load_error = _load_hook_module(skill_id, skill_dir)

        if load_error:
            reports.append({
                "id": skill_id,
                "name": name,
                "findings": [Finding.fail(f"doctor hook could not be loaded ({load_error})")],
                "fixes": [],
            })
            continue
        if module is None:
            continue

        check = getattr(module, "doctor_check", None)
        repair = getattr(module, "doctor_fix", None)
        if not callable(check) and not callable(repair):
            continue

        ctx = SimpleNamespace(skill_id=skill_id, name=name, dir=skill_dir,
                              config=config or {}, fix=fix)
        report = {"id": skill_id, "name": name, "findings": [], "fixes": []}

        if callable(check):
            try:
                raw = check(ctx) or []
                report["findings"] = [
                    f for f in raw if isinstance(f, Finding) and f.level in LEVELS
                ]
            except Exception as e:  # noqa: BLE001
                _logger.error("doctor_check failed for skill '%s': %s", skill_id, e, exc_info=True)
                report["findings"] = [Finding.fail(f"doctor_check raised {type(e).__name__}: {e}")]

        fixable = [f for f in report["findings"] if f.fixable and f.level != "ok"]
        if fix and fixable and callable(repair):
            try:
                report["fixes"] = [str(line) for line in (repair(ctx, fixable) or [])]
            except Exception as e:  # noqa: BLE001
                _logger.error("doctor_fix failed for skill '%s': %s", skill_id, e, exc_info=True)
                report["findings"].append(Finding.fail(f"doctor_fix raised {type(e).__name__}: {e}"))

        reports.append(report)

    return reports
