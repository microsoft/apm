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
from tests.utils.local_package import LocalPackageFactory

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


def _install(
    scene: HarnessScene, *packages: str, expect: int = 0, target: str = "copilot"
) -> CommandResult:
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir(exist_ok=True)
    result = scene.runner.run(
        ("install", "-g", *packages, "--target", target), cwd=cwd, env=scene.env
    )
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


def _as_037_state(scene: HarnessScene, deployed_path: Path | None = None) -> None:
    """Rewind to what 0.37 left: the receipt bound to no lock claim, and no user lock."""
    states = list((scene.isolated.config_root / "deployment-receipts").glob("*.json"))
    deployed = (deployed_path or scene.home / _DEPLOYED).resolve().as_posix()
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


def _assert_claims_bytes(scene: HarnessScene, deployed: Path) -> None:
    """I1: one user-lock claim for the agent records its on-disk hash."""
    hashes = _lock(scene).get("local_deployed_file_hashes") or {}
    claims = [value for value in hashes if value.endswith(f"agents/{deployed.name}")]
    assert [hashes[value] for value in claims] == [compute_file_hash(deployed)], hashes


@pytest.mark.parametrize("relocated", [False, True], ids=["home-root", "claude-config-dir"])
def test_migrated_receipt_survives_an_unchanged_upgrade_on_claude(
    scene: HarnessScene, relocated: bool
) -> None:
    """Case 3 on a non-Copilot target: the migrated claim rebinds to the committed lock."""
    root = scene.home / ".claude"
    if relocated:
        root = scene.isolated.root / "claude-profile"
        root.mkdir()
        scene.env["CLAUDE_CONFIG_DIR"] = str(root)
    else:
        root.mkdir()
    deployed = root / "agents" / "research.md"
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")
    _install(scene, target="claude")
    _as_037_state(scene, deployed)

    adopted = _install(scene, target="claude")
    assert "skipped" not in flat(adopted.stdout + adopted.stderr), describe(adopted)
    _assert_claims_bytes(scene, deployed)

    _source(scene).write_text(_agent("Version two."), encoding="utf-8")
    upgraded = _install(scene, target="claude")
    assert _NO_CHANGES not in flat(upgraded.stdout + upgraded.stderr), describe(upgraded)
    assert deployed.read_text(encoding="utf-8").rstrip().endswith("Version two.")
    _assert_claims_bytes(scene, deployed)


def test_a_bound_receipt_whose_claim_was_removed_is_not_migrated(scene: HarnessScene) -> None:
    """Only a pre-0.38 unbound receipt migrates; a lock that dropped its claim keeps it unowned."""
    _source(scene).write_text(_agent("Version one."), encoding="utf-8")
    _install(scene)
    (scene.isolated.config_root / "apm.lock.yaml").unlink()
    _source(scene).write_text(_agent("Version two."), encoding="utf-8")

    result = _install(scene)

    assert (scene.home / _DEPLOYED).read_text(encoding="utf-8").endswith("Version one.\n")
    assert re.search(r"\b1 file skipped\b", flat(result.stdout + result.stderr)), describe(result)


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


@pytest.mark.parametrize(
    ("skill", "target"),
    [("research", "copilot"), ("skills", "copilot"), ("research", "grok-bot")],
    ids=["copilot", "skill-named-skills", "grok-bot-workflows-root"],
)
def test_first_upgraded_install_updates_a_migrated_user_root_skill(
    scene: HarnessScene, skill: str, target: str
) -> None:
    """Case 3 for a skill directory: an edited source replaces the 0.37 deployed bytes."""
    source = scene.isolated.config_root / "skills" / skill / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        f"---\nname: {skill}\ndescription: Research skill\n---\nVersion one.\n", encoding="utf-8"
    )
    _install(scene, target=target)
    deployed = [
        path
        for path in scene.home.rglob("SKILL.md")
        if path.parent.name == skill and scene.isolated.config_root not in path.parents
    ]
    assert len(deployed) == 1, deployed
    _as_037_state(scene, deployed[0])
    source.write_text(
        f"---\nname: {skill}\ndescription: Research skill\n---\nVersion two.\n", encoding="utf-8"
    )

    upgraded = _install(scene, target=target)

    assert "skipped" not in flat(upgraded.stdout + upgraded.stderr), describe(upgraded)
    assert deployed[0].read_text(encoding="utf-8").rstrip().endswith("Version two.")


def test_first_upgrade_switching_to_a_target_sharing_the_skills_dir_updates_it(
    scene: HarnessScene,
) -> None:
    """#4813: a 0.37 Copilot skill receipt migrates on a first ``install -g --target codex``.

    Copilot and Codex share ``~/.agents/skills``; the receipt's target is not
    selected, yet its skill directory is the selected target's destination.
    """
    source = scene.isolated.config_root / "skills" / "research" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: research\ndescription: Research skill\n---\nVersion one.\n",
        encoding="utf-8",
    )
    _install(scene, target="copilot")
    deployed = scene.home / ".agents" / "skills" / "research" / "SKILL.md"
    assert deployed.is_file(), sorted(p.as_posix() for p in scene.home.rglob("SKILL.md"))
    _as_037_state(scene, deployed)
    (scene.home / ".codex").mkdir(exist_ok=True)
    source.write_text(
        "---\nname: research\ndescription: Research skill\n---\nVersion two.\n",
        encoding="utf-8",
    )

    upgraded = _install(scene, target="codex")

    assert "skipped" not in flat(upgraded.stdout + upgraded.stderr), describe(upgraded)
    assert deployed.read_text(encoding="utf-8").rstrip().endswith("Version two.")
    assert ".agents/skills/research" in (_lock(scene).get("local_deployed_files") or [])


def test_adding_a_dependency_that_deploys_a_root_claimed_agent_keeps_its_file(
    scene: HarnessScene,
) -> None:
    """I7: a dependency taking over a root-claimed path retires the self claim, not the file."""
    _source(scene).write_text(_agent("Shared."), encoding="utf-8")
    _install(scene)
    _assert_claimed(scene)
    factory = LocalPackageFactory(scene.isolated.package_root)
    package = factory.create("research-pack")
    factory.add_agent(package, "research", _agent("Shared."))

    _install(scene, str(package.root), "--no-policy", "--parallel-downloads", "0")

    deployed = scene.home / _DEPLOYED
    assert deployed.is_file(), sorted(p.name for p in deployed.parent.iterdir())
    assert deployed.read_text(encoding="utf-8").endswith("Shared.\n")
    lock = _lock(scene)
    assert _DEPLOYED not in (lock.get("local_deployed_files") or []), lock
    owners = [
        dep
        for dep in lock.get("dependencies") or ()
        if _DEPLOYED in (dep.get("deployed_files") or [])
    ]
    assert len(owners) == 1, lock


def _install_claude_hook_then_audit(scene: HarnessScene, command: str) -> CommandResult:
    hooks = scene.isolated.config_root / "hooks"
    hooks.mkdir()
    hook = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]
        }
    }
    (hooks / "guard.json").write_text(json.dumps(hook, indent=2) + "\n", encoding="utf-8")
    if command.startswith("./scripts/"):
        (hooks / "scripts").mkdir()
        (hooks / "scripts" / "g.sh").write_text("#!/bin/sh\necho guard\n", encoding="utf-8")
    (scene.home / ".copilot").rmdir()
    claude_root = scene.home / ".claude"
    claude_root.mkdir()
    _install(scene, target="claude")
    settings = (claude_root / "settings.json").read_text(encoding="utf-8")
    assert command.removeprefix("./scripts/") in settings, settings
    return scene.runner.run(
        ("audit", "--ci", "--no-fail-fast"), cwd=scene.isolated.config_root, env=scene.env
    )


def test_install_g_then_audit_accepts_a_user_root_scripted_hook(scene: HarnessScene) -> None:
    """I3: audit --ci replays a user-root scripted hook with the commands install -g wrote."""
    audit = _install_claude_hook_then_audit(scene, "./scripts/g.sh")

    assert audit.returncode == 0, describe(audit)


def test_install_g_then_audit_accepts_a_user_root_inline_hook(scene: HarnessScene) -> None:
    """I3: a user-root hook naming no script has no file claim, yet audit --ci replays it."""
    audit = _install_claude_hook_then_audit(scene, "echo guard")

    assert audit.returncode == 0, describe(audit)

    settings = scene.home / ".claude" / "settings.json"
    settings.write_text(
        settings.read_text(encoding="utf-8").replace("echo guard", "echo tampered"),
        encoding="utf-8",
    )
    tampered = scene.runner.run(
        ("audit", "--ci", "--no-fail-fast"), cwd=scene.isolated.config_root, env=scene.env
    )

    assert tampered.returncode == 1, describe(tampered)
    assert "drift detected: 1 file(s): .claude/settings.json" in tampered.stdout, describe(tampered)


def test_install_g_then_audit_accepts_a_user_root_context_link(scene: HarnessScene) -> None:
    """I3: audit --ci replays a user-root agent's context link as install -g wrote it (#4832).

    Install renders ``../context/notes.context.md`` relative to the live
    ``~/.copilot/agents``; replay must emit the same text, not a path from its
    scratch directory, or a clean install reports drift.
    """
    context = scene.isolated.config_root / "context"
    context.mkdir()
    (context / "notes.context.md").write_text(
        "---\ndescription: Notes\n---\n\nShared notes.\n", encoding="utf-8"
    )
    _source(scene).write_text(
        _agent("Read [the notes](../context/notes.context.md) first."), encoding="utf-8"
    )
    _install(scene)
    deployed = (scene.home / _DEPLOYED).read_text(encoding="utf-8")
    assert "[the notes](../../.apm/context/notes.context.md)" in deployed, deployed

    def audit() -> CommandResult:
        return scene.runner.run(
            ("audit", "--ci", "--no-fail-fast"), cwd=scene.isolated.config_root, env=scene.env
        )

    clean = audit()

    assert clean.returncode == 0, describe(clean)
    assert (scene.home / _DEPLOYED).read_text(encoding="utf-8") == deployed

    # Control: a real edit to the deployed agent still reports drift.
    (scene.home / _DEPLOYED).write_text(deployed + "Hand edit.\n", encoding="utf-8")
    edited = audit()

    assert edited.returncode == 1, describe(edited)
    assert "research.agent.md" in flat(edited.stdout + edited.stderr), describe(edited)


def test_install_g_then_audit_accepts_a_dependency_context_link(scene: HarnessScene) -> None:
    """I3: a scratch-materialized dependency's context link replays as install -g wrote it."""
    factory = LocalPackageFactory(scene.isolated.package_root)
    package = factory.create("notes-pack")
    factory.add_agent(
        package, "research", _agent("Read [the notes](../context/notes.context.md) first.")
    )
    context = package.root / ".apm" / "context" / "notes.context.md"
    context.parent.mkdir(parents=True)
    context.write_text("---\ndescription: Notes\n---\n\nShared notes.\n", encoding="utf-8")
    _install(scene, str(package.root), "--no-policy", "--parallel-downloads", "0")
    deployed = (scene.home / _DEPLOYED).read_text(encoding="utf-8")
    assert "notes.context.md)" in deployed, deployed

    audit = scene.runner.run(
        ("audit", "--ci", "--no-fail-fast"), cwd=scene.isolated.config_root, env=scene.env
    )

    assert audit.returncode == 0, describe(audit)
