"""Real-CLI lifecycle: uninstall keeps a skill member a surviving package deploys (#4750).

A legacy lockfile can record a whole skill directory for one package while
another package records a single member inside it. Uninstalling the first
package must remove only its own members: the survivor's member stays byte
identical on disk, and the persisted apm.lock.yaml still records it for the
survivor, so the lockfile equals disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.deployment_evidence import seed_receipts
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.skill_selection_world import INSTALL, SkillSelectionWorld, lock_path

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_ALPHA = ".agents/skills/alpha"
_SHARED = f"{_ALPHA}/shared.md"


def _row(lock: dict, name: str) -> dict:
    return next(dep for dep in lock["dependencies"] if dep.get("name") == name)


def test_uninstall_keeps_a_skill_member_a_surviving_package_deploys(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = SkillSelectionWorld.create(tmp_path / "w", (str(apm_binary_path),))
    departing = world.publish("pkga", skills=("alpha",)).slug
    survivor = world.publish("pkgb", skills=("beta",)).slug
    project = world.project("consumer", dependencies=[departing, survivor])
    with world.serve() as env:
        world.run(INSTALL, cwd=project, env=env, scenario_id="4750-install")

    # The survivor deploys a member inside the departing package's skill directory,
    # with the deployment receipt its install would have written.
    shared = project / _SHARED
    shared.write_bytes(b"# deployed by the surviving package\n")
    monkeypatch.setenv("APM_HOME", str(world.isolated.config_root))
    lock = load_yaml(lock_path(project))
    survivor_key = f"{_row(lock, 'pkgb')['repo_url'].rstrip('/').split('github.com/')[-1]}"
    seed_receipts(project, [shared], target="copilot", owner=survivor_key)
    own = {
        path: value
        for path, value in _row(lock, "pkga").get("deployed_file_hashes", {}).items()
        if path.startswith(f"{_ALPHA}/")
    }
    _row(lock, "pkga")["deployed_files"] = [_ALPHA]
    _row(lock, "pkga")["deployed_file_hashes"] = {**own, _SHARED: compute_file_hash(shared)}
    beta = _row(lock, "pkgb")
    beta["deployed_files"] = sorted([*beta["deployed_files"], _SHARED])
    beta.setdefault("deployed_file_hashes", {})[_SHARED] = compute_file_hash(shared)
    lock.pop("deployments", None)
    dump_yaml(lock, lock_path(project))

    with world.serve() as env:
        result = world.run(
            ("uninstall", departing), cwd=project, env=env, expect=None, scenario_id="4750-rm"
        )

    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    assert shared.read_bytes() == b"# deployed by the surviving package\n"
    assert not (project / _ALPHA / "SKILL.md").exists()
    assert (project / ".agents/skills/beta/SKILL.md").is_file()
    after = load_yaml(lock_path(project))
    assert all(dep.get("name") != "pkga" for dep in after["dependencies"])
    assert _SHARED in _row(after, "pkgb")["deployed_files"]
    assert os.path.isfile(project / _SHARED)
