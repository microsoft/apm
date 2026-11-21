"""User-scope root primitives are recorded in the user lockfile (#3772).

PROMISE: ownership.preserve_unowned + idempotency -- ``apm install -g`` records
the files it deploys from user-root primitives (``~/.apm/agents``) as
self-entry claims in ``~/.apm/apm.lock.yaml``, as project scope does for
root-local primitives, so a later ``install -g`` updates APM-owned files when
the source changes while files APM did not deploy stay protected
(source: issue #3772 scope record).
CELL: OP=install SRC=root-local PRIM=agent SCOPE=user LOCK=absent|present
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import load_yaml
from tests.integration._harness_output_kit import HarnessScene, describe, flat
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I7"),
    pytest.mark.requires_apm_binary,
]

_DEPLOYED = ".copilot/agents/research.agent.md"
_NO_CHANGES = "No changes"


def _agent(body: str) -> str:
    return f"---\nname: research\ndescription: Research helper\n---\n\n{body}\n"


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    root = result.isolated.config_root
    (root / "apm.yml").write_text(
        "name: user-profile\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="utf-8"
    )
    (root / "agents").mkdir()
    (result.home / ".copilot").mkdir()
    try:
        yield result
    finally:
        result.close()


def _source(scene: HarnessScene) -> Path:
    return scene.isolated.config_root / "agents" / "research.agent.md"


def _install(scene: HarnessScene, *, expect: int = 0) -> CommandResult:
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir(exist_ok=True)
    result = scene.runner.run(("install", "-g", "--target", "copilot"), cwd=cwd, env=scene.env)
    assert result.returncode == expect, describe(result)
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), describe(result)
    return result


def _lock(scene: HarnessScene) -> dict:
    path = scene.isolated.config_root / "apm.lock.yaml"
    assert path.is_file(), sorted(p.name for p in scene.isolated.config_root.iterdir())
    return load_yaml(path)


def _assert_claimed(scene: HarnessScene) -> None:
    """I1: the user lock claims the deployed agent with its on-disk hash."""
    lock = _lock(scene)
    deployed = scene.home / _DEPLOYED
    assert _DEPLOYED in (lock.get("local_deployed_files") or []), lock
    assert lock["local_deployed_file_hashes"][_DEPLOYED] == compute_file_hash(deployed), lock


def test_install_g_with_only_user_root_agents_writes_the_user_lockfile(
    scene: HarnessScene,
) -> None:
    """Cases 1 and 2: the lock claims the agent, and a source edit is deployed."""
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")
    _install(scene)
    assert (scene.home / _DEPLOYED).read_text(encoding="utf-8").endswith("Version one.\n")
    _assert_claimed(scene)

    _source(scene).write_text(_agent("Version two."), encoding="utf-8")
    edited = _install(scene)
    assert _NO_CHANGES not in flat(edited.stdout + edited.stderr), describe(edited)
    assert "skipped" not in flat(edited.stdout + edited.stderr), describe(edited)
    assert (scene.home / _DEPLOYED).read_text(encoding="utf-8").endswith("Version two.\n")
    _assert_claimed(scene)

    # I2: a converged reinstall changes no bytes.
    lock_bytes = (scene.isolated.config_root / "apm.lock.yaml").read_bytes()
    agent_bytes = (scene.home / _DEPLOYED).read_bytes()
    _install(scene)
    assert (scene.isolated.config_root / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (scene.home / _DEPLOYED).read_bytes() == agent_bytes


def _as_037_state(scene: HarnessScene) -> None:
    """Rewind to what 0.37 left: the receipt bound to no lock claim, and no user lock."""
    states = list((scene.isolated.config_root / "deployment-receipts").glob("*.json"))
    deployed = (scene.home / _DEPLOYED).resolve().as_posix()
    receipted = False
    for state in states:
        data = json.loads(state.read_text(encoding="utf-8"))
        for key, physical in data["destinations"].items():
            if physical == deployed:
                data["bindings"][key] = [".", "[]"]
                receipted = True
        state.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert receipted, states
    (scene.isolated.config_root / "apm.lock.yaml").unlink()


@pytest.mark.parametrize("edit_first", [True, False], ids=["edited-source", "unchanged-source"])
def test_first_upgraded_install_migrates_a_receipt_without_a_lock(
    scene: HarnessScene, edit_first: bool
) -> None:
    """Case 3: from a 0.37 user scope (receipt, no lock), source edits deploy, no manual step."""
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")
    _install(scene)
    _as_037_state(scene)
    deployed = scene.home / _DEPLOYED

    if not edit_first:
        # The upgrade adopts the unchanged file; the migrated claim must outlive it.
        adopted = _install(scene)
        assert "skipped" not in flat(adopted.stdout + adopted.stderr), describe(adopted)
        assert deployed.read_text(encoding="utf-8").endswith("Version one.\n")
        _assert_claimed(scene)

    _source(scene).write_text(_agent("Version two."), encoding="utf-8")
    upgraded = _install(scene)

    assert _NO_CHANGES not in flat(upgraded.stdout + upgraded.stderr), describe(upgraded)
    assert deployed.read_text(encoding="utf-8").endswith("Version two.\n")
    _assert_claimed(scene)


def test_hand_edited_deployed_agent_is_kept_and_named(scene: HarnessScene) -> None:
    """Case 4: an edit to the deployed file is kept, and one line names it."""
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")
    _install(scene)
    deployed = scene.home / _DEPLOYED
    edited = deployed.read_bytes() + b"Hand edit.\n"
    deployed.write_bytes(edited)
    _source(scene).write_text(_agent("Version two."), encoding="utf-8")

    result = _install(scene, expect=1)

    assert deployed.read_bytes() == edited
    named = [line for line in (result.stdout + result.stderr).splitlines() if _DEPLOYED in line]
    assert len(named) == 1, describe(result)


def test_unreceipted_pre_existing_file_is_still_skipped(scene: HarnessScene) -> None:
    """Case 5: a file APM never deployed is not overwritten and is not claimed."""
    deployed = scene.home / _DEPLOYED
    deployed.parent.mkdir(parents=True)
    deployed.write_text("User authored.\n", encoding="utf-8")
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")

    result = _install(scene)

    assert deployed.read_text(encoding="utf-8") == "User authored.\n"
    assert re.search(r"\b1 file skipped\b", flat(result.stdout + result.stderr)), describe(result)
    lock_path = scene.isolated.config_root / "apm.lock.yaml"
    lock = load_yaml(lock_path) if lock_path.is_file() else {}
    assert _DEPLOYED not in (lock.get("local_deployed_files") or []), lock
