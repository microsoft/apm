"""Every drift remediation clears the drift it reports (#4090).

Each proof induces one drift kind on an installed project, observes ``apm
audit`` (drift text) and, where the entry is structured there, ``apm audit
--ci --format json`` (the ``drift`` check) print the entry's advice, does the
entry's ``before`` step, runs exactly the advised command through the same
binary, and observes the drift gone. Together they run the advice for every
drift kind: modified, unintegrated, orphaned, unrecorded and
unmanaged-conflict.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import _instruction
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.remediation_proof import (
    ProofWorld,
    advised,
    advised_json,
    check,
    flat,
    installed_world,
    run_advised,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

PROVES = (
    "drift-resync",
    "drift-unrecorded",
    "drift-restore-edited",
    "drift-restore-edited-force",
    "drift-restore-bundle-edited",
    "export-local-edits",
    "adopt-new-rendering",
    "drift-resync-after-conflict",
)

_INSTRUCTION = ".github/instructions/proof.instructions.md"
_LOCAL_INSTRUCTION = ".github/instructions/local.instructions.md"
_SKILL = ".agents/skills/proof-skill/SKILL.md"
_EDIT = "local edit\n"
_EDITED_SUBJECT = "the modified files"


def _audit_text(world: ProofWorld, scenario_id: str) -> str:
    result = world.run(("audit", "--no-policy"), scenario_id=scenario_id)
    return result.stdout + result.stderr


def _drift(world: ProofWorld, scenario_id: str) -> tuple[dict[str, object], set[tuple[str, str]]]:
    """Return the audit JSON payload and its drift findings as ``(path, kind)``."""
    payload = world.audit_ci(scenario_id)
    findings = {(item["path"], item["kind"]) for item in payload["drift"]["drift"]}
    return payload, findings


def _assert_drift_cleared(world: ProofWorld, scenario_id: str) -> None:
    payload, findings = _drift(world, scenario_id)
    assert findings == set(), payload
    assert check(payload, "drift")["passed"] is True, payload
    assert payload["passed"] is True, payload
    assert "No drift detected" in _audit_text(world, f"{scenario_id}-text")


def _shows_before(remediation_before: str | None, output: str) -> None:
    assert remediation_before, "entry has no before step"
    assert flat(remediation_before).lower() in flat(output).lower(), output


def _edit(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8") + _EDIT, encoding="utf-8")


def _lockfile(world: ProofWorld) -> dict[str, object]:
    return load_yaml(world.root / "apm.lock.yaml")


def _deployed_files(world: ProofWorld) -> list[str]:
    (dependency,) = _lockfile(world)["dependencies"]
    return dependency["deployed_files"]


# -- drift-resync: unintegrated and orphaned ---------------------------------


def _unintegrated(tmp_path: Path, apm_binary_path: Path) -> tuple[ProofWorld, str]:
    # Keep one deployed primitive: plain 'apm audit' skips drift when none remain.
    world = installed_world(tmp_path, apm_binary_path, skill="proof-skill")
    (world.root / _INSTRUCTION).unlink()
    return world, _INSTRUCTION


def _orphaned(tmp_path: Path, apm_binary_path: Path) -> tuple[ProofWorld, str]:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    source = world.scenario.consumers.add_instruction(
        world.consumer, "local", _instruction("local")
    )
    world.install(scenario_id="orphaned-install-local")
    assert (world.root / _LOCAL_INSTRUCTION).is_file()
    source.unlink()
    return world, _LOCAL_INSTRUCTION


_RESYNC_KINDS: dict[str, Callable[[Path, Path], tuple[ProofWorld, str]]] = {
    "unintegrated": _unintegrated,
    "orphaned": _orphaned,
}


@pytest.mark.parametrize("kind", sorted(_RESYNC_KINDS), ids=lambda kind: f"drift-resync-{kind}")
def test_drift_resync_clears_kind(tmp_path: Path, apm_binary_path: Path, kind: str) -> None:
    world, path = _RESYNC_KINDS[kind](tmp_path, apm_binary_path)

    payload, findings = _drift(world, f"resync-{kind}-reported")
    assert findings == {(path, kind)}, payload
    remediation = advised("drift-resync", _audit_text(world, f"resync-{kind}-text"))
    assert advised_json("drift-resync", check(payload, "drift")) == remediation

    run_advised(world, remediation)
    _assert_drift_cleared(world, f"resync-{kind}-cleared")
    assert (world.root / path).exists() is (kind == "unintegrated")


# -- drift-unrecorded ---------------------------------------------------------


def _drop_claim(world: ProofWorld, path: str) -> None:
    lock = _lockfile(world)
    (dependency,) = lock["dependencies"]
    dependency["deployed_files"].remove(path)
    dependency["deployed_file_hashes"].pop(path)
    lock["deployments"] = [row for row in lock["deployments"] if row["value"] != path]
    dump_yaml(lock, world.root / "apm.lock.yaml")


def test_drift_unrecorded(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    _drop_claim(world, _INSTRUCTION)
    assert _INSTRUCTION not in _deployed_files(world)

    payload, findings = _drift(world, "unrecorded-reported")
    assert findings == {(_INSTRUCTION, "unrecorded")}, payload
    remediation = advised("drift-unrecorded", _audit_text(world, "unrecorded-text"))
    assert advised_json("drift-unrecorded", check(payload, "drift")) == remediation

    run_advised(world, remediation)
    assert _INSTRUCTION in _deployed_files(world)
    _assert_drift_cleared(world, "unrecorded-cleared")


# -- modified: restore edited managed files ----------------------------------


def _edited_skill_world(tmp_path: Path, apm_binary_path: Path) -> tuple[ProofWorld, str, str]:
    """Edit a deployed skill file; return the world, its locked text and audit text."""
    world = installed_world(tmp_path, apm_binary_path, skill="proof-skill")
    skill = world.root / _SKILL
    locked = skill.read_text(encoding="utf-8")
    _edit(skill)
    payload, findings = _drift(world, "edited-reported")
    assert findings == {(_SKILL, "modified")}, payload
    text = _audit_text(world, "edited-text")
    restore = advised("drift-restore-edited", text, subject=_EDITED_SUBJECT)
    assert advised_json("drift-restore-edited", check(payload, "drift")) == restore
    return world, locked, text


def test_drift_restore_edited(tmp_path: Path, apm_binary_path: Path) -> None:
    world, locked, text = _edited_skill_world(tmp_path, apm_binary_path)
    remediation = advised("drift-restore-edited", text, subject=_EDITED_SUBJECT)
    _shows_before(remediation.before, text)
    skill = world.root / _SKILL

    # A plain install without the delete keeps the edit: the drift stays.
    world.install(scenario_id="edited-plain-install")
    assert skill.read_text(encoding="utf-8") == locked + _EDIT
    assert _drift(world, "edited-still-reported")[1] == {(_SKILL, "modified")}

    skill.unlink()
    run_advised(world, remediation)
    assert skill.read_text(encoding="utf-8") == locked
    _assert_drift_cleared(world, "edited-cleared")


def test_drift_restore_edited_force(tmp_path: Path, apm_binary_path: Path) -> None:
    world, locked, text = _edited_skill_world(tmp_path, apm_binary_path)
    remediation = advised("drift-restore-edited-force", text)

    run_advised(world, remediation)
    assert (world.root / _SKILL).read_text(encoding="utf-8") == locked
    _assert_drift_cleared(world, "edited-force-cleared")


def test_drift_restore_bundle_edited(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, skill="proof-skill")
    world.run(("pack",), scenario_id="bundle-pack")
    (bundle,) = (world.root / "build").iterdir()
    project = world.scenario.consumers.create("bundle-consumer", targets=("copilot",))
    bundled = ProofWorld(world.scenario, world.source, project, world.env)
    bundled.run(("install", str(bundle), "--no-policy"), scenario_id="bundle-install")
    skill = bundled.root / _SKILL
    locked = skill.read_text(encoding="utf-8")
    _edit(skill)

    payload, findings = _drift(bundled, "bundle-reported")
    assert findings == {(_SKILL, "modified")}, payload
    text = _audit_text(bundled, "bundle-text")
    remediation = advised("drift-restore-bundle-edited", text, subject="the modified bundle files")
    _shows_before(remediation.before, text)
    assert advised_json("drift-restore-bundle-edited", check(payload, "drift")) == remediation

    skill.unlink()
    run_advised(bundled, remediation, substitutions={"<bundle-path>": str(bundle)})
    assert skill.read_text(encoding="utf-8") == locked
    _assert_drift_cleared(bundled, "bundle-cleared")


# -- export-local-edits -------------------------------------------------------


def test_export_local_edits(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    deployed = world.root / _INSTRUCTION
    _edit(deployed)
    edited = deployed.read_bytes()

    remediation = advised("export-local-edits", _audit_text(world, "export-text"))
    run_advised(world, remediation)

    (patch,) = (world.root / "patches").glob("*.patch")
    patch_text = patch.read_text(encoding="utf-8")
    assert "--- a/.apm/instructions/proof.instructions.md\n" in patch_text, patch_text
    assert f"+{_EDIT}" in patch_text, patch_text
    assert deployed.read_bytes() == edited
    applied = subprocess.run(
        ["git", "apply", "--check", str(patch)],
        cwd=world.source.repository.worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr

    # The install that overwrites the edit names the same advice; the patch survives it.
    installed: CommandResult = world.install(scenario_id="export-install")
    advised("export-local-edits", installed.stdout)
    assert deployed.read_bytes() != edited
    assert patch.read_text(encoding="utf-8") == patch_text


# -- adopt-new-rendering ------------------------------------------------------


def test_adopt_new_rendering(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    deployed = world.root / _INSTRUCTION
    rendered = deployed.read_text(encoding="utf-8")
    # An older APM rendered and recorded different bytes: the file still matches
    # apm.lock.yaml, but this version renders it differently.
    deployed.write_text(rendered + "older rendering\n", encoding="utf-8")
    recorded = compute_file_hash(deployed)
    lock = _lockfile(world)
    lock["dependencies"][0]["deployed_file_hashes"][_INSTRUCTION] = recorded
    for row in lock["deployments"]:
        if row["value"] == _INSTRUCTION:
            row["content_hash"] = recorded
    dump_yaml(lock, world.root / "apm.lock.yaml")

    payload, findings = _drift(world, "rendering-reported")
    assert findings == {(_INSTRUCTION, "modified")}, payload
    assert check(payload, "content-integrity")["passed"] is True, payload
    text = _audit_text(world, "rendering-text")
    assert "export-patch" not in text, text
    remediation = advised("adopt-new-rendering", text)

    run_advised(world, remediation)
    assert deployed.read_text(encoding="utf-8") == rendered
    assert _lockfile(world)["dependencies"][0]["deployed_file_hashes"][_INSTRUCTION] != recorded
    _assert_drift_cleared(world, "rendering-cleared")


# -- unmanaged-conflict: drift-resync-after-conflict -------------------------


def test_drift_resync_after_conflict(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, skill="proof-skill", install=False)
    user_file = world.root / _INSTRUCTION
    user_file.parent.mkdir(parents=True)
    user_file.write_text("USER instruction\n", encoding="utf-8")
    world.install(scenario_id="conflict-install")
    assert user_file.read_text(encoding="utf-8") == "USER instruction\n"
    shutil.rmtree(world.root / Path(_SKILL).parent)

    payload, findings = _drift(world, "conflict-reported")
    assert findings == {(_INSTRUCTION, "unmanaged-conflict"), (_SKILL, "unintegrated")}, payload
    text = _audit_text(world, "conflict-text")
    remediation = advised("drift-resync-after-conflict", text)
    _shows_before(remediation.before, text)
    assert advised_json("drift-resync-after-conflict", check(payload, "drift")) == remediation

    # The before step: resolve the conflict by renaming the user's file.
    mine = user_file.with_name("mine.instructions.md")
    user_file.rename(mine)
    run_advised(world, remediation)
    assert mine.read_text(encoding="utf-8") == "USER instruction\n"
    assert user_file.is_file() and (world.root / _SKILL).is_file()
    _assert_drift_cleared(world, "conflict-cleared")
