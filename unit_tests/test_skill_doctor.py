"""Tests for skill-level ``evonic doctor`` hooks (backend/skill_doctor.py)."""

import sys
import types

from backend.plugin_doctor import Finding
from backend.skill_doctor import run_skill_doctor


def _module(check=None, fix=None):
    mod = types.ModuleType("fake_skill_doctor_module")
    if check is not None:
        mod.doctor_check = check
    if fix is not None:
        mod.doctor_fix = fix
    return mod


def test_findings_and_fixes_are_reported():
    def check(ctx):
        assert ctx.skill_id == "demo"
        assert ctx.config == {"k": "v"}
        return [Finding.ok("all good"), Finding.warn("needs work", fixable=True)]

    def fix(ctx, findings):
        assert [f.message for f in findings] == ["needs work"]
        return ["did the work"]

    reports = run_skill_doctor(fix=True, skills=[("demo", "Demo", _module(check, fix), {"k": "v"})])
    assert len(reports) == 1
    assert [f.level for f in reports[0]["findings"]] == ["ok", "warn"]
    assert reports[0]["fixes"] == ["did the work"]


def test_fix_hook_is_skipped_without_fix_flag():
    called = []

    def check(ctx):
        return [Finding.fail("broken", fixable=True)]

    def fix(ctx, findings):
        called.append(True)
        return ["fixed"]

    reports = run_skill_doctor(fix=False, skills=[("demo", "Demo", _module(check, fix), {})])
    assert called == []
    assert reports[0]["fixes"] == []
    assert reports[0]["findings"][0].fixable is True


def test_non_fixable_findings_are_not_sent_to_fix():
    def check(ctx):
        return [Finding.warn("informational")]

    def fix(ctx, findings):
        raise AssertionError("must not be called")

    reports = run_skill_doctor(fix=True, skills=[("demo", "Demo", _module(check, fix), {})])
    assert reports[0]["fixes"] == []


def test_raising_check_becomes_failed_finding():
    def check(ctx):
        raise ValueError("boom")

    reports = run_skill_doctor(skills=[("demo", "Demo", _module(check), {})])
    assert reports[0]["findings"][0].level == "fail"
    assert "boom" in reports[0]["findings"][0].message


def test_raising_fix_becomes_failed_finding():
    def check(ctx):
        return [Finding.fail("broken", fixable=True)]

    def fix(ctx, findings):
        raise RuntimeError("no")

    reports = run_skill_doctor(fix=True, skills=[("demo", "Demo", _module(check, fix), {})])
    assert any(f.level == "fail" and "no" in f.message for f in reports[0]["findings"])


def test_skill_without_hook_is_skipped():
    reports = run_skill_doctor(skills=[("demo", "Demo", _module(), {})])
    assert reports == []


def test_directory_hook_is_imported(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "doctor.py").write_text(
        "from backend.plugin_doctor import Finding\n"
        "def doctor_check(ctx):\n"
        "    return [Finding.ok('loaded from dir')]\n",
        encoding="utf-8",
    )
    reports = run_skill_doctor(skills=[("demo", "Demo", str(tmp_path), {})])
    assert reports[0]["findings"][0].message == "loaded from dir"
    assert "evonic_skill_doctor_demo" in sys.modules


def test_broken_hook_reports_failure(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "doctor.py").write_text("raise ImportError('missing dep')\n", encoding="utf-8")
    reports = run_skill_doctor(skills=[("demo", "Demo", str(tmp_path), {})])
    assert reports[0]["findings"][0].level == "fail"
    assert "could not be loaded" in reports[0]["findings"][0].message
