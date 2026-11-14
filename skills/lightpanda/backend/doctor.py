"""Doctor hooks for the Lightpanda skill (`evonic doctor` / `--fix`).

Loaded by ``backend/skill_doctor.py``. Checks the local binary, its runtime
compatibility (glibc loader), the auto-started MCP server and the skill's
safety settings; ``--fix`` reinstalls a missing/outdated binary and clears a
stale server record.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from backend.plugin_doctor import Finding

_SKILL_DIR = Path(__file__).resolve().parent.parent


def _helpers(skill_dir: Path | None = None):
    """Import the skill's shared tool helper module outside the tools package."""
    base = Path(skill_dir) if skill_dir else _SKILL_DIR
    path = base / "backend" / "tools" / "_lp.py"
    spec = importlib.util.spec_from_file_location("evonic_lightpanda_lp", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("evonic_lightpanda_lp", module)
    spec.loader.exec_module(module)
    return module


def _install_script(skill_dir: Path) -> Path:
    return skill_dir / "scripts" / "install.sh"


def doctor_check(ctx):
    """Report binary, runtime, server and safety state."""
    findings = []
    skill_dir = Path(ctx.dir) if getattr(ctx, "dir", None) else _SKILL_DIR
    lp = _helpers(skill_dir)
    cfg = getattr(ctx, "config", None) or {}

    binary = lp.resolve_binary(cfg)
    if not binary:
        findings.append(Finding.fail(
            "lightpanda binary not found — interactive browsing is unavailable",
            fixable=True, action="install"))
    else:
        version = lp.binary_version(binary, cfg)
        source = lp.binary_source(binary, cfg)
        if not version:
            findings.append(Finding.fail(
                f"lightpanda at {binary} does not execute — it likely needs a newer glibc "
                "(musl/Alpine is unsupported). Rebuild on a glibc host.",
                fixable=False))
        else:
            findings.append(Finding.ok(f"lightpanda {version} ({source}) at {binary}"))
            try:
                from cli.requirements_check import parse_semver
                if parse_semver(version) < parse_semver(lp.MIN_VERSION):
                    findings.append(Finding.warn(
                        f"lightpanda {version} is older than the supported "
                        f"{lp.MIN_VERSION} — upgrade recommended",
                        fixable=True, action="install"))
            except Exception:  # noqa: BLE001 — version comparison is advisory
                pass

    # Server: only report; never start one from the doctor.
    state = lp.load_state()
    pid = state.get("pid")
    if lp.server_alive():
        findings.append(Finding.ok(
            f"MCP server running on {lp.host_of(cfg)}:{lp.port_of(cfg)} (pid {pid})"))
    elif pid:
        try:
            os.kill(int(pid), 0)
            alive = True
        except (OSError, ValueError):
            alive = False
        if alive:
            findings.append(Finding.warn(
                f"MCP server pid {pid} is alive but not answering on port "
                f"{lp.port_of(cfg)} — it will be restarted on next use"))
        else:
            findings.append(Finding.warn(
                f"stale MCP server record (pid {pid} is gone)",
                fixable=True, action="clean_state"))
    else:
        findings.append(Finding.ok("MCP server not running (starts on first use)"))

    if lp.port_open(lp.host_of(cfg), lp.port_of(cfg)) and not lp.server_alive():
        findings.append(Finding.warn(
            f"port {lp.port_of(cfg)} is occupied by another process; set a different "
            "MCP port in the skill settings"))

    if not lp.block_private(cfg):
        findings.append(Finding.warn(
            "SSRF guard is off — requests to private/internal addresses are allowed"))
    if not lp.obey_robots(cfg):
        findings.append(Finding.ok("robots.txt is not enforced (opt-in for crawls)"))

    try:
        lp.state_dir()
    except OSError as e:
        findings.append(Finding.fail(f"state directory is not writable: {e}"))

    script = _install_script(skill_dir)
    if not os.access(script, os.X_OK):
        findings.append(Finding.warn(f"installer is not executable: {script}"))
    return findings


def doctor_fix(ctx, findings):
    """Repair the fixable findings: reinstall the binary, clear stale state."""
    fixes = []
    skill_dir = Path(ctx.dir) if getattr(ctx, "dir", None) else _SKILL_DIR
    actions = {f.data.get("action") for f in findings if getattr(f, "data", None)}

    if "clean_state" in actions:
        try:
            lp = _helpers(skill_dir)
            lp.stop_server()
            lp.save_state({})
            fixes.append("Cleared stale Lightpanda server state")
        except Exception as e:  # noqa: BLE001
            fixes.append(f"Could not clear server state: {type(e).__name__}: {e}")

    if "install" in actions:
        script = _install_script(skill_dir)
        if not script.is_file():
            fixes.append(f"Installer missing: {script}")
            return fixes
        try:
            proc = subprocess.run(["bash", str(script)], cwd=str(skill_dir),
                                  capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            fixes.append(f"Installer failed to start: {type(e).__name__}: {e}")
            return fixes

        output = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        lines = [ln.strip() for ln in output if ln.strip()]
        # Prefer the installer's own summary line over curl/progress noise.
        summary = next((ln for ln in reversed(lines) if ln.startswith("[install_lightpanda]")),
                       lines[-1] if lines else "(no output)")
        if proc.returncode == 0:
            fixes.append(f"lightpanda — {summary}")
        else:
            fixes.append(f"Installer exited {proc.returncode} — {summary}")

    return fixes
