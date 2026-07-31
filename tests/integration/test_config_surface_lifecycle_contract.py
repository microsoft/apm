"""Real lifecycle contracts for configuration and dependency graph state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return the bounded real-binary runner for one lifecycle scenario."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )


def _instruction(name: str) -> str:
    """Return a valid observable instruction source document."""
    return f"---\napplyTo: '**'\ndescription: Lifecycle contract fixture {name}\n---\n# {name}\n"


def _dependency_rows(project_root: Path) -> dict[str, dict[str, object]]:
    """Read lockfile dependencies by their canonical repository key."""
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    rows: dict[str, dict[str, object]] = {}
    for dependency in dependencies:
        assert isinstance(dependency, dict)
        key = dependency["repo_url"]
        assert isinstance(key, str)
        rows[key] = dependency
    return rows


@dataclass(frozen=True)
class _GitLifecycleProject:
    """One isolated project consuming a local Git package through a Git URL."""

    isolated: IsolatedApmEnvironment
    project_root: Path
    repository: LocalGitRepository
    commit: GitCommit
    source: str


def _configure_local_source_rewrite(
    source: str,
    repository: LocalGitRepository,
    *,
    environment: dict[str, str],
) -> None:
    """Map a production-shaped Git URL to the local bare fixture origin."""
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            f"url.{repository.file_url}.insteadOf",
            source,
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _create_git_lifecycle_project(
    root: Path,
    *,
    source_name: str,
    mcp_dependencies: tuple[dict[str, object], ...] = (),
    lsp_dependencies: tuple[dict[str, object], ...] = (),
    targets: tuple[str, ...] = ("copilot",),
) -> _GitLifecycleProject:
    """Create an isolated consumer and real local-Git configuration package."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    package_factory = LocalPackageFactory(isolated.package_root)
    source_package = package_factory.create(
        source_name,
        mcp_dependencies=mcp_dependencies,
        lsp_dependencies=lsp_dependencies,
    )
    package_factory.add_instruction(
        source_package,
        f"{source_name}-instruction",
        _instruction(f"{source_name}-instruction"),
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create(source_name, source_tree=source_package.root)
    commit = repositories.commit(repository, message=f"seed {source_name} fixture")
    source = f"git@gitlab.example.invalid:contracts/{source_name}.git"
    _configure_local_source_rewrite(
        source,
        repository,
        environment=environment,
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(
            {
                "git": source,
                "type": "gitlab",
                "ref": "main",
                "alias": source_name,
            },
        ),
        targets=targets,
    )
    return _GitLifecycleProject(
        isolated=isolated,
        project_root=project.root,
        repository=repository,
        commit=commit,
        source=source,
    )


def _audit_payload(
    runner: ApmLifecycleRunner,
    *,
    scenario_id: str,
    cwd: Path,
    environment: dict[str, str],
    expected_returncode: int = 0,
) -> dict[str, object]:
    """Run the real JSON CI audit and return its complete persisted-state report."""
    (result,) = runner.run_sequence(
        (("audit", "--ci", "--no-policy", "--format", "json"),),
        expected_returncodes=(expected_returncode,),
        scenario_id=scenario_id,
        cwd=cwd,
        env=environment,
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    """Return one named audit check, failing when audit shape changes."""
    checks = payload["checks"]
    assert isinstance(checks, list)
    for check in checks:
        assert isinstance(check, dict)
        if check["name"] == name:
            return check
    raise AssertionError(f"Audit did not report {name!r}: {checks!r}")


def _capture_portable_mcp_state(
    project_root: Path,
    isolated: IsolatedApmEnvironment,
) -> LifecycleStateSnapshot:
    """Capture exact project and native MCP state for Copilot and Codex."""
    copilot_root = isolated.home / ".copilot"
    external_roots = (
        (
            LifecycleStateRoot(
                root_id="copilot-user",
                target="copilot",
                scope="project",
                path=copilot_root,
                config_paths=(PurePosixPath("mcp-config.json"),),
            ),
        )
        if copilot_root.is_dir()
        else ()
    )
    return LifecycleStateSnapshot.capture(
        project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
        external_roots=external_roots,
    )


def _assert_exact_lifecycle_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    """Assert byte and semantic idempotency inside one isolated machine."""
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _assert_semantic_lifecycle_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    """Assert convergence while ignoring the separate generated-at churn lane."""
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _create_divergent_mcp_runtime_signal(
    signal: str,
    project_root: Path,
    isolated: IsolatedApmEnvironment,
) -> None:
    """Create one machine-local signal that maps to the Copilot target."""
    if signal == "vscode":
        (project_root / ".vscode").mkdir(exist_ok=True)
        return
    if signal != "intellij":
        raise ValueError(f"Unsupported MCP runtime signal: {signal}")
    if sys.platform == "win32":
        data_root = Path(isolated.process_environment["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        data_root = isolated.home / "Library" / "Application Support"
    else:
        data_root = isolated.home / ".local" / "share"
    (data_root / "github-copilot" / "intellij").mkdir(parents=True)


def test_declared_mcp_targets_are_portable_across_installed_binary_lifecycles(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One committed project converges identically on divergent machines."""
    seed = IsolatedApmEnvironment.create(tmp_path / "portable-seed", base_env=dict(os.environ))
    seed_environment = seed.subprocess_env()
    project = LocalPackageFactory(seed.work_root).create(
        "portable-mcp-project",
        mcp_dependencies=(
            {
                "name": "portable-server",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["portable"],
            },
        ),
        targets=("copilot", "codex"),
    )
    LockFile().write(project.root / "apm.lock.yaml")
    _runner(apm_binary_path).run_sequence(
        (("install", "--no-policy", "--parallel-downloads", "0"),),
        expected_returncodes=(0,),
        scenario_id="portable-mcp-seed-install",
        cwd=project.root,
        env=seed_environment,
    )
    seed_lock = LockFile.read(project.root / "apm.lock.yaml")
    assert seed_lock is not None
    assert seed_lock.mcp_target_servers == {
        "codex": ["portable-server"],
        "vscode": ["portable-server"],
    }
    seed_ledger = DeploymentLedgerCodec.from_lockfile(seed_lock)
    seed_rows = tuple(
        record
        for _key, record in sorted(seed_ledger.records.items())
        if record.locator.target == "mcp"
    )
    assert {record.locator.runtime for record in seed_rows} == {"codex", "vscode"}

    seed_codex_config = project.root / ".codex" / "config.toml"
    seed_codex_config.unlink()
    seed_codex_config.parent.rmdir()
    repositories = LocalGitRepositoryFactory(
        seed.repository_root,
        env=seed_environment,
    )
    committed_project = repositories.create(
        "portable-mcp-project",
        source_tree=project.root,
    )
    project_commit = repositories.commit(
        committed_project,
        message="seed portable MCP project",
    )

    machine_states: list[LifecycleStateSnapshot] = []
    for machine_name, runtime_signal in (
        ("vscode-machine", "vscode"),
        ("intellij-machine", "intellij"),
    ):
        isolated = IsolatedApmEnvironment.create(
            tmp_path / machine_name,
            base_env=dict(os.environ),
        )
        environment = isolated.subprocess_env()
        clone = isolated.work_root / "portable-mcp-project"
        cloned = subprocess.run(
            ("git", "clone", "--quiet", committed_project.file_url, str(clone)),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert cloned.returncode == 0
        clone_head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=clone,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert clone_head.stdout.strip() == project_commit.sha
        _create_divergent_mcp_runtime_signal(runtime_signal, clone, isolated)

        runner = _runner(apm_binary_path)
        install = ("install", "--no-policy", "--parallel-downloads", "0")
        runner.run_sequence(
            (install,),
            expected_returncodes=(0,),
            scenario_id=f"{machine_name}-install",
            cwd=clone,
            env=environment,
        )
        installed = _capture_portable_mcp_state(clone, isolated)
        runner.run_sequence(
            (install,),
            expected_returncodes=(0,),
            scenario_id=f"{machine_name}-unchanged-reinstall",
            cwd=clone,
            env=environment,
        )
        repeated = _capture_portable_mcp_state(clone, isolated)
        _assert_exact_lifecycle_state(installed, repeated)

        runner.run_sequence(
            (("update", "--yes"),),
            expected_returncodes=(0,),
            scenario_id=f"{machine_name}-update",
            cwd=clone,
            env=environment,
        )
        updated = _capture_portable_mcp_state(clone, isolated)
        _assert_exact_lifecycle_state(installed, updated)

        runner.run_sequence(
            (("install", "--frozen", "--no-policy"),),
            expected_returncodes=(0,),
            scenario_id=f"{machine_name}-frozen-install",
            cwd=clone,
            env=environment,
        )
        frozen = _capture_portable_mcp_state(clone, isolated)
        _assert_exact_lifecycle_state(installed, frozen)

        before_audit = _capture_portable_mcp_state(clone, isolated)
        audit = _audit_payload(
            runner,
            scenario_id=f"{machine_name}-audit",
            cwd=clone,
            environment=environment,
        )
        assert audit["passed"] is True
        after_audit = _capture_portable_mcp_state(clone, isolated)
        _assert_exact_lifecycle_state(before_audit, after_audit)

        lockfile = LockFile.read(clone / "apm.lock.yaml")
        assert lockfile is not None
        assert lockfile.mcp_target_servers == {
            "codex": ["portable-server"],
            "vscode": ["portable-server"],
        }
        ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        rows = tuple(
            record
            for _key, record in sorted(ledger.records.items())
            if record.locator.target == "mcp"
        )
        assert rows == seed_rows
        machine_states.append(after_audit)

    first, second = machine_states
    assert first.manifest_bytes == second.manifest_bytes
    assert first.lockfile_bytes == second.lockfile_bytes
    assert first.deployment_records == second.deployment_records
    assert first.mcp_state_bytes == second.mcp_state_bytes
    assert first.semantic_bytes == second.semantic_bytes


def test_conflicting_manifest_targets_fail_before_installed_binary_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A conflicting MCP-only manifest exits explicitly without mutating state."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "conflicting-targets",
        base_env=dict(os.environ),
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "conflicting-target-project",
        mcp_dependencies=(
            {
                "name": "must-not-write",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
            },
        ),
        targets=("copilot",),
    )
    manifest = load_yaml(project.manifest_path)
    assert isinstance(manifest, dict)
    manifest["target"] = "codex"
    dump_yaml(manifest, project.manifest_path)
    LockFile().write(project.root / "apm.lock.yaml")

    codex_config = project.root / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text(
        '[mcp_servers.user-authored]\ncommand = "user-command"\n',
        encoding="utf-8",
    )
    copilot_root = isolated.home / ".copilot"
    copilot_root.mkdir()
    copilot_config = copilot_root / "mcp-config.json"
    copilot_config.write_text(
        '{"mcpServers":{"user-authored":{"command":"user-command"}}}\n',
        encoding="utf-8",
    )
    before = _capture_portable_mcp_state(project.root, isolated)

    result = _runner(apm_binary_path).run(
        ("install", "--no-policy"),
        scenario_id="conflicting-targets-no-write",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 2
    assert "Cannot use both 'target:' and 'targets:'" in f"{result.stdout}\n{result.stderr}"
    after = _capture_portable_mcp_state(project.root, isolated)
    _assert_exact_lifecycle_state(before, after)


def test_project_mcp_reinstall_repairs_canonical_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """MCP reinstall must restore lock provenance, deployment ownership, and audit closure."""
    server = {
        "name": "project-contract-server",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": ["project-contract"],
    }
    fixture = _create_git_lifecycle_project(
        tmp_path / "project-mcp",
        source_name="mcp-source",
        mcp_dependencies=(server,),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    manifest_bytes = (fixture.project_root / "apm.yml").read_bytes()
    install_args = (
        "install",
        "--runtime",
        "vscode",
        "--target",
        "copilot",
        "--trust-transitive-mcp",
        "--no-policy",
    )

    runner.run_sequence(
        (install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-initial-install",
        cwd=fixture.project_root,
        env=environment,
    )
    first = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".vscode/mcp.json"),),
    )
    assert first.manifest_bytes == manifest_bytes
    assert first.deployment_records
    assert b"project-contract-server" in first.mcp_state_bytes
    assert first.file(".vscode/mcp.json").kind == "file"
    assert (
        _audit_payload(
            runner,
            scenario_id="project-mcp-initial-audit",
            cwd=fixture.project_root,
            environment=environment,
        )["passed"]
        is True
    )

    runner.run_sequence(
        (install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-reinstall",
        cwd=fixture.project_root,
        env=environment,
    )
    reinstalled = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".vscode/mcp.json"),),
    )
    assert reinstalled.manifest_bytes == first.manifest_bytes
    assert reinstalled.file(".vscode/mcp.json").content == first.file(".vscode/mcp.json").content
    assert reinstalled.mcp_state_bytes == first.mcp_state_bytes
    assert reinstalled.semantic_bytes == first.semantic_bytes

    lock_data = load_yaml(fixture.project_root / "apm.lock.yaml")
    assert isinstance(lock_data, dict)
    deployments = lock_data["deployments"]
    assert isinstance(deployments, list)
    assert deployments
    deployments.clear()
    dump_yaml(lock_data, fixture.project_root / "apm.lock.yaml")

    broken = _audit_payload(
        runner,
        scenario_id="project-mcp-mutated-audit",
        cwd=fixture.project_root,
        environment=environment,
        expected_returncode=1,
    )
    assert broken["passed"] is False
    assert _check(broken, "content-integrity")["passed"] is False

    runner.run_sequence(
        (install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-repair-install",
        cwd=fixture.project_root,
        env=environment,
    )
    repaired = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".vscode/mcp.json"),),
    )
    assert repaired.manifest_bytes == manifest_bytes
    assert repaired.mcp_state_bytes == reinstalled.mcp_state_bytes
    assert repaired.semantic_bytes == reinstalled.semantic_bytes
    closure = _audit_payload(
        runner,
        scenario_id="project-mcp-repaired-audit",
        cwd=fixture.project_root,
        environment=environment,
    )
    assert closure["passed"] is True


def test_user_scope_mcp_reinstall_keeps_global_copilot_state_isolated(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global MCP installs converge under isolated APM and Copilot user roots."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "user-mcp",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.package_root)
    package = package_factory.create(
        "user-mcp-source",
        mcp_dependencies=(
            {
                "name": "user-contract-server",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["user-contract"],
            },
        ),
    )
    unrelated_project = isolated.work_root / "unrelated-project"
    unrelated_project.mkdir()
    unrelated_manifest = unrelated_project / "apm.yml"
    unrelated_bytes = b"name: unrelated\nversion: 0.1.0\n"
    unrelated_manifest.write_bytes(unrelated_bytes)
    copilot_root = isolated.home / ".copilot"
    copilot_root.mkdir()
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    initial_install = (
        "install",
        "--global",
        str(package.root),
        "--target",
        "copilot",
        "--trust-transitive-mcp",
        "--no-policy",
    )
    reinstall = (
        "install",
        "--global",
        "--target",
        "copilot",
        "--trust-transitive-mcp",
        "--no-policy",
    )

    runner.run_sequence(
        (initial_install,),
        expected_returncodes=(0,),
        scenario_id="user-mcp-initial-install",
        cwd=unrelated_project,
        env=environment,
    )
    user_root = isolated.home / ".apm"
    first = LifecycleStateSnapshot.capture(
        user_root,
        external_roots=(
            LifecycleStateRoot(
                root_id="copilot-user",
                target="copilot",
                scope="user",
                path=copilot_root,
                config_paths=(PurePosixPath("mcp-config.json"),),
            ),
        ),
    )
    assert unrelated_manifest.read_bytes() == unrelated_bytes
    assert first.manifest_bytes is not None
    assert first.lockfile_bytes is not None
    assert b"user-contract-server" in first.mcp_state_bytes
    assert first.file("mcp-config.json", root_id="copilot-user").kind == "file"

    runner.run_sequence(
        (reinstall,),
        expected_returncodes=(0,),
        scenario_id="user-mcp-reinstall",
        cwd=unrelated_project,
        env=environment,
    )
    second = LifecycleStateSnapshot.capture(
        user_root,
        external_roots=(
            LifecycleStateRoot(
                root_id="copilot-user",
                target="copilot",
                scope="user",
                path=copilot_root,
                config_paths=(PurePosixPath("mcp-config.json"),),
            ),
        ),
    )
    assert unrelated_manifest.read_bytes() == unrelated_bytes
    assert second.manifest_bytes == first.manifest_bytes
    assert (
        second.file("mcp-config.json", root_id="copilot-user").content
        == first.file("mcp-config.json", root_id="copilot-user").content
    )
    assert second.semantic_bytes == first.semantic_bytes
    closure = _audit_payload(
        runner,
        scenario_id="user-mcp-audit",
        cwd=user_root,
        environment=environment,
    )
    assert closure["passed"] is True


def test_mcp_target_contraction_removes_only_apm_owned_native_config(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Target narrowing removes managed MCP config while preserving user-owned entries."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "mcp-target-contraction",
        source_name="contraction-source",
        mcp_dependencies=(
            {
                "name": "managed-contract-server",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["managed-contract"],
            },
        ),
        targets=("copilot", "codex"),
    )
    codex_config = fixture.project_root / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    unrelated_codex_file = codex_config.parent / "user-notes.txt"
    unrelated_codex_bytes = b"user-owned codex notes\n"
    unrelated_codex_file.write_bytes(unrelated_codex_bytes)
    codex_config.write_text(
        "[projects.'c:\\\\contracts\\\\consumer']\n"
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.user-authored]\n"
        'command = "user-command"\n',
        encoding="utf-8",
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    broad_install = (
        "install",
        "--trust-transitive-mcp",
        "--no-policy",
    )
    narrow_install = (
        "install",
        "--trust-transitive-mcp",
        "--no-policy",
    )

    runner.run_sequence(
        (broad_install,),
        expected_returncodes=(0,),
        scenario_id="mcp-target-contraction-broad-install",
        cwd=fixture.project_root,
        env=environment,
    )
    broad = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    assert broad.file(".vscode/mcp.json").kind == "file"
    assert b"managed-contract-server" in broad.file(".codex/config.toml").content
    assert b"user-authored" in broad.file(".codex/config.toml").content
    assert b"trust_level" in broad.file(".codex/config.toml").content
    assert (
        b'"target_servers":{"codex":["managed-contract-server"],"vscode":["managed-contract-server"]}'
        in (broad.mcp_state_bytes)
    )

    manifest = load_yaml(fixture.project_root / "apm.yml")
    assert isinstance(manifest, dict)
    manifest["targets"] = ["copilot"]
    dump_yaml(manifest, fixture.project_root / "apm.yml")
    runner.run_sequence(
        (narrow_install,),
        expected_returncodes=(0,),
        scenario_id="mcp-target-contraction-narrow-install",
        cwd=fixture.project_root,
        env=environment,
    )
    narrow = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    codex_bytes = narrow.file(".codex/config.toml").content
    assert codex_bytes is not None
    assert b"managed-contract-server" not in codex_bytes
    assert b"user-authored" in codex_bytes
    assert b"trust_level" in codex_bytes
    assert b'"target_servers":{"vscode":["managed-contract-server"]}' in narrow.mcp_state_bytes
    assert unrelated_codex_file.read_bytes() == unrelated_codex_bytes

    runner.run_sequence(
        (narrow_install,),
        expected_returncodes=(0,),
        scenario_id="mcp-target-contraction-convergence",
        cwd=fixture.project_root,
        env=environment,
    )
    converged = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    _assert_semantic_lifecycle_state(narrow, converged)
    assert unrelated_codex_file.read_bytes() == unrelated_codex_bytes

    before_audit = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    assert (
        _audit_payload(
            runner,
            scenario_id="mcp-target-contraction-audit",
            cwd=fixture.project_root,
            environment=environment,
        )["passed"]
        is True
    )
    after_audit = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    _assert_exact_lifecycle_state(before_audit, after_audit)


def test_lsp_reinstall_and_update_keep_copilot_state_deterministic(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """LSP install, reinstall, and update converge on one native Copilot state."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "lsp-reinstall-update",
        source_name="lsp-source",
        lsp_dependencies=(
            {
                "name": "contract-lsp",
                "command": "contract-lsp-command",
                "extensionToLanguage": {".contract": "contract"},
            },
        ),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    manifest_bytes = (fixture.project_root / "apm.yml").read_bytes()
    install = ("install", "--target", "copilot", "--no-policy")
    update = ("update", "--yes", "--target", "copilot")

    runner.run_sequence(
        (install,),
        expected_returncodes=(0,),
        scenario_id="lsp-initial-install",
        cwd=fixture.project_root,
        env=environment,
    )
    first = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".github/lsp.json"),),
    )
    assert first.manifest_bytes == manifest_bytes
    assert b"contract-lsp" in first.lsp_state_bytes
    assert first.file(".github/lsp.json").kind == "file"

    runner.run_sequence(
        (install,),
        expected_returncodes=(0,),
        scenario_id="lsp-reinstall",
        cwd=fixture.project_root,
        env=environment,
    )
    second = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".github/lsp.json"),),
    )
    assert second.manifest_bytes == manifest_bytes
    assert second.file(".github/lsp.json").content == first.file(".github/lsp.json").content
    assert second.lsp_state_bytes == first.lsp_state_bytes
    assert second.semantic_bytes == first.semantic_bytes

    runner.run_sequence(
        (update,),
        expected_returncodes=(0,),
        scenario_id="lsp-update",
        cwd=fixture.project_root,
        env=environment,
    )
    updated = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".github/lsp.json"),),
    )
    assert updated.manifest_bytes == manifest_bytes
    assert updated.file(".github/lsp.json").content == first.file(".github/lsp.json").content
    assert updated.lsp_state_bytes == first.lsp_state_bytes
    assert updated.semantic_bytes == first.semantic_bytes
    assert (
        _audit_payload(
            runner,
            scenario_id="lsp-update-audit",
            cwd=fixture.project_root,
            environment=environment,
        )["passed"]
        is True
    )


def test_saved_target_drives_package_mcp_lsp_update_audit_and_uninstall(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One saved target must drive every package and service lifecycle phase."""
    server_name = "saved-target-mcp"
    lsp_name = "saved-target-lsp"
    fixture = _create_git_lifecycle_project(
        tmp_path / "saved-target-project",
        source_name="saved-target-source",
        mcp_dependencies=(
            {
                "name": server_name,
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["initial"],
            },
        ),
        lsp_dependencies=(
            {
                "name": lsp_name,
                "command": "saved-target-language-server",
                "extensionToLanguage": {".saved": "saved"},
            },
        ),
        targets=(),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    install = ("install", "--no-policy")

    runner.run_sequence(
        (("config", "set", "target", "claude"), install),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-config-and-install",
        cwd=fixture.project_root,
        env=environment,
    )
    claude_mcp = fixture.project_root / ".mcp.json"
    claude_lsp = fixture.project_root / ".lsp.json"
    claude_instruction = (
        fixture.project_root / ".claude" / "rules" / "saved-target-source-instruction.md"
    )
    assert server_name in json.loads(claude_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))
    assert claude_instruction.is_file()

    first_mcp = claude_mcp.read_bytes()
    first_lsp = claude_lsp.read_bytes()
    runner.run_sequence(
        (install,),
        expected_returncodes=(0,),
        scenario_id="saved-target-reinstall",
        cwd=fixture.project_root,
        env=environment,
    )
    assert claude_mcp.read_bytes() == first_mcp
    assert claude_lsp.read_bytes() == first_lsp

    claude_mcp.unlink()
    claude_lsp.unlink()
    runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-noop-update-repair",
        cwd=fixture.project_root,
        env=environment,
    )
    assert server_name in json.loads(claude_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))

    source_manifest = load_yaml(fixture.repository.worktree / "apm.yml")
    source_manifest["version"] = "0.2.0"
    dump_yaml(source_manifest, fixture.repository.worktree / "apm.yml")
    for command in (
        ("git", "add", "--all"),
        ("git", "commit", "-m", "update saved target fixture"),
        ("git", "push", "origin", "HEAD:refs/heads/main"),
    ):
        subprocess.run(
            command,
            cwd=fixture.repository.worktree,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-update",
        cwd=fixture.project_root,
        env=environment,
    )
    assert claude_instruction.is_file()
    assert server_name in json.loads(claude_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))
    runner.run_sequence(
        (install, ("audit", "--ci")),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-update-reinstall-audit",
        cwd=fixture.project_root,
        env=environment,
    )

    runner.run_sequence(
        (("uninstall", fixture.source), install),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-uninstall-reconcile",
        cwd=fixture.project_root,
        env=environment,
    )
    assert not claude_instruction.exists()

    runner.run_sequence(
        (
            ("config", "set", "target", "copilot"),
            ("install", str(fixture.repository.worktree), "--no-policy"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-change-and-reinstall",
        cwd=fixture.project_root,
        env=environment,
    )
    assert (fixture.project_root / ".vscode" / "mcp.json").is_file()
    assert (fixture.project_root / ".github" / "lsp.json").is_file()
    assert (
        fixture.project_root
        / ".github"
        / "instructions"
        / "saved-target-source-instruction.instructions.md"
    ).is_file()


def test_saved_target_drives_user_scope_package_mcp_and_lsp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The real config command must also govern global package and service writes."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "saved-target-user",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.package_root)
    package = package_factory.create(
        "saved-target-user-source",
        mcp_dependencies=(
            {
                "name": "saved-target-user-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["user"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "saved-target-user-lsp",
                "command": "saved-target-user-language-server",
                "extensionToLanguage": {".user": "user"},
            },
        ),
    )
    package_factory.add_instruction(
        package,
        "saved-target-user-instruction",
        _instruction("saved-target-user-instruction"),
    )
    cwd = isolated.work_root / "caller"
    cwd.mkdir()
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()

    runner.run_sequence(
        (
            ("config", "set", "target", "claude"),
            ("install", "--global", str(package.root), "--no-policy"),
            ("install", "--global", "--no-policy"),
        ),
        expected_returncodes=(0, 0, 0),
        scenario_id="saved-target-user-install-reinstall",
        cwd=cwd,
        env=environment,
    )
    claude_config = json.loads((isolated.home / ".claude.json").read_text(encoding="utf-8"))
    assert "saved-target-user-mcp" in claude_config["mcpServers"]
    assert "saved-target-user-lsp" in claude_config["lspServers"]
    assert (isolated.home / ".claude" / "rules" / "saved-target-user-instruction.md").is_file()


def test_saved_target_drives_direct_mcp_without_target_flag(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The issue #2345 direct --mcp reproduction must configure Claude and exit zero."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "saved-target-direct-mcp",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: saved-target-direct\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    results = runner.run_sequence(
        (
            ("config", "set", "target", "claude"),
            ("install", "--mcp", "saved-direct-mcp", "--", "echo", "ready"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-direct-mcp",
        cwd=project,
        env=environment,
    )

    config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "saved-direct-mcp" in config["mcpServers"]
    output = results[-1].stdout + results[-1].stderr
    assert "Skipping all MCP config writes" not in output
    assert "Install interrupted" not in output

    (project / ".mcp.json").unlink()
    runner.run_sequence(
        (("install", "--mcp", "saved-direct-mcp", "--", "echo", "ready"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-direct-mcp-repair",
        cwd=project,
        env=environment,
    )
    repaired = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "saved-direct-mcp" in repaired["mcpServers"]


def test_saved_target_drives_declared_mcp_and_lsp_without_package(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A plain install must forward the saved decision to both service phases."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "saved-target-declared-services",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        mcp_dependencies=(
            {
                "name": "saved-declared-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["declared"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "saved-declared-lsp",
                "command": "saved-declared-language-server",
                "extensionToLanguage": {".declared": "declared"},
            },
        ),
    )
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (
            ("config", "set", "target", "claude"),
            ("install", "--no-policy"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-declared-services",
        cwd=project.root,
        env=environment,
    )

    assert (project.root / ".mcp.json").is_file()
    assert (project.root / ".lsp.json").is_file()

    (project.root / ".mcp.json").unlink()
    (project.root / ".lsp.json").unlink()
    runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-service-only-update-repair",
        cwd=project.root,
        env=environment,
    )
    assert (project.root / ".mcp.json").is_file()
    assert (project.root / ".lsp.json").is_file()

    manifest = load_yaml(project.manifest_path)
    manifest["dependencies"].pop("mcp")
    manifest["dependencies"].pop("lsp")
    dump_yaml(manifest, project.manifest_path)
    runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-service-only-removal",
        cwd=project.root,
        env=environment,
    )
    assert (
        "saved-declared-mcp"
        not in json.loads((project.root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    )
    assert "saved-declared-lsp" not in json.loads(
        (project.root / ".lsp.json").read_text(encoding="utf-8")
    )


def test_saved_copilot_target_projects_to_vscode_for_direct_mcp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A saved Copilot profile must use the project-scoped VS Code MCP runtime."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "saved-copilot-direct-mcp",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: saved-copilot-direct\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (
            ("config", "set", "target", "copilot"),
            ("install", "--mcp", "saved-copilot-mcp", "--", "echo", "ready"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="saved-copilot-direct-mcp",
        cwd=project,
        env=environment,
    )

    assert (project / ".vscode" / "mcp.json").is_file()
    assert not (isolated.home / ".copilot" / "mcp-config.json").exists()


@pytest.mark.parametrize("ambiguous", [False, True])
def test_direct_mcp_requires_resolved_target_before_manifest_write(
    tmp_path: Path,
    apm_binary_path: Path,
    ambiguous: bool,
) -> None:
    """No-harness and ambiguous-harness failures must be nonzero and atomic."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"required-target-{ambiguous}",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = project / "apm.yml"
    original = b"name: required-target\nversion: 0.1.0\n"
    manifest.write_bytes(original)
    if ambiguous:
        (project / ".claude").mkdir()
        cursor = project / ".cursor"
        cursor.mkdir()

    result = _runner(apm_binary_path).run(
        ("install", "--mcp", "required-target-server", "--", "echo", "ready"),
        scenario_id=f"required-target-{ambiguous}",
        cwd=project,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 2
    output = result.stdout + result.stderr
    expected = "Multiple harnesses detected" if ambiguous else "No harness detected"
    assert expected in output
    assert "Added MCP server" not in output
    assert manifest.read_bytes() == original
    assert not (project / ".mcp.json").exists()
    assert not (project / ".vscode" / "mcp.json").exists()


def test_malformed_saved_target_falls_back_to_strict_detection_without_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An invalid persisted value keeps the existing unset-config contract."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "malformed-saved-target",
        base_env=dict(os.environ),
    )
    (isolated.config_root / "config.json").write_text(
        json.dumps({"default_client": "vscode", "install_target": "not-a-target"}),
        encoding="utf-8",
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = project / "apm.yml"
    original = b"name: malformed-target\nversion: 0.1.0\n"
    manifest.write_bytes(original)

    result = _runner(apm_binary_path).run(
        ("install", "--mcp", "malformed-target-server", "--", "echo", "ready"),
        scenario_id="malformed-saved-target",
        cwd=project,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 2
    assert "No harness detected" in result.stdout + result.stderr
    assert manifest.read_bytes() == original


def test_unset_target_keeps_normal_detection_for_package_without_services(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A service-free package still succeeds through unrestricted auto-detection."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "unset-target-package",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.package_root)
    package = package_factory.create("service-free-source")
    package_factory.add_instruction(
        package,
        "service-free",
        _instruction("service-free"),
    )
    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        "consumer",
        dependencies=(str(package.root),),
    )
    (project.root / ".claude").mkdir()

    _runner(apm_binary_path).run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="unset-target-service-free-package",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert (project.root / ".claude" / "rules" / "service-free.md").is_file()


def test_service_write_failure_is_nonzero(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A required native config write failure must never report success."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "required-write-failure",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: write-failure\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    (project / ".mcp.json").mkdir()
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (("config", "set", "target", "claude"),),
        expected_returncodes=(0,),
        scenario_id="required-write-failure-config",
        cwd=project,
        env=environment,
    )

    result = runner.run(
        ("install", "--mcp", "write-failure-server", "--", "echo", "ready"),
        scenario_id="required-write-failure",
        cwd=project,
        env=environment,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "failed" in output.lower()
    assert "Added MCP server" not in output


def test_lsp_write_failure_is_nonzero(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A required LSP config write failure must never report success."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "required-lsp-write-failure",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        lsp_dependencies=(
            {
                "name": "write-failure-lsp",
                "command": "write-failure-language-server",
                "extensionToLanguage": {".failure": "failure"},
            },
        ),
    )
    (project.root / ".lsp.json").mkdir()
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (("config", "set", "target", "claude"),),
        expected_returncodes=(0,),
        scenario_id="required-lsp-write-failure-config",
        cwd=project.root,
        env=environment,
    )

    result = runner.run(
        ("install", "--no-policy"),
        scenario_id="required-lsp-write-failure",
        cwd=project.root,
        env=environment,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "LSP configuration failed for target 'claude'" in output


def test_manifest_and_explicit_target_precedence_for_mcp_and_lsp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Manifest beats saved config, while the explicit flag beats both."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "effective-target-precedence",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        mcp_dependencies=(
            {
                "name": "precedence-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["precedence"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "precedence-lsp",
                "command": "precedence-language-server",
                "extensionToLanguage": {".precedence": "precedence"},
            },
        ),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (
            ("config", "set", "target", "claude"),
            ("install", "--no-policy"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="manifest-over-saved-target",
        cwd=project.root,
        env=environment,
    )
    assert (project.root / ".vscode" / "mcp.json").is_file()
    assert (project.root / ".github" / "lsp.json").is_file()
    assert not (project.root / ".mcp.json").exists()
    assert not (project.root / ".lsp.json").exists()

    runner.run_sequence(
        (("install", "--target", "claude", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="explicit-over-manifest-target",
        cwd=project.root,
        env=environment,
    )
    assert (project.root / ".mcp.json").is_file()
    assert (project.root / ".lsp.json").is_file()


def test_multi_target_exclusion_applies_to_mcp_and_lsp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A canonical exclusion removes its runtime alias from both service phases."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "effective-target-exclusion",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        mcp_dependencies=(
            {
                "name": "excluded-target-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["excluded"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "excluded-target-lsp",
                "command": "excluded-target-language-server",
                "extensionToLanguage": {".excluded": "excluded"},
            },
        ),
    )

    _runner(apm_binary_path).run_sequence(
        (
            (
                "install",
                "--target",
                "claude,copilot",
                "--exclude",
                "copilot",
                "--no-policy",
            ),
        ),
        expected_returncodes=(0,),
        scenario_id="effective-target-multi-exclusion",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert (project.root / ".mcp.json").is_file()
    assert (project.root / ".lsp.json").is_file()
    assert not (project.root / ".vscode" / "mcp.json").exists()
    assert not (project.root / ".github" / "lsp.json").exists()


def test_explicit_runtime_alias_overrides_saved_target_for_all_phases(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The legacy explicit runtime alias retains CLI-level precedence."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "explicit-runtime-precedence",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        mcp_dependencies=(
            {
                "name": "runtime-precedence-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["runtime"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "runtime-precedence-lsp",
                "command": "runtime-precedence-language-server",
                "extensionToLanguage": {".runtime": "runtime"},
            },
        ),
    )
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()
    runner.run_sequence(
        (
            ("config", "set", "target", "copilot"),
            ("install", "--runtime", "claude", "--no-policy"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="explicit-runtime-over-saved-target",
        cwd=project.root,
        env=environment,
    )

    assert (project.root / ".mcp.json").is_file()
    assert (project.root / ".lsp.json").is_file()
    assert not (project.root / ".vscode" / "mcp.json").exists()
    assert not (project.root / ".github" / "lsp.json").exists()


def test_mcp_only_target_rejects_required_lsp_without_copilot_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An MCP-only target must not inherit its primitive profile's LSP config."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "mcp-only-lsp",
        base_env=dict(os.environ),
    )
    package_factory = LocalPackageFactory(isolated.work_root)
    project = package_factory.create(
        "consumer",
        lsp_dependencies=(
            {
                "name": "unsupported-intellij-lsp",
                "command": "unsupported-language-server",
                "extensionToLanguage": {".unsupported": "unsupported"},
            },
        ),
    )

    result = _runner(apm_binary_path).run(
        ("install", "--target", "intellij", "--no-policy"),
        scenario_id="mcp-only-target-required-lsp",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert result.returncode != 0
    assert "no effective target supports LSP" in result.stdout + result.stderr
    assert not (project.root / ".github" / "lsp.json").exists()


def test_frozen_failure_writes_no_package_or_service_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Frozen validation remains earlier than package and service writes."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "effective-target-frozen",
        source_name="frozen-target-source",
        mcp_dependencies=(
            {
                "name": "frozen-target-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["frozen"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "frozen-target-lsp",
                "command": "frozen-language-server",
                "extensionToLanguage": {".frozen": "frozen"},
            },
        ),
        targets=(),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    runner.run_sequence(
        (("config", "set", "target", "claude"),),
        expected_returncodes=(0,),
        scenario_id="effective-target-frozen-config",
        cwd=fixture.project_root,
        env=environment,
    )

    result = runner.run(
        ("install", "--frozen", "--no-policy"),
        scenario_id="effective-target-frozen",
        cwd=fixture.project_root,
        env=environment,
    )

    assert result.returncode != 0
    assert not (fixture.project_root / "apm_modules").exists()
    assert not (fixture.project_root / ".claude").exists()
    assert not (fixture.project_root / ".mcp.json").exists()
    assert not (fixture.project_root / ".lsp.json").exists()


def test_unresolved_package_services_fail_before_package_deployment(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A service-bearing package cannot partially deploy before target resolution."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "unresolved-package-services",
        source_name="unresolved-service-source",
        mcp_dependencies=(
            {
                "name": "unresolved-package-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["unresolved"],
            },
        ),
        lsp_dependencies=(
            {
                "name": "unresolved-package-lsp",
                "command": "unresolved-language-server",
                "extensionToLanguage": {".unresolved": "unresolved"},
            },
        ),
        targets=(),
    )
    manifest_bytes = (fixture.project_root / "apm.yml").read_bytes()

    result = _runner(apm_binary_path).run(
        ("install", "--no-policy"),
        scenario_id="unresolved-package-services",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )

    assert result.returncode == 2
    assert "No harness detected" in result.stdout + result.stderr
    assert (fixture.project_root / "apm.yml").read_bytes() == manifest_bytes
    modules = fixture.project_root / "apm_modules"
    assert not modules.exists() or not any(modules.rglob("*"))
    assert not (fixture.project_root / ".mcp.json").exists()
    assert not (fixture.project_root / ".lsp.json").exists()


def test_uninstalling_one_shared_root_retains_shared_dependency_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One removed root must not orphan a dependency still reached by another."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "shared-transitive",
        base_env=dict(os.environ),
    )
    factory = LocalPackageFactory(isolated.work_root)
    project = factory.create("consumer", targets=("copilot",))
    root_a = factory.create("root-a")
    root_b = factory.create("root-b")
    shared = factory.create("shared")
    factory.add_relative_dependency(project, root_a)
    factory.add_relative_dependency(project, root_b)
    factory.add_relative_dependency(root_a, shared)
    factory.add_relative_dependency(root_b, shared)
    factory.add_instruction(root_a, "root-a", _instruction("root-a"))
    factory.add_instruction(root_b, "root-b", _instruction("root-b"))
    factory.add_instruction(shared, "shared", _instruction("shared"))
    environment = isolated.subprocess_env()

    _runner(apm_binary_path).run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-install",
        cwd=project.root,
        env=environment,
    )

    before = _dependency_rows(project.root)
    assert set(before) == {"_local/root-a", "_local/root-b", "_local/shared"}
    assert before["_local/shared"]["resolved_by"] in {"_local/root-a", "_local/root-b"}
    shared_instruction = project.root / ".github" / "instructions" / "shared.instructions.md"
    assert shared_instruction.is_file()

    _runner(apm_binary_path).run_sequence(
        (("uninstall", "../root-a"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-uninstall-first-root",
        cwd=project.root,
        env=environment,
    )

    after_first_uninstall = _dependency_rows(project.root)
    assert set(after_first_uninstall) == {"_local/root-b", "_local/shared"}
    assert after_first_uninstall["_local/shared"].get("resolved_ref") == before[
        "_local/shared"
    ].get("resolved_ref")
    assert after_first_uninstall["_local/shared"].get("resolved_commit") == before[
        "_local/shared"
    ].get("resolved_commit")
    assert shared_instruction.is_file()

    _runner(apm_binary_path).run_sequence(
        (("audit", "--ci", "--no-policy", "--format", "json"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-audit-after-first-uninstall",
        cwd=project.root,
        env=environment,
    )
