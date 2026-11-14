"""Tests for local-binary resolution in cli/requirements_check.py (search_paths)."""

import os

from cli import requirements_check as rc


def _fake_binary(tmp_path, name="lp_fake_bin", version="2.3.4"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _spec(name="lp_fake_bin", version="1.0.0", search_paths=("bin",)):
    return {
        "name": name,
        "version": version,
        "version_command": f"{name} --version",
        "search_paths": list(search_paths),
    }


def test_search_path_finds_skill_local_binary(tmp_path):
    _fake_binary(tmp_path)
    result = rc.check_binary(_spec(), str(tmp_path))
    assert result["status"] == "ok"
    assert result["current"] == "2.3.4"
    assert result["path"].endswith("bin/lp_fake_bin")


def test_search_path_ignored_without_source_dir(tmp_path):
    _fake_binary(tmp_path)
    assert rc.check_binary(_spec())["status"] == "missing"


def test_missing_binary_is_missing(tmp_path):
    (tmp_path / "bin").mkdir()
    assert rc.check_binary(_spec(), str(tmp_path))["status"] == "missing"


def test_outdated_local_binary_is_reported(tmp_path):
    _fake_binary(tmp_path, version="0.9.0")
    result = rc.check_binary(_spec(version="1.0.0"), str(tmp_path))
    assert result["status"] == "outdated"
    assert result["current"] == "0.9.0"


def test_non_executable_file_is_ignored(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "lp_fake_bin"
    path.write_text("#!/bin/sh\necho 9.9.9\n", encoding="utf-8")
    os.chmod(path, 0o644)
    assert rc.check_binary(_spec(), str(tmp_path))["status"] == "missing"


def test_absolute_search_path_is_supported(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    path = target / "lp_fake_bin"
    path.write_text("#!/bin/sh\necho 3.1.0\n", encoding="utf-8")
    os.chmod(path, 0o755)
    result = rc.check_binary(_spec(search_paths=[str(target)]), None)
    assert result["status"] == "ok"
    assert result["current"] == "3.1.0"


def test_no_search_paths_keeps_path_only_behaviour(monkeypatch, tmp_path):
    _fake_binary(tmp_path)
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    assert rc.check_binary({"name": "lp_absent_bin"})["status"] == "missing"
