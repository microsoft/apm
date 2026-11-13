"""Uninstall without a lockfile never removes skill directories.

Only lockfile ownership proves which skill directories APM deployed. When
``apm.lock.yaml`` is missing, ``apm uninstall`` (project and ``-g``) keeps
every skill directory, including user-authored ones, and says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._lockfile_chain_kit import build_kit, output, skill, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_NOTICE = "Skipped skill directory cleanup: no lockfile records which skills APM deployed."


def _user_skill(skills_root: Path) -> Path:
    path = skills_root / "user-authored" / "SKILL.md"
    write(path, skill("user-authored", "Written by hand."))
    return path


def test_project_uninstall_without_lockfile_keeps_skill_directories(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_kit(tmp_path, apm_binary_path, targets=("claude",))
    user_skill = _user_skill(kit.project / ".claude/skills")
    deployed = kit.project / ".claude/skills/alpha/SKILL.md"
    assert deployed.is_file()
    (kit.project / "apm.lock.yaml").unlink()

    result = kit.run("uninstall", "test/kit")

    assert _NOTICE in output(result), output(result)
    assert user_skill.is_file()
    assert deployed.is_file()


def test_global_uninstall_without_lockfile_keeps_skill_directories(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_kit(tmp_path, apm_binary_path, targets=("claude",))
    installed = kit.run("install", "-g", "--no-policy", "-t", "claude", "test/kit#^1.0.0")
    assert installed.returncode == 0, output(installed)
    home = kit.isolated.home
    user_skill = _user_skill(home / ".claude/skills")
    (kit.isolated.config_root / "apm.lock.yaml").unlink()

    result = kit.run("uninstall", "-g", "test/kit")

    assert _NOTICE in output(result), output(result)
    assert user_skill.is_file()
