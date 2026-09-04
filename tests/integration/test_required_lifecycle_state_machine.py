"""Required real-CLI lifecycle state-machine contracts."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
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
_AUDIT_ALL_ARGS = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
_LOCK_ARGS = ("lock", "--no-policy", "--parallel-downloads", "0")
_EXTERNAL_USER_ROOT_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "hermes": "HERMES_HOME",
}
_GLOBAL_AUDIT_RULES = frozenset(
    {
        "config-consistency",
        "content-integrity",
        "deployed-files-present",
        "deployment-ledger-owners",
        "drift",
        "includes-consent",
        "lockfile-exists",
        "no-orphaned-packages",
        "ref-consistency",
        "skill-subset-consistency",
    }
)


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


def _audit_at(
    scenario: _Scenario,
    cwd: Path,
    *,
    environment: dict[str, str],
    expected_returncode: int = 0,
    scenario_id: str,
) -> tuple[CommandResult, dict[str, object]]:
    result = scenario.runner.run(
        _AUDIT_ALL_ARGS,
        scenario_id=scenario_id,
        cwd=cwd,
        env=environment,
    )
    assert result.returncode == expected_returncode, _result_evidence(result)
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return result, payload


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    checks = _checks(payload)
    if name in checks:
        return checks[name]
    raise AssertionError(f"Missing audit check {name!r}")


def _checks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    by_name: dict[str, dict[str, object]] = {}
    for check in checks:
        assert isinstance(check, dict)
        name = check["name"]
        assert isinstance(name, str)
        by_name[name] = check
    return by_name


def _assert_global_audit_rules(
    payload: dict[str, object],
    *,
    failed: set[str] | frozenset[str],
) -> None:
    checks = _checks(payload)
    assert set(checks) == _GLOBAL_AUDIT_RULES
    actual_failed = {name for name, check in checks.items() if check["passed"] is False}
    assert actual_failed == failed
    assert payload["passed"] is (not failed)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    assert summary["total"] == len(_GLOBAL_AUDIT_RULES)
    assert summary["failed"] == len(failed)
    assert summary["passed"] == len(_GLOBAL_AUDIT_RULES) - len(failed)
    for name in _GLOBAL_AUDIT_RULES - failed:
        assert checks[name]["passed"] is True, name


def _drift_kinds_for(
    payload: dict[str, object],
    expected_paths: set[str],
) -> set[tuple[str, str]]:
    drift = payload["drift"]
    assert isinstance(drift, dict)
    entries = drift["drift"]
    assert isinstance(entries, list)
    return {
        (entry["path"], entry["kind"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") in expected_paths
    }


def _skill_deploy_path(target: str, skill_name: str) -> str:
    mapping = KNOWN_TARGETS[target].primitives["skills"]
    suffix = PurePosixPath(mapping.extension.lstrip("/"))
    return (PurePosixPath(mapping.subdir) / skill_name / suffix).as_posix()


def _external_root_specs(
    roots: Mapping[str, Path],
    *,
    config_paths: Mapping[str, tuple[PurePosixPath, ...]],
) -> tuple[LifecycleStateRoot, ...]:
    return tuple(
        LifecycleStateRoot(
            root_id=f"{target}-home",
            target=target,
            scope="user",
            path=root,
            config_paths=config_paths.get(target, ()),
        )
        for target, root in sorted(roots.items())
    )


def _apm_home_root(scenario: _Scenario) -> LifecycleStateRoot:
    return LifecycleStateRoot(
        root_id="apm-home",
        target="copilot",
        scope="user",
        path=scenario.isolated.config_root,
        config_paths=(PurePosixPath("apm.yml"), PurePosixPath("apm.lock.yaml")),
    )


def _deployment_paths(snapshot: LifecycleStateSnapshot) -> set[str]:
    return {record.locator.value for record in snapshot.deployment_records}


def _single_locked_dependency(project_root: Path) -> tuple[LockFile, object]:
    """Return the only package dependency recorded in the consumer lockfile."""
    lockfile = LockFile.read(project_root / "apm.lock.yaml")
    assert lockfile is not None, f"Expected lockfile at {project_root / 'apm.lock.yaml'}"
    dependencies = lockfile.get_package_dependencies()
    assert len(dependencies) == 1, f"Expected one locked dependency, got {dependencies!r}"
    return lockfile, dependencies[0]


def _publish_invalid_package_repository(
    scenario: _Scenario,
    name: str,
) -> tuple[LocalGitRepository, str]:
    """Publish a Git repository that cannot be classified as an APM package."""
    source_root = scenario.isolated.package_root / name
    source_root.mkdir()
    (source_root / "README.md").write_text("# invalid package fixture\n", encoding="utf-8")
    repository = scenario.repositories.create(name, source_tree=source_root)
    scenario.repositories.commit(repository, message=f"seed invalid {name}")
    return repository, f"https://github.com/{_OWNER}/{name}"


def _publish_nested_plugin_repository(
    scenario: _Scenario,
    name: str,
) -> tuple[LocalGitRepository, str]:
    """Publish a plugin whose real skill selector is a source-relative path."""
    source_root = scenario.isolated.package_root / name
    skill_dir = source_root / "skills" / "productivity" / "grill-me"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill("grill-me"), encoding="utf-8")
    plugin_dir = source_root / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "Nested skill selector fixture",
                "author": {"name": "APM Test"},
                "license": "MIT",
                "skills": ["./skills/productivity/grill-me"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    repository = scenario.repositories.create(name, source_tree=source_root)
    scenario.repositories.commit(repository, message=f"seed plugin {name}")
    return repository, f"https://github.com/{_OWNER}/{name}"


def _record_by_value(snapshot: LifecycleStateSnapshot, value: str):
    """Return the deployment record tracking one project-relative value."""
    for record in snapshot.deployment_records:
        if record.locator.value == value:
            return record
    raise AssertionError(f"Missing deployment record for {value!r}")


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


@pytest.mark.parametrize(
    ("args", "success_summary"),
    (
        (("update", "--yes", "--parallel-downloads", "0"), "Updated 2 APM dependencies."),
        (
            (*_LOCK_ARGS, "--update", "--verbose"),
            "Lockfile written to apm.lock.yaml",
        ),
    ),
    ids=("update", "lock-update"),
)
def test_required_failed_dependency_outcome_tuple_matches_durable_state(
    tmp_path: Path,
    apm_binary_path: Path,
    args: tuple[str, ...],
    success_summary: str,
) -> None:
    """Exit code, success summary, and lockfile state must agree on failure."""
    scenario = _new_scenario(tmp_path / "truthful-command-outcomes", apm_binary_path)
    child_repo, child_remote = _publish_invalid_package_repository(scenario, "missing-child")
    parent = scenario.sources.create(
        "truthful-parent",
        dependencies=({"git": child_remote},),
    )
    scenario.sources.add_skill(parent, "parent-skill", _skill("parent-skill"))
    parent_repo = scenario.repositories.create("truthful-parent", source_tree=parent.root)
    scenario.repositories.commit(parent_repo, message="seed parent with invalid transitive dep")
    parent_remote = f"https://github.com/{_OWNER}/truthful-parent"
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        (
            (child_repo, child_remote),
            (parent_repo, parent_remote),
        )
    )
    consumer = scenario.consumers.create(
        f"truthful-consumer-{args[0]}",
        dependencies=({"git": parent_remote},),
        targets=("claude",),
    )

    result = scenario.runner.run(
        args,
        scenario_id=f"truthful-command-outcomes-{args[0]}",
        cwd=consumer.root,
        env=environment,
    )

    assert (
        result.returncode,
        success_summary in result.stdout,
        (consumer.root / "apm.lock.yaml").exists(),
    ) == (1, False, False), _result_evidence(result)


def test_required_invalid_skill_subset_never_reaches_manifest_or_lockfile(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A bogus prefixed skill selector must fail before durable state is written."""
    scenario = _new_scenario(tmp_path / "invalid-skill-selector", apm_binary_path)
    plugin_repo, plugin_remote = _publish_nested_plugin_repository(scenario, "nested-skills")
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        ((plugin_repo, plugin_remote),)
    )
    consumer = scenario.consumers.create("invalid-skill-consumer", targets=("claude",))
    plugin_ref = plugin_repo.worktree.as_posix()
    invalid_selector = "prod/grill-me"
    valid_selector = "productivity/grill-me"

    invalid_result = scenario.runner.run(
        (
            "install",
            "--skill",
            invalid_selector,
            "--target",
            "claude",
            "--no-policy",
            "--parallel-downloads",
            "0",
            plugin_ref,
        ),
        scenario_id="invalid-skill-selector-install",
        cwd=consumer.root,
        env=environment,
    )
    manifest_after_failure = consumer.manifest_path.read_text(encoding="utf-8")
    lock_after_failure = consumer.root / "apm.lock.yaml"
    lock_after_failure_contents = (
        lock_after_failure.read_text(encoding="utf-8") if lock_after_failure.exists() else ""
    )

    assert invalid_selector not in manifest_after_failure
    assert invalid_selector not in lock_after_failure_contents
    assert not lock_after_failure.exists()
    assert invalid_result.returncode == 1, _result_evidence(invalid_result)

    _run_success(
        scenario,
        consumer,
        (
            "install",
            "--skill",
            valid_selector,
            "--target",
            "claude",
            "--no-policy",
            "--parallel-downloads",
            "0",
            plugin_ref,
        ),
        environment=environment,
        scenario_id="valid-skill-selector-install",
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["skills"] = [invalid_selector]
    dump_yaml(manifest, consumer.manifest_path)
    lock_path = consumer.root / "apm.lock.yaml"
    lock_document = load_yaml(lock_path)
    lock_document["dependencies"][0]["skill_subset"] = [invalid_selector]
    dump_yaml(lock_document, lock_path)

    audit_result, audit = _audit(
        scenario,
        consumer,
        environment=environment,
        expected_returncode=1,
        scenario_id="invalid-skill-selector-audit",
    )
    skill_subset_check = _check(audit, "skill-subset-consistency")

    assert invalid_selector in consumer.manifest_path.read_text(encoding="utf-8")
    assert invalid_selector in lock_path.read_text(encoding="utf-8")
    assert skill_subset_check["passed"] is False, _result_evidence(audit_result)


def test_required_lsp_only_dry_run_reports_plan_without_writing_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Real CLI dry-run proof for LSP-only manifests."""
    scenario = _new_scenario(tmp_path / "lsp-only-dry-run", apm_binary_path)
    project = scenario.consumers.create(
        "lsp-only-consumer",
        lsp_dependencies=(
            "typescript-language-server",
            {
                "name": "pyright",
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            },
        ),
        targets=("claude", "copilot"),
    )
    capture_args = {
        "targets": ("claude", "copilot"),
        "config_paths": (
            PurePosixPath("apm_modules"),
            PurePosixPath(".github/lsp.json"),
            PurePosixPath(".claude/skills/apm-lsp/.claude-plugin/plugin.json"),
        ),
        "external_roots": (
            LifecycleStateRoot(
                root_id="apm-home",
                target="claude",
                scope="user",
                path=scenario.isolated.config_root,
                config_paths=(
                    PurePosixPath("apm.yml"),
                    PurePosixPath("config.json"),
                ),
            ),
        ),
    }
    before = LifecycleStateSnapshot.capture(project.root, **capture_args)

    result = _run_success(
        scenario,
        project,
        (*_INSTALL_ARGS, "--dry-run"),
        environment=scenario.environment,
        scenario_id="lsp-only-dry-run-install",
    )
    after = LifecycleStateSnapshot.capture(project.root, **capture_args)

    assert "LSP servers to configure (2):" in result.stdout
    assert "typescript-language-server" in result.stdout
    assert "pyright" in result.stdout
    assert "No dependencies found" not in result.stdout
    assert after.lockfile_bytes is None
    assert after.file("apm_modules").kind == "missing"
    assert after.file(".github/lsp.json").kind == "missing"
    assert after.file(".claude/skills/apm-lsp/.claude-plugin/plugin.json").kind == "missing"
    assert after.file("apm.yml", root_id="apm-home").kind == "missing"
    assert after.file("config.json", root_id="apm-home").kind == "missing"
    _assert_same_state(before, after)


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


def test_required_hybrid_manifest_installs_as_apm_package_through_cli_state_machine(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "hybrid-precedence", apm_binary_path)
    source = _publish(
        scenario,
        "hybrid-apm-kit",
        instruction="authoritative",
    )
    (source.repository.worktree / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "hybrid.agent.plugin",
                "version": "1.0.0",
                "description": "Competing Agent Plugin fixture",
                "author": {"name": "APM Test"},
                "license": "MIT",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (source.repository.worktree / ".claude-plugin").mkdir()
    source_commit = scenario.repositories.commit(
        source.repository,
        message="add competing plugin surfaces",
    )
    dependency = dict(source.dependency)
    dependency["ref"] = source_commit.sha
    consumer = scenario.consumers.create(
        "hybrid-precedence-consumer",
        dependencies=(dependency,),
        targets=("copilot",),
    )
    deployed_instruction = ".github/instructions/authoritative.instructions.md"
    capture_args = {
        "targets": ("copilot",),
        "config_paths": (PurePosixPath(deployed_instruction),),
    }
    before_install = LifecycleStateSnapshot.capture(consumer.root, **capture_args)

    install_result = _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="hybrid-precedence-install",
    )
    after_install = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _lockfile, locked_dep = _single_locked_dependency(consumer.root)

    assert install_result.command == (str(apm_binary_path), *_INSTALL_ARGS)
    assert before_install.file(deployed_instruction).kind == "missing"
    assert (
        after_install.file(deployed_instruction).content == _instruction("authoritative").encode()
    )
    assert deployed_instruction in _deployment_paths(after_install)
    assert locked_dep.package_type == "apm_package"
    assert locked_dep.resolved_commit == source_commit.sha
    assert locked_dep.name == "hybrid-apm-kit"
    assert deployed_instruction in locked_dep.deployed_files
    assert deployed_instruction in locked_dep.deployed_file_hashes
    assert locked_dep.marketplace_plugin_name is None
    assert locked_dep.discovered_via is None
    assert locked_dep.source_url is None
    assert locked_dep.source_digest is None

    _run_success(
        scenario,
        consumer,
        ("uninstall", f"{_OWNER}/hybrid-apm-kit"),
        environment=scenario.environment,
        scenario_id="hybrid-precedence-uninstall",
    )
    after_uninstall = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _, audit = _audit(
        scenario,
        consumer,
        environment=scenario.environment,
        scenario_id="hybrid-precedence-audit",
    )
    after_audit = LifecycleStateSnapshot.capture(consumer.root, **capture_args)

    assert after_uninstall.file(deployed_instruction).kind == "missing"
    assert after_uninstall.lockfile_bytes is None
    assert not after_uninstall.deployment_records
    assert audit["passed"] is True
    assert audit["summary"]["failed"] == 0
    _assert_same_state(after_uninstall, after_audit)


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
            "claude-plugin",
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


def test_required_lock_preserves_bytes_and_provenance_until_install_prunes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Real CLI lifecycle proof for issue #2296.

    `apm lock` must preserve dropped deployed bytes plus their lockfile
    provenance, then a later normal `apm install` must prune the same bytes
    through the canonical cleanup chokepoint while leaving unchanged targets
    untouched.
    """
    scenario = _new_scenario(tmp_path / "lock-provenance-prune", apm_binary_path)
    source = _publish(
        scenario,
        "lock-provenance-kit",
        instruction="guard",
        skill="reviewer",
    )
    consumer = scenario.consumers.create(
        "lock-provenance-consumer",
        dependencies=(source.dependency,),
        targets=("claude", "copilot"),
    )
    tracked_paths = (
        PurePosixPath(".claude/rules/guard.md"),
        PurePosixPath(".claude/skills/reviewer/SKILL.md"),
        PurePosixPath(".github/instructions/guard.instructions.md"),
        PurePosixPath(".agents/skills/reviewer/SKILL.md"),
    )
    capture_args = {"targets": ("claude", "copilot"), "config_paths": tracked_paths}

    install_result = _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="lock-provenance-install-both",
    )
    installed = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _installed_lock, installed_dep = _single_locked_dependency(consumer.root)
    dropped_instruction = ".github/instructions/guard.instructions.md"
    dropped_skill = ".agents/skills/reviewer/SKILL.md"
    kept_instruction = ".claude/rules/guard.md"
    kept_skill = ".claude/skills/reviewer/SKILL.md"

    assert install_result.command == (str(apm_binary_path), *_INSTALL_ARGS)
    assert installed.file(dropped_instruction).kind == "file"
    assert installed.file(dropped_skill).kind == "file"
    assert installed.file(kept_instruction).kind == "file"
    assert installed.file(kept_skill).kind == "file"
    assert {
        dropped_instruction,
        dropped_skill,
        kept_instruction,
        kept_skill,
    }.issubset(_deployment_paths(installed))
    dropped_instruction_hash = installed_dep.deployed_file_hashes[dropped_instruction]
    dropped_skill_hash = installed_dep.deployed_file_hashes[dropped_skill]
    installed_instruction_record = _record_by_value(installed, dropped_instruction)
    installed_skill_record = _record_by_value(installed, dropped_skill)

    scenario.consumers.set_targets(consumer, ("claude",))
    lock_result = _run_success(
        scenario,
        consumer,
        _LOCK_ARGS,
        environment=source.environment,
        scenario_id="lock-provenance-lock-claude-only",
    )
    locked = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _locked_lock, locked_dep = _single_locked_dependency(consumer.root)

    assert lock_result.command == (str(apm_binary_path), *_LOCK_ARGS)
    assert locked.file(dropped_instruction).content == installed.file(dropped_instruction).content
    assert locked.file(dropped_skill).content == installed.file(dropped_skill).content
    assert locked.file(kept_instruction).content == installed.file(kept_instruction).content
    assert locked.file(kept_skill).content == installed.file(kept_skill).content
    assert dropped_instruction in locked_dep.deployed_files
    assert dropped_skill in locked_dep.deployed_files
    assert locked_dep.deployed_file_hashes[dropped_instruction] == dropped_instruction_hash
    assert locked_dep.deployed_file_hashes[dropped_skill] == dropped_skill_hash
    assert _record_by_value(locked, dropped_instruction) == installed_instruction_record
    assert _record_by_value(locked, dropped_skill) == installed_skill_record

    reinstall_result = _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="lock-provenance-install-prune-claude",
    )
    pruned = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    pruned_lock, pruned_dep = _single_locked_dependency(consumer.root)
    _, audit = _audit(
        scenario,
        consumer,
        environment=scenario.environment,
        scenario_id="lock-provenance-audit-final",
    )

    assert reinstall_result.command == (str(apm_binary_path), *_INSTALL_ARGS)
    assert pruned.file(dropped_instruction).kind == "missing"
    assert pruned.file(dropped_skill).kind == "missing"
    assert pruned.file(kept_instruction).content == installed.file(kept_instruction).content
    assert pruned.file(kept_skill).content == installed.file(kept_skill).content
    assert dropped_instruction not in (pruned_dep.deployed_files or [])
    assert dropped_skill not in (pruned_dep.deployed_files or [])
    assert dropped_instruction not in (pruned_dep.deployed_file_hashes or {})
    assert dropped_skill not in (pruned_dep.deployed_file_hashes or {})
    pruned_paths = _deployment_paths(pruned)
    assert dropped_instruction not in pruned_paths
    assert dropped_skill not in pruned_paths
    assert kept_instruction in pruned_paths
    assert kept_skill in pruned_paths
    assert not any(path.startswith(".github/") for path in pruned_paths)
    assert pruned_lock.deployment_ledger.records
    assert audit["passed"] is True
    assert audit["summary"]["failed"] == 0


def test_required_lock_preserves_user_edited_dropped_file_and_row(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Lock mode must never unlink a user-edited dropped deployment.

    The retained row and hash must continue to describe the last APM-deployed
    bytes so a later normal install can route the path through the user-edit
    safety gate instead of silently deleting it.
    """
    scenario = _new_scenario(tmp_path / "lock-user-edit", apm_binary_path)
    source = _publish(
        scenario,
        "lock-user-edit-kit",
        skill="reviewer",
    )
    consumer = scenario.consumers.create(
        "lock-user-edit-consumer",
        dependencies=(source.dependency,),
        targets=("claude", "copilot"),
    )
    tracked_paths = (
        PurePosixPath(".claude/skills/reviewer/SKILL.md"),
        PurePosixPath(".agents/skills/reviewer/SKILL.md"),
    )
    capture_args = {"targets": ("claude", "copilot"), "config_paths": tracked_paths}

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="lock-user-edit-install-both",
    )
    installed = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _installed_lock, installed_dep = _single_locked_dependency(consumer.root)
    dropped_skill = ".agents/skills/reviewer/SKILL.md"
    kept_skill = ".claude/skills/reviewer/SKILL.md"
    dropped_record = _record_by_value(installed, dropped_skill)
    recorded_hash = installed_dep.deployed_file_hashes[dropped_skill]

    scenario.consumers.set_targets(consumer, ("claude",))
    edited_path = consumer.root / dropped_skill
    edited_path.write_text("# user-edited reviewer\n", encoding="utf-8")
    edited = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    assert edited.file(dropped_skill).content != installed.file(dropped_skill).content

    _run_success(
        scenario,
        consumer,
        _LOCK_ARGS,
        environment=source.environment,
        scenario_id="lock-user-edit-lock-claude-only",
    )
    locked = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    _locked_lock, locked_dep = _single_locked_dependency(consumer.root)

    assert locked.file(dropped_skill).content == b"# user-edited reviewer\n"
    assert locked.file(kept_skill).content == installed.file(kept_skill).content
    assert dropped_skill in locked_dep.deployed_files
    assert locked_dep.deployed_file_hashes[dropped_skill] == recorded_hash
    assert _record_by_value(locked, dropped_skill).content_hash == dropped_record.content_hash
    assert _record_by_value(locked, dropped_skill).owners == dropped_record.owners


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


def test_required_global_lock_ignores_inactive_experimental_resolver(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global lockfile generation must not run inactive experimental resolvers."""
    scenario = _new_scenario(tmp_path / "global-inactive-resolver", apm_binary_path)
    source = _publish(scenario, "inactive-resolver-kit", skill="inactive-resolver")
    cwd = scenario.isolated.work_root
    cloud_storage = scenario.isolated.home / "Library" / "CloudStorage"
    cowork_mounts = (
        cloud_storage / "OneDrive-Org",
        cloud_storage / "OneDrive-SharedLibraries-Team",
    )
    for mount in cowork_mounts:
        mount.mkdir(parents=True)
    targets = ("copilot", "claude", "codex")
    scenario.isolated.config_root.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {
            "name": "global-inactive-resolver-consumer",
            "version": "0.1.0",
            "dependencies": {"apm": [source.dependency]},
            "targets": list(targets),
        },
        scenario.isolated.config_root / "apm.yml",
    )

    install = scenario.runner.run(
        (
            "install",
            "--global",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="global-inactive-resolver-install",
        cwd=cwd,
        env=source.environment,
    )

    assert install.returncode == 0, _result_evidence(install)
    lockfile = LockFile.read(scenario.isolated.config_root / "apm.lock.yaml")
    assert lockfile is not None
    dependencies = lockfile.get_package_dependencies()
    assert len(dependencies) == 1
    deployed_files = dependencies[0].deployed_files
    assert deployed_files
    assert all(not path.startswith("cowork://") for path in deployed_files)
    copilot_skill_path = (
        scenario.isolated.home / ".agents" / "skills" / "inactive-resolver" / "SKILL.md"
    )
    claude_skill_path = (
        scenario.isolated.home / ".claude" / "skills" / "inactive-resolver" / "SKILL.md"
    )
    assert copilot_skill_path.is_file()
    assert claude_skill_path.is_file()
    for mount in cowork_mounts:
        assert not (mount / "Documents" / "Cowork" / "skills" / "inactive-resolver").exists()


def test_required_global_audit_rule_matrix_for_external_roots(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "global-audit-matrix", apm_binary_path)
    source = _publish(scenario, "global-audit-kit", skill="global-audit")
    cwd = scenario.isolated.work_root
    targets = ("claude", "hermes")
    external_roots = {target: scenario.isolated.root / f"{target}-home" for target in targets}
    for root in external_roots.values():
        root.mkdir(parents=True)

    sentinel_paths = {
        target: PurePosixPath("user-owned") / f"{target}.sentinel" for target in targets
    }
    for target, sentinel in sentinel_paths.items():
        sentinel_path = external_roots[target] / sentinel
        sentinel_path.parent.mkdir(parents=True)
        sentinel_path.write_bytes(f"{target}-owned-by-user\n".encode("ascii"))

    environment = dict(source.environment)
    for target, root in external_roots.items():
        environment[_EXTERNAL_USER_ROOT_ENV[target]] = str(root)

    skill_paths = {
        target: PurePosixPath(_skill_deploy_path(target, "global-audit")) for target in targets
    }
    snapshot_paths = {target: (sentinel_paths[target], skill_paths[target]) for target in targets}
    capture_roots = _external_root_specs(external_roots, config_paths=snapshot_paths)
    scenario.isolated.config_root.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {
            "name": "global-audit-consumer",
            "version": "0.1.0",
            "dependencies": {"apm": [source.dependency]},
            "targets": list(targets),
        },
        scenario.isolated.config_root / "apm.yml",
    )

    install = scenario.runner.run(
        (
            "install",
            "--global",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="global-audit-install",
        cwd=cwd,
        env=environment,
    )
    assert install.returncode == 0, _result_evidence(install)
    installed = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    installed_home = LifecycleStateSnapshot.capture(
        cwd,
        external_roots=(_apm_home_root(scenario),),
    )
    assert installed_home.file("apm.lock.yaml", root_id="apm-home").kind == "file"

    source_skill_bytes = _skill("global-audit").encode()
    for target in targets:
        assert (
            installed.file(skill_paths[target].as_posix(), root_id=f"{target}-home").content
            == source_skill_bytes
        )
        assert installed.file(
            sentinel_paths[target].as_posix(), root_id=f"{target}-home"
        ).content == f"{target}-owned-by-user\n".encode("ascii")

    def audit_row(
        scenario_id: str,
        *,
        failed: set[str] | frozenset[str],
        expected_returncode: int | None = None,
    ) -> dict[str, object]:
        if expected_returncode is None:
            expected_returncode = 1 if failed else 0
        _, payload = _audit_at(
            scenario,
            scenario.isolated.config_root,
            environment=environment,
            expected_returncode=expected_returncode,
            scenario_id=scenario_id,
        )
        _assert_global_audit_rules(payload, failed=failed)
        return payload

    def assert_clean(scenario_id: str) -> dict[str, object]:
        clean_payload = audit_row(scenario_id, failed=set())
        assert _check(clean_payload, "drift")["passed"] is True
        assert clean_payload["drift"] == {"drift": []}
        return clean_payload

    clean_audit = assert_clean("global-audit-clean")
    assert _check(clean_audit, "deployed-files-present")["passed"] is True
    assert _check(clean_audit, "content-integrity")["passed"] is True
    post_clean_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    _assert_same_state(installed, post_clean_audit)

    claude_skill = external_roots["claude"] / skill_paths["claude"]
    hermes_skill = external_roots["hermes"] / skill_paths["hermes"]
    claude_skill_bytes = claude_skill.read_bytes()
    hermes_skill_bytes = hermes_skill.read_bytes()

    claude_skill.unlink()
    deleted_before_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    deleted_audit = audit_row(
        "global-audit-deleted-file",
        failed={"deployed-files-present", "drift"},
    )
    deleted_after_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    assert _check(deleted_audit, "content-integrity")["passed"] is True
    assert _drift_kinds_for(deleted_audit, {str(claude_skill)}) == {
        (str(claude_skill), "unintegrated")
    }
    _assert_same_state(deleted_before_audit, deleted_after_audit)
    claude_skill.write_bytes(claude_skill_bytes)
    assert_clean("global-audit-after-delete-restore")

    claude_skill.write_bytes(b"# user drift\n")
    edited_before_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    edited_audit = audit_row(
        "global-audit-edited-file",
        failed={"content-integrity", "drift"},
    )
    edited_after_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    assert _check(edited_audit, "deployed-files-present")["passed"] is True
    assert _drift_kinds_for(edited_audit, {str(claude_skill)}) == {(str(claude_skill), "modified")}
    _assert_same_state(edited_before_audit, edited_after_audit)
    claude_skill.write_bytes(claude_skill_bytes)
    assert_clean("global-audit-after-edit-restore")

    modules_dir = scenario.isolated.config_root / "apm_modules"
    modules_backup = scenario.isolated.root / "apm-modules-backup"
    shutil.move(str(modules_dir), str(modules_backup))
    package_removed_before_audit = LifecycleStateSnapshot.capture(
        cwd,
        external_roots=capture_roots,
    )
    package_removed_audit = audit_row(
        "global-audit-package-dir-removed",
        failed={"config-consistency", "drift"},
    )
    package_removed_after_audit = LifecycleStateSnapshot.capture(
        cwd,
        external_roots=capture_roots,
    )
    assert _check(package_removed_audit, "no-orphaned-packages")["passed"] is True
    assert _check(package_removed_audit, "deployed-files-present")["passed"] is True
    assert _check(package_removed_audit, "content-integrity")["passed"] is True
    _assert_same_state(package_removed_before_audit, package_removed_after_audit)
    shutil.move(str(modules_backup), str(modules_dir))
    assert_clean("global-audit-after-package-restore")

    lock_path = scenario.isolated.config_root / "apm.lock.yaml"
    lock_bytes = lock_path.read_bytes()
    lock = load_yaml(lock_path)
    lock["deployments"][0]["owners"] = ["missing-owner"]
    lock["deployments"][0]["active_owner"] = "missing-owner"
    dump_yaml(lock, lock_path)
    ledger_before_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    ledger_audit = audit_row(
        "global-audit-ledger-owner-fault",
        failed={"deployment-ledger-owners"},
    )
    ledger_after_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    assert _check(ledger_audit, "deployed-files-present")["passed"] is True
    assert _check(ledger_audit, "content-integrity")["passed"] is True
    assert _check(ledger_audit, "drift")["passed"] is True
    _assert_same_state(ledger_before_audit, ledger_after_audit)
    lock_path.write_bytes(lock_bytes)
    assert_clean("global-audit-after-ledger-restore")

    outside = scenario.isolated.root / "outside-owned.md"
    outside.write_bytes(b"outside-user-owned\n")
    claude_skill.unlink()
    claude_skill.symlink_to(outside)
    symlink_before_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    symlink_audit = audit_row(
        "global-audit-unsafe-symlink",
        failed={"deployed-files-present", "content-integrity", "drift"},
    )
    symlink_after_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    assert _drift_kinds_for(symlink_audit, {str(claude_skill)}) == {
        (str(claude_skill), "unintegrated")
    }
    assert outside.read_bytes() == b"outside-user-owned\n"
    assert (
        symlink_after_audit.file(skill_paths["claude"].as_posix(), root_id="claude-home").kind
        == "symlink"
    )
    assert (
        symlink_after_audit.file(
            skill_paths["claude"].as_posix(), root_id="claude-home"
        ).link_target
        == symlink_before_audit.file(
            skill_paths["claude"].as_posix(), root_id="claude-home"
        ).link_target
    )
    _assert_same_state(symlink_before_audit, symlink_after_audit)
    claude_skill.unlink()
    claude_skill.write_bytes(claude_skill_bytes)
    assert_clean("global-audit-after-symlink-restore")

    claude_skill.unlink()
    hermes_skill.write_bytes(b"# second fault\n")
    combo_before_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    combo_audit = audit_row(
        "global-audit-combined-missing-and-edited",
        failed={"deployed-files-present", "content-integrity", "drift"},
    )
    combo_after_audit = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    assert _drift_kinds_for(combo_audit, {str(claude_skill), str(hermes_skill)}) == {
        (str(claude_skill), "unintegrated"),
        (str(hermes_skill), "modified"),
    }
    _assert_same_state(combo_before_audit, combo_after_audit)
    claude_skill.write_bytes(claude_skill_bytes)
    hermes_skill.write_bytes(hermes_skill_bytes)
    assert_clean("global-audit-after-combo-restore")

    uninstall = scenario.runner.run(
        ("uninstall", "--global", source.remote_url),
        scenario_id="global-audit-uninstall",
        cwd=cwd,
        env=environment,
    )
    assert uninstall.returncode == 0, _result_evidence(uninstall)
    removed = LifecycleStateSnapshot.capture(cwd, external_roots=capture_roots)
    for target in targets:
        assert (
            removed.file(skill_paths[target].as_posix(), root_id=f"{target}-home").kind == "missing"
        )
        assert removed.file(
            sentinel_paths[target].as_posix(), root_id=f"{target}-home"
        ).content == f"{target}-owned-by-user\n".encode("ascii")
    removed_home = LifecycleStateSnapshot.capture(
        cwd,
        external_roots=(_apm_home_root(scenario),),
    )
    assert removed_home.file("apm.lock.yaml", root_id="apm-home").kind == "missing"

    _, final_audit = _audit_at(
        scenario,
        scenario.isolated.config_root,
        environment=environment,
        scenario_id="global-audit-after-uninstall",
    )
    assert final_audit["passed"] is True
    assert final_audit["summary"]["failed"] == 0


def test_required_failed_lock_write_releases_workspace_lock_and_preserves_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    scenario = _new_scenario(tmp_path / "lock-release", apm_binary_path)
    source = _publish(scenario, "lock-release-kit", skill="lock-release")
    consumer = scenario.consumers.create(
        "lock-release-consumer",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="lock-release-install-baseline",
    )
    scenario.consumers.set_targets(consumer, ("claude",))
    capture_args = {
        "targets": ("claude", "copilot"),
        "config_paths": (
            PurePosixPath(".agents/skills/lock-release/SKILL.md"),
            PurePosixPath(".claude/skills/lock-release/SKILL.md"),
        ),
    }
    before_failure = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    failing_environment = dict(source.environment)
    failing_environment["APM_TEST_FAIL_LOCK_REPLACE"] = "1"
    failed = scenario.runner.run(
        _INSTALL_ARGS,
        scenario_id="lock-release-write-failure",
        cwd=consumer.root,
        env=failing_environment,
    )
    assert failed.returncode != 0, _result_evidence(failed)
    after_failure = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    assert after_failure.lockfile_bytes == before_failure.lockfile_bytes
    assert list(consumer.root.glob("apm-atomic-*")) == []

    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="lock-release-followup-install",
    )
    _, audit = _audit(
        scenario,
        consumer,
        environment=source.environment,
        scenario_id="lock-release-followup-audit",
    )
    released = LifecycleStateSnapshot.capture(consumer.root, **capture_args)
    assert (
        released.file(".claude/skills/lock-release/SKILL.md").content
        == _skill("lock-release").encode()
    )
    assert released.file(".agents/skills/lock-release/SKILL.md").kind == "missing"
    assert audit["passed"] is True


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
