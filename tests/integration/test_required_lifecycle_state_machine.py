"""Required real-CLI lifecycle state-machine contracts."""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_OWNER = "apm-fixture-org"
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")


@dataclass(frozen=True)
class _PublishedPackage:
    package: LocalPackage
    repository: LocalGitRepository
    commit: GitCommit
    remote_url: str
    dependency: dict[str, object]
    environment: dict[str, str]


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    sources: LocalPackageFactory
    consumers: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner


def _new_scenario(root: Path, apm_binary_path: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    return _Scenario(
        isolated=isolated,
        environment=environment,
        sources=LocalPackageFactory(isolated.package_root),
        consumers=LocalPackageFactory(isolated.work_root),
        repositories=LocalGitRepositoryFactory(
            isolated.repository_root,
            env=environment,
        ),
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=60,
            scenario_timeout_seconds=90,
        ),
    )


def _skill(name: str) -> str:
    return (
        f"---\nname: {name}\ndescription: Required lifecycle fixture skill {name}\n---\n# {name}\n"
    )


def _instruction(name: str) -> str:
    return (
        "---\n"
        "applyTo: '**'\n"
        f"description: Required lifecycle fixture instruction {name}\n"
        "---\n"
        f"# {name}\n"
    )


def _agent(name: str) -> str:
    return f"---\ndescription: Required lifecycle fixture agent {name}\n---\n# {name}\n"


def _hook(command: str) -> dict[str, object]:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _publish(
    scenario: _Scenario,
    name: str,
    *,
    skill: str | None = None,
    instruction: str | None = None,
    agent: str | None = None,
    hook_command: str | None = None,
    mcp: bool = False,
) -> _PublishedPackage:
    mcp_dependencies: tuple[dict[str, object], ...] = ()
    if mcp:
        mcp_dependencies = (
            {
                "name": "fixture-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "printf",
                "args": ["fixture"],
            },
        )
    package = scenario.sources.create(
        name,
        mcp_dependencies=mcp_dependencies,
    )
    if skill is not None:
        scenario.sources.add_skill(package, skill, _skill(skill))
    if instruction is not None:
        scenario.sources.add_instruction(package, instruction, _instruction(instruction))
    if agent is not None:
        scenario.sources.add_agent(package, agent, _agent(agent))
    if hook_command is not None:
        scenario.sources.add_hook(package, "pretool", _hook(hook_command))

    repository = scenario.repositories.create(name, source_tree=package.root)
    commit = scenario.repositories.commit(repository, message=f"seed {name}")
    remote_url = f"https://github.com/{_OWNER}/{name}"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    dependency: dict[str, object] = {
        "git": remote_url,
        "ref": commit.sha,
        "alias": name,
    }
    return _PublishedPackage(
        package=package,
        repository=repository,
        commit=commit,
        remote_url=remote_url,
        dependency=dependency,
        environment=environment,
    )


def _result_evidence(result: CommandResult) -> str:
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _run_success(
    scenario: _Scenario,
    project: LocalPackage,
    args: tuple[str, ...],
    *,
    environment: dict[str, str],
    scenario_id: str,
) -> CommandResult:
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=project.root,
        env=environment,
    )
    assert result.returncode == 0, _result_evidence(result)
    return result


def _audit(
    scenario: _Scenario,
    project: LocalPackage,
    *,
    environment: dict[str, str],
    expected_returncode: int = 0,
    scenario_id: str,
) -> tuple[CommandResult, dict[str, object]]:
    result = scenario.runner.run(
        _AUDIT_ARGS,
        scenario_id=scenario_id,
        cwd=project.root,
        env=environment,
    )
    assert result.returncode == expected_returncode, _result_evidence(result)
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return result, payload


def _deployment_paths(snapshot: LifecycleStateSnapshot) -> set[str]:
    return {record.locator.value for record in snapshot.deployment_records}


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes, "manifest bytes diverged"
    assert actual.deployment_records == expected.deployment_records, "deployment records diverged"
    assert actual.lockfile_bytes == expected.lockfile_bytes, "lockfile bytes diverged"
    assert actual.mcp_state_bytes == expected.mcp_state_bytes, "MCP state diverged"
    assert actual.lsp_state_bytes == expected.lsp_state_bytes, "LSP state diverged"
    assert actual.files == expected.files, "materialized files diverged"
    assert actual.semantic_bytes == expected.semantic_bytes, "semantic state diverged"


def _hook_commands(settings_path: Path) -> list[str]:
    if not settings_path.exists():
        return []
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for entry in document.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str):
                commands.append(command)
    return commands


def test_required_pack_install_compile_audit_closes_regular_package_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "pack-closure", apm_binary_path)
    source = _publish(
        scenario,
        "regular-kit-source",
        skill="triage",
        instruction="guard",
        agent="reviewer",
    )
    producer = scenario.consumers.create(
        "regular-kit",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )
    producer_manifest = producer.manifest_path.read_bytes()

    _run_success(
        scenario,
        producer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="pack-closure-install-producer",
    )
    _run_success(
        scenario,
        producer,
        (
            "pack",
            "--format",
            "plugin",
            "--archive",
            "--archive-format",
            "zip",
            "--offline",
            "--output",
            "build",
        ),
        environment=source.environment,
        scenario_id="pack-closure-pack",
    )

    archive = producer.root / "build" / "regular-kit-0.1.0.zip"
    assert archive.is_file()
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert any(name.endswith("/plugin.json") for name in names)
    assert any(name.endswith("/apm.lock.yaml") for name in names)

    consumer = scenario.consumers.create(
        "regular-kit-consumer",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )
    _run_success(
        scenario,
        consumer,
        (
            "install",
            str(archive),
            "--target",
            "copilot",
            "--no-policy",
        ),
        environment=scenario.environment,
        scenario_id="pack-closure-install-consumer",
    )
    assert (consumer.root / ".agents" / "skills" / "triage" / "SKILL.md").is_file()
    assert (consumer.root / ".github" / "instructions" / "guard.instructions.md").is_file()
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="pack-closure-reconcile-declared-source",
    )
    _run_success(
        scenario,
        consumer,
        ("compile", "--target", "copilot", "--force-instructions"),
        environment=scenario.environment,
        scenario_id="pack-closure-compile",
    )
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="pack-closure-reconcile-after-compile",
    )
    _, audit = _audit(
        scenario,
        consumer,
        environment=scenario.environment,
        scenario_id="pack-closure-audit",
    )

    producer_state = LifecycleStateSnapshot.capture(producer.root, targets=("copilot",))
    consumer_state = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert producer.manifest_path.read_bytes() == producer_manifest
    assert (
        producer_state.file(".agents/skills/triage/SKILL.md").content == _skill("triage").encode()
    )
    assert (
        consumer_state.file(".agents/skills/triage/SKILL.md").content == _skill("triage").encode()
    )
    assert ".github/instructions/guard.instructions.md" in _deployment_paths(consumer_state)
    compiled = [
        file
        for file in consumer_state.files
        if "compiled" in file.roles and file.content is not None
    ]
    assert compiled
    assert any(b"guard" in file.content for file in compiled if file.content is not None)
    assert audit["passed"] is True
    assert audit["summary"]["failed"] == 0


def test_required_target_widen_then_narrow_reconciles_owned_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "target-contraction", apm_binary_path)
    source = _publish(
        scenario,
        "scope-kit",
        skill="scope",
        instruction="scope",
        hook_command="echo scope",
    )
    consumer = scenario.consumers.create(
        "scope-consumer",
        dependencies=(source.dependency,),
        targets=("claude",),
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="target-contraction-install-a",
    )
    state_a = LifecycleStateSnapshot.capture(consumer.root, targets=("claude",))

    scenario.consumers.set_targets(consumer, ("claude", "cursor"))
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="target-contraction-install-a-b",
    )
    state_ab = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("claude", "cursor"),
    )

    scenario.consumers.set_targets(consumer, ("claude",))
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="target-contraction-install-a-final",
    )
    _run_success(
        scenario,
        consumer,
        ("prune",),
        environment=source.environment,
        scenario_id="target-contraction-prune-a-final",
    )
    state_final = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("claude", "cursor"),
        config_paths=(
            PurePosixPath(".agents/skills/scope/SKILL.md"),
            PurePosixPath(".cursor/rules/scope.mdc"),
        ),
    )
    _, audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        scenario_id="target-contraction-audit",
    )

    assert (
        state_a.file(".claude/skills/scope/SKILL.md").content
        == state_ab.file(".claude/skills/scope/SKILL.md").content
    )
    assert state_ab.file(".agents/skills/scope/SKILL.md").content == _skill("scope").encode()
    assert state_ab.file(".cursor/rules/scope.mdc").kind == "file"
    assert state_ab.file(".cursor/hooks.json").kind == "file"
    assert state_ab.file(".cursor/apm-hooks.json").kind == "file"
    assert state_final.file(".agents/skills/scope/SKILL.md").kind == "missing"
    assert state_final.file(".cursor/rules/scope.mdc").kind == "missing"
    assert _hook_commands(consumer.root / ".cursor" / "hooks.json") == []
    assert state_final.file(".cursor/apm-hooks.json").kind == "missing"
    assert (
        state_final.file(".claude/skills/scope/SKILL.md").content
        == state_a.file(".claude/skills/scope/SKILL.md").content
    )
    assert not any(record.locator.target == "cursor" for record in state_final.deployment_records)
    assert audit["passed"] is True


def test_required_reinstall_is_byte_idempotent_across_durable_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "reinstall-idempotency", apm_binary_path)
    source = _publish(
        scenario,
        "stable-kit",
        skill="stable",
        instruction="stable",
    )
    consumer = scenario.consumers.create(
        "stable-consumer",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="reinstall-idempotency-install-first",
    )
    _run_success(
        scenario,
        consumer,
        ("compile", "--target", "copilot", "--force-instructions"),
        environment=source.environment,
        scenario_id="reinstall-idempotency-compile-first",
    )
    before = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="reinstall-idempotency-install-second",
    )
    _run_success(
        scenario,
        consumer,
        ("compile", "--target", "copilot", "--force-instructions"),
        environment=source.environment,
        scenario_id="reinstall-idempotency-compile-second",
    )
    after = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    _, audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        scenario_id="reinstall-idempotency-audit",
    )

    _assert_same_state(before, after)
    assert all("hook-sidecar" not in file.roles for file in after.files)
    assert audit["passed"] is True


def test_required_dependency_prune_then_uninstall_cascades_owned_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "dependency-cascade", apm_binary_path)
    alpha = _publish(
        scenario,
        "alpha-kit",
        skill="alpha",
        instruction="alpha",
        hook_command="echo alpha",
    )
    beta = _publish(
        scenario,
        "beta-kit",
        skill="beta",
        instruction="beta",
        hook_command="echo beta",
    )
    consumer = scenario.consumers.create(
        "cascade-consumer",
        dependencies=(alpha.dependency,),
        targets=("claude",),
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=alpha.environment,
        scenario_id="dependency-cascade-install-alpha",
    )
    scenario.consumers.replace_apm_dependencies(
        consumer,
        (alpha.dependency, beta.dependency),
    )
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=beta.environment,
        scenario_id="dependency-cascade-install-beta",
    )

    settings = consumer.root / ".claude" / "settings.json"
    assert scenario.consumers.remove_apm_dependency(consumer, beta.dependency)
    _run_success(
        scenario,
        consumer,
        ("prune",),
        environment=scenario.environment,
        scenario_id="dependency-cascade-prune-beta",
    )
    _, prune_audit = _audit(
        scenario,
        consumer,
        environment=scenario.environment,
        scenario_id="dependency-cascade-audit-alpha",
    )
    after_prune = LifecycleStateSnapshot.capture(consumer.root, targets=("claude",))

    assert not (consumer.root / "apm_modules" / _OWNER / "beta-kit").exists()
    assert (consumer.root / "apm_modules" / _OWNER / "alpha-kit").is_dir()
    assert "echo beta" not in _hook_commands(settings)
    assert _hook_commands(settings) == ["echo alpha"]
    assert not any(
        "beta-kit" in owner for record in after_prune.deployment_records for owner in record.owners
    )
    assert prune_audit["passed"] is True

    _run_success(
        scenario,
        consumer,
        ("uninstall", f"{_OWNER}/alpha-kit"),
        environment=scenario.environment,
        scenario_id="dependency-cascade-uninstall-alpha",
    )
    _, uninstall_audit = _audit(
        scenario,
        consumer,
        environment=scenario.environment,
        scenario_id="dependency-cascade-audit-empty",
    )
    after_uninstall = LifecycleStateSnapshot.capture(consumer.root, targets=("claude",))
    manifest = load_yaml(consumer.manifest_path)

    assert not manifest.get("dependencies", {}).get("apm")
    assert after_uninstall.lockfile_bytes is None
    assert not after_uninstall.deployment_records
    assert _hook_commands(settings) == []
    assert uninstall_audit["passed"] is True


def test_required_tamper_is_detected_and_repair_restores_last_good_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "tamper-repair", apm_binary_path)
    source = _publish(
        scenario,
        "repair-kit",
        skill="repair",
        instruction="repair",
    )
    consumer = scenario.consumers.create(
        "repair-consumer",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="tamper-repair-install",
    )
    _run_success(
        scenario,
        consumer,
        ("compile", "--target", "copilot", "--force-instructions"),
        environment=source.environment,
        scenario_id="tamper-repair-compile",
    )
    last_good = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    lock_path = consumer.root / "apm.lock.yaml"
    lock_bytes = lock_path.read_bytes()
    lock = load_yaml(lock_path)
    assert lock["deployments"]
    lock["deployments"][0]["owners"] = ["mutation-owner"]
    lock["deployments"][0]["active_owner"] = "mutation-owner"
    dump_yaml(lock, lock_path)
    mutated = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    with pytest.raises(AssertionError, match="deployment records diverged"):
        _assert_same_state(last_good, mutated)
    lock_path.write_bytes(lock_bytes)

    deployed_instruction = consumer.root / ".github" / "instructions" / "repair.instructions.md"
    deployed_instruction.write_text("# tampered\n", encoding="utf-8")
    _, failed_audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        expected_returncode=1,
        scenario_id="tamper-repair-audit-failed",
    )
    failed_checks = {check["name"] for check in failed_audit["checks"] if not check["passed"]}
    assert failed_checks == {"content-integrity", "drift"}
    assert any(
        ".github/instructions/repair.instructions.md" in detail
        for check in failed_audit["checks"]
        if not check["passed"]
        for detail in check.get("details", [])
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="tamper-repair-reinstall",
    )
    _, clean_audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        scenario_id="tamper-repair-audit-clean",
    )
    repaired = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))

    _assert_same_state(last_good, repaired)
    assert clean_audit["passed"] is True


def test_required_mixed_primitives_survive_reinstall_without_state_loss(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "mixed-primitives", apm_binary_path)
    source = _publish(
        scenario,
        "mixed-kit",
        skill="mixed",
        instruction="mixed",
        hook_command="echo mixed",
        mcp=True,
    )
    consumer = scenario.consumers.create(
        "mixed-consumer",
        dependencies=(source.dependency,),
        targets=("claude",),
    )
    capture_args = {
        "targets": ("claude",),
        "config_paths": (PurePosixPath(".mcp.json"),),
    }

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="mixed-primitives-install-first",
    )
    before = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="mixed-primitives-install-second",
    )
    after = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _, audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        scenario_id="mixed-primitives-audit",
    )

    assert (
        before.file(".claude/skills/mixed/SKILL.md").content
        == after.file(".claude/skills/mixed/SKILL.md").content
    )
    assert (
        before.file(".claude/rules/mixed.md").content
        == after.file(".claude/rules/mixed.md").content
    )
    assert _hook_commands(consumer.root / ".claude" / "settings.json") == ["echo mixed"]
    assert after.file(".claude/apm-hooks.json").kind == "file"
    mcp_document = json.loads(after.file(".mcp.json").content or b"{}")
    assert list(mcp_document["mcpServers"]) == ["fixture-mcp"]
    assert before.mcp_state_bytes == after.mcp_state_bytes
    assert before.semantic_bytes == after.semantic_bytes
    assert audit["passed"] is True
