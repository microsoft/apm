"""Executable proofs for the advice ``apm install --frozen`` prints when it refuses (#4090).

Each proof induces the refused state, observes the frozen install fail with the
entry's advice, runs exactly the advised command, and observes the same frozen
install succeed. The consumer project is not a git checkout, so a ``then``
commit step is not needed for the refusal to clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _OWNER,
    _instruction,
    _publish,
    _result_evidence,
    _skill,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.remediation_proof import ProofWorld, advised, installed_world, run_advised

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

PROVES = (
    "record-selection-change",
    "frozen-refresh-lock",
    "accept-local-change",
    "record-mcp-declarations",
    "record-deployed-files",
    "repair-lock-state",
    "update-refs-then-frozen",
)

_FROZEN = (*_INSTALL_ARGS, "--frozen")


def _frozen(world: ProofWorld, label: str, *, expect: int) -> CommandResult:
    return world.run(_FROZEN, scenario_id=f"frozen-{label}", expect=expect)


def _output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def _refused(world: ProofWorld, label: str, diagnostic: str) -> str:
    """Run the frozen install, require a refusal naming *diagnostic*, return its output."""
    result = _frozen(world, f"{label}-refused", expect=1)
    output = _output(result)
    assert diagnostic in " ".join(output.split()), _result_evidence(result)
    return output


def _converges(world: ProofWorld, label: str) -> None:
    """The same frozen install now succeeds without rewriting the lockfile."""
    lock = world.root / "apm.lock.yaml"
    recorded = lock.read_bytes()
    _frozen(world, f"{label}-cleared", expect=0)
    assert lock.read_bytes() == recorded


def _publish_extra(world: ProofWorld, name: str) -> dict[str, object]:
    """Publish a second package, route both remotes locally, and return its dependency."""
    added = _publish(world.scenario, name, instruction="extra")
    world.env = world.scenario.repositories.url_rewrite_subprocess_env_many(
        (
            (world.source.repository, world.source.remote_url),
            (added.repository, added.remote_url),
        )
    )
    return added.dependency


def _set_dependency_field(world: ProofWorld, key: str, value: object) -> None:
    document = load_yaml(world.consumer.manifest_path)
    document["dependencies"]["apm"][0][key] = value
    dump_yaml(document, world.consumer.manifest_path)


def test_update_refs_then_frozen(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="update")
    combined = world.run((*_FROZEN, "--update"), scenario_id="frozen-update", expect=2)
    remediation = advised("update-refs-then-frozen", _output(combined))
    assert "--frozen and --update are mutually exclusive" in " ".join(_output(combined).split())

    run_advised(world, remediation)
    # then: run 'apm install --frozen' in CI
    _converges(world, "update")


def test_frozen_refresh_lock(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="refresh")
    added = _publish_extra(world, "refresh-extra")
    remote = f"{_OWNER}/refresh-extra#{added['ref']}"
    refused = world.run((*_FROZEN, remote), scenario_id="frozen-positional", expect=1)
    output = _output(refused)
    assert "--frozen cannot be combined with positional packages" in " ".join(output.split())
    remediation = advised("frozen-refresh-lock", output)
    assert remediation.then in " ".join(output.split())

    # before: update apm.yml
    world.scenario.consumers.replace_apm_dependencies(
        world.consumer, (world.source.dependency, added)
    )
    run_advised(world, remediation)
    # Repeating the refused command verbatim is still refused, so `then` names
    # the frozen install without the package argument.
    world.run((*_FROZEN, remote), scenario_id="frozen-positional-again", expect=1)
    assert "'apm install --frozen', without packages or --mcp" in remediation.then
    _converges(world, "refresh")
    assert (world.root / ".github" / "instructions" / "extra.instructions.md").is_file()


def test_record_selection_change(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="select", skill="alpha", install=False)
    # Publish a second skill so the dependency can select a subset.
    beta = world.source.repository.worktree / "skills" / "beta" / "SKILL.md"
    beta.parent.mkdir(parents=True)
    beta.write_text(_skill("beta"), encoding="ascii")
    commit = world.scenario.repositories.commit(world.source.repository, message="add beta")
    _set_dependency_field(world, "ref", commit.sha)
    _set_dependency_field(world, "skills", ["alpha"])
    world.install(scenario_id="select-install")
    _converges(world, "select-baseline")

    _set_dependency_field(world, "skills", ["alpha", "beta"])
    output = _refused(world, "select", "dependency selections differ from apm.lock.yaml")
    remediation = advised("record-selection-change", output)

    run_advised(world, remediation)
    # then: commit apm.lock.yaml (not needed outside a git checkout)
    _converges(world, "select")
    assert (world.root / ".agents" / "skills" / "beta" / "SKILL.md").is_file()


def test_accept_local_change(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="local", install=False)
    consumers = world.scenario.consumers
    local = consumers.create("local-dep")
    source = consumers.add_instruction(local, "local-rule", _instruction("local-rule"))
    consumers.replace_apm_dependencies(world.consumer, ("../local-dep",))
    world.install(scenario_id="local-install")
    _converges(world, "local-baseline")

    source.write_text(_instruction("local-rule") + "accepted local edit\n", encoding="ascii")
    output = _refused(world, "local", "local dependency content does not match apm.lock.yaml")
    remediation = advised("accept-local-change", output)

    run_advised(world, remediation)
    _converges(world, "local")
    deployed = world.root / ".github" / "instructions" / "local-rule.instructions.md"
    assert "accepted local edit" in deployed.read_text(encoding="ascii")


def test_record_mcp_declarations(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="mcp", install=False)
    published = _publish(world.scenario, "mcp-service", instruction="service", mcp=True)
    world.env = dict(published.environment)
    world.scenario.consumers.replace_apm_dependencies(world.consumer, (published.dependency,))
    world.install(scenario_id="mcp-install")
    _converges(world, "mcp-baseline")

    # A lockfile whose recorded declaration no longer matches the locked source,
    # replayed on a cold cache so only the restored source can reveal it.
    lock_path = world.root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["mcp_configs"]["fixture-mcp"]["command"] = "changed-command"
    dump_yaml(lock, lock_path)
    modules = world.root / "apm_modules"
    for path in sorted(modules.rglob("*"), reverse=True):
        path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
    modules.rmdir()

    output = _refused(world, "mcp", "restored MCP declarations differ from apm.lock.yaml")
    remediation = advised("record-mcp-declarations", output)

    # before: review the restored sources and lockfile (the source is the intended one)
    run_advised(world, remediation)
    assert load_yaml(lock_path)["mcp_configs"]["fixture-mcp"]["command"] == "printf"
    _converges(world, "mcp")


def test_record_deployed_files(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="deployed")
    _converges(world, "deployed-baseline")
    skill = world.root / ".apm" / "skills" / "unrecorded" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill("unrecorded"), encoding="ascii")

    output = _refused(world, "deployed", "not recorded in apm.lock.yaml")
    remediation = advised("record-deployed-files", output)

    run_advised(world, remediation)
    # then: commit the deployed files and apm.lock.yaml (not needed outside git)
    _converges(world, "deployed")


def test_repair_lock_state(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="repair")
    added = _publish_extra(world, "repair-extra")
    world.scenario.consumers.replace_apm_dependencies(
        world.consumer, (world.source.dependency, added)
    )

    output = _refused(world, "repair", "out of sync")
    remediation = advised("repair-lock-state", output, lock_state="package lock state")

    run_advised(world, remediation)
    _converges(world, "repair")
