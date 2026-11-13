"""Cursor user-scope rules (#2506) survive install, reinstall, edit and uninstall -g (#3649)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._target_compile_kit import Scenario, output, write, write_package

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_RULES = ("global.mdc", "multi.mdc", "none.mdc", "py.mdc")


def test_cursor_user_scope_rules_lifecycle(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    home = scenario.isolated.home
    rules = home / ".cursor/rules"
    unrelated = rules / "unrelated.mdc"
    write(unrelated, "---\nalwaysApply: true\n---\nMine.\n")
    package = write_package(scenario.isolated.package_root / "pkg")
    project = scenario.isolated.work_root / "project"
    write(project / ".cursor/rules/global.mdc", "project rule\n")
    cwd = scenario.isolated.work_root / "cwd"
    cwd.mkdir()

    scenario.run("install", "-g", str(package), "--target", "cursor", cwd=cwd)
    for name in _RULES:
        assert (rules / name).is_file(), name
    assert "alwaysApply: true" in (rules / "global.mdc").read_text("ascii")
    assert "globs: src/**/*.py" in (rules / "py.mdc").read_text("ascii")
    lock_path = scenario.isolated.config_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    recorded = {path for dep in lock["dependencies"] for path in dep.get("deployed_files") or []}
    assert {f".cursor/rules/{name}" for name in _RULES} <= recorded, recorded
    assert ".cursor/rules/unrelated.mdc" not in recorded

    lock_bytes = lock_path.read_bytes()
    rule_bytes = {name: (rules / name).read_bytes() for name in _RULES}
    scenario.run("install", "-g", cwd=cwd)
    assert lock_path.read_bytes() == lock_bytes
    assert {name: (rules / name).read_bytes() for name in _RULES} == rule_bytes

    # Edited package rule: uninstall -g fails closed, names it and keeps it.
    edited = rules / "py.mdc"
    edited.write_text(edited.read_text("ascii") + "EDIT\n", encoding="ascii")
    failed = scenario.run("uninstall", "-g", str(package), cwd=cwd, expect=1)
    assert "py.mdc" in output(failed)
    assert edited.read_text("ascii").endswith("EDIT\n")

    edited.unlink()
    scenario.run("uninstall", "-g", str(package), cwd=cwd)
    for name in _RULES:
        assert not (rules / name).exists(), name
    assert unrelated.read_text("ascii") == "---\nalwaysApply: true\n---\nMine.\n"
    assert (project / ".cursor/rules/global.mdc").read_text("ascii") == "project rule\n"
