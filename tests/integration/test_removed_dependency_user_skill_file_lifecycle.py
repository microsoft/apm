"""Installed-CLI contract: removing a dependency keeps a user's file in its skill dir.

When a removed dependency's skill directory also holds a user's own file,
``apm install`` deletes APM's members, keeps the directory with the user's
file, and drops the dependency from ``apm.lock.yaml``. Install, ``deps
list`` and ``audit --ci`` then agree exactly as for a plain removal (#3677).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.integration._fault_injection_kit import build_world, evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_SKILL = ".claude/skills/alpha-skill-0"


def _locked_names(project: Path) -> set[str]:
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    return {entry["name"] for entry in lock["dependencies"]}


def test_removed_dependency_releases_skill_dir_holding_user_file(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(tmp_path, apm_binary_path, targets=("claude",))
    user_file = world.project / _SKILL / "my-notes.md"
    user_file.write_text("my notes\n", encoding="ascii")
    manifest = world.project / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="ascii").replace("    - ./packages/alpha\n", ""),
        encoding="ascii",
    )

    result = world.install()

    assert result.returncode == 0, evidence(result)
    assert user_file.read_text(encoding="ascii") == "my notes\n"
    assert not (world.project / _SKILL / "SKILL.md").exists()
    assert "alpha" not in _locked_names(world.project)
    lock = world.lockfile_bytes()
    rerun = world.install()
    assert rerun.returncode == 0, evidence(rerun)
    assert world.lockfile_bytes() == lock

    audit = world.apm("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, evidence(audit)
    listing = world.apm("deps", "list")
    assert listing.returncode == 0, evidence(listing)
    assert "orphaned" in listing.stdout, evidence(listing)

    prune = world.apm("prune")
    assert prune.returncode == 0, evidence(prune)
    assert not (world.project / "apm_modules/_local/alpha").exists()
    assert user_file.read_text(encoding="ascii") == "my notes\n"
    final = world.apm("audit", "--ci", "--no-policy")
    assert final.returncode == 0, evidence(final)
