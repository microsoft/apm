"""Installed-CLI proofs: a refused uninstall changes nothing (#4185).

``apm uninstall`` validates what it will need before its first write: the
survivors' org-policy trust and every recorded path. A refusal leaves
apm.yml, apm.lock.yaml and every deployed file byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import build_world, evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_policy_refusal_mid_uninstall_changes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Survivor trust is resolved before any write, so a blocking policy refuses cleanly."""
    world = build_world(tmp_path, apm_binary_path, packages=("alpha", "beta"))
    manifest = world.project / "apm.yml"
    # No git remote: an enforceable org policy cannot be resolved, and the
    # project asks that to block.
    manifest.write_text(
        manifest.read_text(encoding="ascii") + "policy:\n  fetch_failure_default: block\n",
        encoding="ascii",
    )
    before = world.snapshot(".")

    refused = world.apm("uninstall", "./packages/alpha")

    assert refused.returncode == 1, evidence(refused)
    assert "Blocked by org policy" in refused.stdout + refused.stderr, evidence(refused)
    assert "no APM writes were made" in " ".join(refused.stdout.split()), evidence(refused)
    diff = before.diff(world.snapshot("."))
    assert not (diff.added | diff.removed | diff.changed), diff
    world.assert_user_file_intact()


def test_uninstall_validates_every_recorded_path_before_changing_state(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A recorded path outside the project stops uninstall before any write."""
    from apm_cli.utils.yaml_io import dump_yaml, load_yaml

    world = build_world(tmp_path, apm_binary_path, packages=("alpha", "beta"))
    outside = world.isolated.root / "outside.md"
    outside.write_text("not APM's\n", encoding="ascii")
    lock_path = world.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    escape = "../../outside.md"
    owner = next(dep for dep in lock["dependencies"] if "alpha" in dep["local_path"])
    owner["deployed_files"].append(escape)
    lock["deployments"].append(
        {
            "kind": "project-relative",
            "target": "copilot",
            "value": escape,
            "runtime": None,
            "scope": "project",
            "owners": [owner["local_path"]],
            "active_owner": owner["local_path"],
            "content_hash": "sha256:" + "0" * 64,
        }
    )
    dump_yaml(lock, lock_path)
    before = world.snapshot(".")

    refused = world.apm("uninstall", "./packages/alpha")

    assert refused.returncode == 1, evidence(refused)
    assert escape in refused.stdout + refused.stderr, evidence(refused)
    diff = before.diff(world.snapshot("."))
    assert not (diff.added | diff.removed | diff.changed), diff
    assert outside.read_text(encoding="ascii") == "not APM's\n"
    world.assert_user_file_intact()


def test_uninstall_releases_a_skill_directory_holding_user_content(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A user file in a deployed skill directory is kept and the uninstall succeeds (#4162)."""
    world = build_world(tmp_path, apm_binary_path, packages=("alpha", "beta"))
    skill_dirs = sorted(path.parent for path in world.project.glob("**/alpha-skill-0/SKILL.md"))
    assert skill_dirs, "fixture must deploy the alpha skill"
    notes = skill_dirs[0] / "NOTES.md"
    notes.write_text("my notes\n", encoding="ascii")

    removed = world.apm("uninstall", "./packages/alpha")

    assert removed.returncode == 0, evidence(removed)
    assert notes.read_text(encoding="ascii") == "my notes\n"
    assert not (skill_dirs[0] / "SKILL.md").exists()
    assert "alpha" not in (world.project / "apm.yml").read_text(encoding="ascii")
    assert "packages/alpha" not in (world.project / "apm.lock.yaml").read_text(encoding="ascii")
    assert world.apm("audit", "--ci").returncode == 0
    world.assert_user_file_intact()
