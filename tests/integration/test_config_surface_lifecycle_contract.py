"""Real lifecycle contracts for configuration and dependency graph state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

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
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_CLAUDE_LSP_PLUGIN = Path(".claude") / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"


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
            PurePosixPath(".github/mcp.json"),
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
    """Assert convergence while ignoring optional legacy generated-at metadata."""
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
        "copilot": ["portable-server"],
    }
    seed_ledger = DeploymentLedgerCodec.from_lockfile(seed_lock)
    seed_rows = tuple(
        record
        for _key, record in sorted(seed_ledger.records.items())
        if record.locator.target == "mcp"
    )
    assert {record.locator.runtime for record in seed_rows} == {"codex", "copilot"}

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
            "copilot": ["portable-server"],
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
    """VS Code stays untouched when Copilot migrates MCP ownership to .github."""
    from apm_cli.adapters.client.copilot import CopilotClientAdapter

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
    vscode_install_args = (
        "install",
        "--runtime",
        "vscode",
        "--target",
        "vscode",
        "--trust-transitive-mcp",
        "--no-policy",
    )
    copilot_install_args = (
        "install",
        "--runtime",
        "copilot",
        "--target",
        "copilot",
        "--trust-transitive-mcp",
        "--no-policy",
    )

    runner.run_sequence(
        (vscode_install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-vscode-install",
        cwd=fixture.project_root,
        env=environment,
    )
    vscode_installed = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(PurePosixPath(".vscode/mcp.json"),),
    )
    assert vscode_installed.manifest_bytes == manifest_bytes
    assert vscode_installed.deployment_records
    assert b"project-contract-server" in vscode_installed.mcp_state_bytes
    assert vscode_installed.file(".vscode/mcp.json").kind == "file"
    vscode_bytes = vscode_installed.file(".vscode/mcp.json").content

    runner.run_sequence(
        (copilot_install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-copilot-install",
        cwd=fixture.project_root,
        env=environment,
    )
    copilot_installed = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".github/mcp.json"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    assert copilot_installed.file(".vscode/mcp.json").content == vscode_bytes
    copilot_config = fixture.project_root / ".github" / "mcp.json"
    assert CopilotClientAdapter(project_root=fixture.project_root).get_config_path() == str(
        copilot_config
    )
    mcp_servers = json.loads(copilot_config.read_text(encoding="utf-8"))["mcpServers"]
    assert mcp_servers["project-contract-server"]["command"] == "echo"
    assert mcp_servers["project-contract-server"]["args"] == ["project-contract"]
    copilot_lock = LockFile.read(fixture.project_root / "apm.lock.yaml")
    assert copilot_lock is not None
    assert copilot_lock.mcp_target_servers == {"copilot": ["project-contract-server"]}
    assert (
        _audit_payload(
            runner,
            scenario_id="project-mcp-copilot-audit",
            cwd=fixture.project_root,
            environment=environment,
        )["passed"]
        is True
    )

    runner.run_sequence(
        (copilot_install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-copilot-reinstall",
        cwd=fixture.project_root,
        env=environment,
    )
    reinstalled = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".github/mcp.json"),
            PurePosixPath(".vscode/mcp.json"),
        ),
    )
    _assert_exact_lifecycle_state(copilot_installed, reinstalled)

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
        (copilot_install_args,),
        expected_returncodes=(0,),
        scenario_id="project-mcp-repair-install",
        cwd=fixture.project_root,
        env=environment,
    )
    repaired = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".github/mcp.json"),
            PurePosixPath(".vscode/mcp.json"),
        ),
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
            PurePosixPath(".github/mcp.json"),
        ),
    )
    assert broad.file(".github/mcp.json").kind == "file"
    assert b"managed-contract-server" in broad.file(".codex/config.toml").content
    assert b"user-authored" in broad.file(".codex/config.toml").content
    assert b"trust_level" in broad.file(".codex/config.toml").content
    assert (
        b'"target_servers":{"codex":["managed-contract-server"],"copilot":["managed-contract-server"]}'
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
            PurePosixPath(".github/mcp.json"),
        ),
    )
    codex_bytes = narrow.file(".codex/config.toml").content
    assert codex_bytes is not None
    assert b"managed-contract-server" not in codex_bytes
    assert b"user-authored" in codex_bytes
    assert b"trust_level" in codex_bytes
    assert b'"target_servers":{"copilot":["managed-contract-server"]}' in narrow.mcp_state_bytes
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
            PurePosixPath(".github/mcp.json"),
        ),
    )
    _assert_semantic_lifecycle_state(narrow, converged)
    assert unrelated_codex_file.read_bytes() == unrelated_codex_bytes

    before_audit = LifecycleStateSnapshot.capture(
        fixture.project_root,
        config_paths=(
            PurePosixPath(".codex/config.toml"),
            PurePosixPath(".github/mcp.json"),
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
            PurePosixPath(".github/mcp.json"),
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


def test_claude_lsp_install_writes_discoverable_skills_directory_plugin(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Claude LSP install must emit the plugin manifest Claude discovers."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "claude-lsp-discovery",
        source_name="claude-lsp-source",
        lsp_dependencies=(
            {
                "name": "basedpyright",
                "command": "uv",
                "args": ["run", "basedpyright-langserver", "--stdio"],
                "extensionToLanguage": {".py": "python", ".pyi": "python"},
            },
        ),
        targets=("claude",),
    )
    legacy_path = fixture.project_root / ".lsp.json"
    legacy_bytes = b'{"lspServers":{"user-owned":{"command":"keep-me"}}}\n'
    legacy_path.write_bytes(legacy_bytes)

    result = _runner(apm_binary_path).run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-discovery",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )

    plugin_path = fixture.project_root / _CLAUDE_LSP_PLUGIN
    assert json.loads(plugin_path.read_text(encoding="utf-8")) == {
        "name": "apm-lsp",
        "lspServers": {
            "basedpyright": {
                "command": "uv",
                "args": ["run", "basedpyright-langserver", "--stdio"],
                "extensionToLanguage": {".py": "python", ".pyi": "python"},
            }
        },
    }
    assert legacy_path.read_bytes() == legacy_bytes
    assert "Retained legacy .lsp.json" in result[0].stdout


def test_claude_lsp_collision_requires_force_and_force_reconciles(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed CLI must require explicit consent before replacement."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "claude-lsp-collision",
        base_env=dict(os.environ),
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        lsp_dependencies=(
            {
                "name": "pyright",
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            },
        ),
        targets=("claude",),
    )
    plugin_path = project.root / _CLAUDE_LSP_PLUGIN
    plugin_path.parent.mkdir(parents=True)
    original = b"[]\n"
    plugin_path.write_bytes(original)
    runner = _runner(apm_binary_path)
    environment = isolated.subprocess_env()

    (refused,) = runner.run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(1,),
        scenario_id="claude-lsp-collision-refused",
        cwd=project.root,
        env=environment,
    )
    assert "--force" in refused.stdout
    assert plugin_path.read_bytes() == original

    runner.run_sequence(
        (("install", "--no-policy", "--force"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-collision-forced",
        cwd=project.root,
        env=environment,
    )
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    assert plugin["name"] == "apm-lsp"
    assert set(plugin["lspServers"]) == {"pyright"}

    foreign = b'{"name":"custom-plugin","lspServers":{"pyright":{"command":"foreign"}}}\n'
    plugin_path.write_bytes(foreign)
    (update_refused,) = runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(1,),
        scenario_id="claude-lsp-update-collision-refused",
        cwd=project.root,
        env=environment,
    )
    assert "--force" in update_refused.stdout
    assert plugin_path.read_bytes() == foreign

    runner.run_sequence(
        (("update", "--yes", "--force"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-update-collision-forced",
        cwd=project.root,
        env=environment,
    )
    repaired = json.loads(plugin_path.read_text(encoding="utf-8"))
    assert repaired["name"] == "apm-lsp"
    assert repaired["lspServers"]["pyright"]["command"] == "pyright-langserver"


def test_claude_lsp_unapproved_package_is_not_discoverable(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Executable approval must bind a generated LSP to its declaring package."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "claude-lsp-approval",
        source_name="unapproved-lsp-source",
        lsp_dependencies=(
            {
                "name": "shared-approved-name",
                "command": "unapproved-language-server",
                "extensionToLanguage": {".unsafe": "unsafe"},
            },
        ),
        targets=("claude",),
    )
    manifest_path = fixture.project_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["executables"] = {
        "allow": {
            "different/package": {
                "lsp": True,
            }
        }
    }
    dump_yaml(manifest, manifest_path)

    (result,) = _runner(apm_binary_path).run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-unapproved-package",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )

    assert not (fixture.project_root / _CLAUDE_LSP_PLUGIN).exists()
    assert "apm policy explain" in result.stdout
    (partial,) = _runner(apm_binary_path).run_sequence(
        (("install", "--only", "mcp", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-unapproved-package-partial-install",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )
    assert not (fixture.project_root / _CLAUDE_LSP_PLUGIN).exists()
    assert "apm approve" not in partial.stdout
    (updated,) = _runner(apm_binary_path).run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-unapproved-package-update",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )
    assert not (fixture.project_root / _CLAUDE_LSP_PLUGIN).exists()
    assert "apm policy explain" in updated.stdout


def test_claude_lsp_approved_declaring_package_is_discoverable(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A package-scoped LSP grant must materialize that package's server."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "claude-lsp-approved",
        source_name="approved-lsp-source",
        lsp_dependencies=(
            {
                "name": "server-name-is-not-the-approval-key",
                "command": "approved-language-server",
                "extensionToLanguage": {".safe": "safe"},
            },
        ),
        targets=("claude",),
    )
    manifest_path = fixture.project_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    from apm_cli.models.apm_package import APMPackage

    approval_key = APMPackage.from_apm_yml(manifest_path).get_apm_dependencies()[0].get_unique_key()
    manifest["executables"] = {
        "allow": {
            approval_key: {
                "lsp": True,
            }
        }
    }
    dump_yaml(manifest, manifest_path)

    _runner(apm_binary_path).run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="claude-lsp-approved-package",
        cwd=fixture.project_root,
        env=fixture.isolated.subprocess_env(),
    )

    plugin = json.loads((fixture.project_root / _CLAUDE_LSP_PLUGIN).read_text(encoding="utf-8"))
    assert set(plugin["lspServers"]) == {"server-name-is-not-the-approval-key"}


def test_lsp_target_contraction_revokes_dropped_claude_plugin_entry(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Changing the manifest target must revoke the old executable config."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "lsp-target-contraction",
        source_name="target-contraction-source",
        lsp_dependencies=(
            {
                "name": "target-contraction-lsp",
                "command": "target-contraction-language-server",
                "extensionToLanguage": {".target": "target"},
            },
        ),
        targets=("claude",),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    runner.run_sequence(
        ((("install", "--no-policy")),),
        expected_returncodes=(0,),
        scenario_id="lsp-target-contraction-initial",
        cwd=fixture.project_root,
        env=environment,
    )
    claude_lsp = fixture.project_root / _CLAUDE_LSP_PLUGIN
    assert "target-contraction-lsp" in json.loads(claude_lsp.read_text())["lspServers"]

    manifest_path = fixture.project_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["copilot"]
    dump_yaml(manifest, manifest_path)
    runner.run_sequence(
        ((("install", "--no-policy")),),
        expected_returncodes=(0,),
        scenario_id="lsp-target-contraction-copilot",
        cwd=fixture.project_root,
        env=environment,
    )

    copilot_lsp = fixture.project_root / ".github" / "lsp.json"
    assert "target-contraction-lsp" in json.loads(copilot_lsp.read_text())["lspServers"]
    assert not claude_lsp.exists()
    lockfile = LockFile.read(fixture.project_root / "apm.lock.yaml")
    assert lockfile is not None
    assert lockfile.lsp_target_servers == {"copilot": ["target-contraction-lsp"]}


def test_only_mcp_does_not_materialize_root_lsp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The MCP-only filter must exclude root-project LSP declarations."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "only-mcp-lsp",
        base_env=dict(os.environ),
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        lsp_dependencies=(
            {
                "name": "must-not-deploy",
                "command": "must-not-run",
                "extensionToLanguage": {".none": "none"},
            },
        ),
        targets=("claude",),
    )

    _runner(apm_binary_path).run_sequence(
        (("install", "--only", "mcp", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="only-mcp-excludes-lsp",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert not (project.root / _CLAUDE_LSP_PLUGIN).exists()


def test_lsp_uninstall_cleanup_failure_is_nonzero_and_actionable(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Unsafe LSP cleanup must preserve foreign config and report recovery."""
    fixture = _create_git_lifecycle_project(
        tmp_path / "lsp-uninstall-cleanup-failure",
        source_name="lsp-uninstall-source",
        lsp_dependencies=(
            {
                "name": "uninstall-lsp",
                "command": "uninstall-language-server",
                "extensionToLanguage": {".uninstall": "uninstall"},
            },
        ),
        targets=("claude",),
    )
    runner = _runner(apm_binary_path)
    environment = fixture.isolated.subprocess_env()
    runner.run_sequence(
        (("install", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="lsp-uninstall-cleanup-install",
        cwd=fixture.project_root,
        env=environment,
    )
    plugin_path = fixture.project_root / _CLAUDE_LSP_PLUGIN
    foreign = b'{"name":"foreign-plugin","lspServers":{"uninstall-lsp":{"command":"keep"}}}\n'
    plugin_path.write_bytes(foreign)

    (result,) = runner.run_sequence(
        (("uninstall", fixture.source),),
        expected_returncodes=(1,),
        scenario_id="lsp-uninstall-cleanup-refusal",
        cwd=fixture.project_root,
        env=environment,
    )

    assert plugin_path.read_bytes() == foreign
    normalized_output = " ".join(result.stdout.split())
    assert "Uninstall incomplete" in normalized_output
    assert "'apm install'" in normalized_output


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
    claude_lsp = fixture.project_root / _CLAUDE_LSP_PLUGIN
    claude_lsp_plugin_dir = claude_lsp.parent
    claude_lsp_dir = claude_lsp_plugin_dir.parent
    claude_instruction = (
        fixture.project_root / ".claude" / "rules" / "saved-target-source-instruction.md"
    )
    assert server_name in json.loads(claude_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))["lspServers"]
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
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))["lspServers"]

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
    assert lsp_name in json.loads(claude_lsp.read_text(encoding="utf-8"))["lspServers"]
    unrelated_claude_skill = fixture.project_root / ".claude" / "skills" / "local-skill"
    unrelated_claude_skill.mkdir(parents=True)
    unrelated_skill_file = unrelated_claude_skill / "README.md"
    unrelated_skill_file.write_text("local Claude skill\n", encoding="utf-8")
    runner.run_sequence(
        (install, ("audit", "--ci")),
        expected_returncodes=(0, 0),
        scenario_id="saved-target-update-reinstall-audit",
        cwd=fixture.project_root,
        env=environment,
    )

    runner.run_sequence(
        (("uninstall", fixture.source),),
        expected_returncodes=(0,),
        scenario_id="saved-target-uninstall-reconcile",
        cwd=fixture.project_root,
        env=environment,
    )
    assert not claude_instruction.exists()
    post_uninstall_lock = LockFile.read(fixture.project_root / "apm.lock.yaml")
    assert not claude_lsp.exists(), (
        post_uninstall_lock.lsp_config_provenance if post_uninstall_lock is not None else None
    )
    assert not claude_lsp_plugin_dir.exists()
    assert not claude_lsp_dir.exists()
    assert unrelated_skill_file.read_text(encoding="utf-8") == "local Claude skill\n"

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
    assert (fixture.project_root / ".github" / "mcp.json").is_file()
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
    claude_lsp_plugin = json.loads((isolated.home / _CLAUDE_LSP_PLUGIN).read_text(encoding="utf-8"))
    assert "saved-target-user-mcp" in claude_config["mcpServers"]
    assert "saved-target-user-lsp" in claude_lsp_plugin["lspServers"]
    assert claude_lsp_plugin["name"] == "apm-lsp"
    assert (isolated.home / ".claude" / "rules" / "saved-target-user-instruction.md").is_file()

    runner.run_sequence(
        (("uninstall", "--global", str(package.root)),),
        expected_returncodes=(0,),
        scenario_id="saved-target-user-uninstall",
        cwd=cwd,
        env=environment,
    )

    assert not (isolated.home / _CLAUDE_LSP_PLUGIN).exists()
    assert not (isolated.home / ".claude" / "skills" / "apm-lsp").exists()


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


def test_global_direct_mcp_uses_user_manifest_and_runtime_config(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global direct MCP install must avoid project-scoped state."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-direct-mcp",
        base_env=dict(os.environ),
    )
    user_manifest = isolated.home / ".apm" / "apm.yml"
    (isolated.home / ".gemini").mkdir()
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / ".cursor").mkdir()
    project_manifest = project / "apm.yml"
    dump_yaml(
        {
            "name": "project-direct",
            "version": "0.1.0",
            "targets": ["cursor"],
            "dependencies": {"mcp": []},
        },
        project_manifest,
    )

    result = _runner(apm_binary_path).run_sequence(
        (
            (
                "install",
                "-g",
                "--mcp",
                "global-direct-server",
                "--no-policy",
                "--",
                "echo",
                "ready",
            ),
        ),
        expected_returncodes=(0,),
        scenario_id="global-direct-mcp",
        cwd=project,
        env=isolated.subprocess_env(),
    )[0]

    user_config = load_yaml(user_manifest)
    assert user_config["dependencies"]["mcp"][0]["name"] == "global-direct-server"
    project_config = load_yaml(project_manifest)
    assert project_config["dependencies"]["mcp"] == []
    gemini_config = json.loads(
        (isolated.home / ".gemini" / "settings.json").read_text(encoding="utf-8")
    )
    assert gemini_config["mcpServers"]["global-direct-server"] == {
        "args": ["ready"],
        "command": "echo",
    }
    assert "Install interrupted" not in result.stdout + result.stderr


def test_configured_mcp_registry_drives_global_direct_install(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Persisted registry config must reach a real direct MCP integration."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "configured-direct-registry",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    server_name = "io.github.apm/configured-registry"
    document = {
        "name": server_name,
        "description": "Configured direct registry fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@apm/configured-registry",
                "runtimeHint": "npx",
                "transport": {"type": "stdio"},
                "runtimeArguments": [],
            }
        ],
    }
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")

    with registry_factory.start(document) as registry:
        parsed_registry = urlparse(registry.url)
        assert parsed_registry.port is not None
        environment = isolated.subprocess_env()
        environment["APM_TEST_LOOPBACK_PORTS"] = str(parsed_registry.port)
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "0"
        runner = _runner(apm_binary_path)
        config_result = runner.run(
            ("config", "set", "mcp-registry-url", registry.url),
            scenario_id="configured-global-direct-registry-config",
            cwd=project,
            env=environment,
        )
        denied = runner.run(
            (
                "install",
                "-g",
                "--mcp",
                server_name,
                "--target",
                "claude",
                "--no-policy",
                "--verbose",
            ),
            scenario_id="configured-global-direct-registry-denied",
            cwd=project,
            env=environment,
        )
        assert config_result.returncode == 0
        assert denied.returncode == 2
        assert "MCP_REGISTRY_ALLOW_HTTP=1" in denied.stdout + denied.stderr
        assert list(registry.request_paths) == []

        (isolated.home / ".apm" / "apm.yml").unlink(missing_ok=True)
        (isolated.home / ".apm" / "apm.lock.yaml").unlink(missing_ok=True)
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        runner.run_sequence(
            (
                (
                    "install",
                    "-g",
                    "--mcp",
                    server_name,
                    "--target",
                    "claude",
                    "--no-policy",
                ),
            ),
            expected_returncodes=(0,),
            scenario_id="configured-global-direct-registry",
            cwd=project,
            env=environment,
        )

        assert any(path.startswith("/v0.1/servers?") for path in registry.request_paths)
        assert any(path.endswith("/versions/latest") for path in registry.request_paths)

    user_manifest = load_yaml(isolated.home / ".apm" / "apm.yml")
    entry = user_manifest["dependencies"]["mcp"][0]
    stored_registry = urlparse(entry["registry"])
    assert (
        stored_registry.scheme,
        stored_registry.hostname,
        stored_registry.port,
    ) == (
        parsed_registry.scheme,
        parsed_registry.hostname,
        parsed_registry.port,
    )
    claude_config = json.loads((isolated.home / ".claude.json").read_text(encoding="utf-8"))
    assert "configured-registry" in claude_config["mcpServers"]


def test_unknown_global_registry_server_changes_no_user_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Registry identity validation must precede every user-scope write."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "missing-direct-registry",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    document = {
        "name": "io.github.apm/known-server",
        "description": "Known registry fixture",
        "version": "1.0.0",
        "packages": [],
    }
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")

    with registry_factory.start(document) as registry:
        parsed_registry = urlparse(registry.url)
        assert parsed_registry.port is not None
        environment = isolated.subprocess_env()
        environment["APM_TEST_LOOPBACK_PORTS"] = str(parsed_registry.port)
        result = _runner(apm_binary_path).run(
            (
                "install",
                "-g",
                "--mcp",
                "io.github.apm/missing-server",
                "--target",
                "claude",
                "--registry",
                registry.url,
                "--no-policy",
            ),
            scenario_id="missing-global-direct-registry",
            cwd=project,
            env=environment,
        )

    assert result.returncode == 1, (result.stdout, result.stderr)
    output = result.stdout + result.stderr
    assert "Check the server name" in output
    assert "then retry" in output
    assert "no state was changed" in output
    assert not (isolated.home / ".apm" / "apm.yml").exists()
    assert not (isolated.home / ".apm" / "apm.lock.yaml").exists()
    assert not (isolated.home / ".claude.json").exists()


def test_unreachable_global_registry_changes_no_user_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A connection failure must precede every user-scope write."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "unreachable-direct-registry",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    document = {
        "name": "io.github.apm/known-server",
        "description": "Closed registry fixture",
        "version": "1.0.0",
        "packages": [],
    }
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")
    with registry_factory.start(document) as registry:
        registry_url = registry.url
        registry_port = urlparse(registry_url).port
        assert registry_port is not None

    environment = isolated.subprocess_env()
    environment["APM_TEST_LOOPBACK_PORTS"] = str(registry_port)
    environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    result = _runner(apm_binary_path).run(
        (
            "install",
            "-g",
            "--mcp",
            document["name"],
            "--target",
            "claude",
            "--registry",
            registry_url,
            "--no-policy",
        ),
        scenario_id="unreachable-global-direct-registry",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    output = result.stdout + result.stderr
    assert "Could not reach MCP registry" in output
    assert "verify the --registry URL" in output
    assert "reachability" in output
    assert "No state was changed." in output
    assert not (isolated.home / ".apm" / "apm.yml").exists()
    assert not (isolated.home / ".apm" / "apm.lock.yaml").exists()
    assert not (isolated.home / ".claude.json").exists()


def test_ambient_registry_source_is_pinned_for_replay(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An ambient private registry becomes credential-free manifest identity."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "ambient-registry-replay",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    server_name = "io.github.apm/ambient-replay"
    document = {
        "name": server_name,
        "description": "Ambient registry replay fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@apm/ambient-replay",
                "runtimeHint": "npx",
                "transport": {"type": "stdio"},
                "runtimeArguments": [],
            }
        ],
    }
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")

    with registry_factory.start(document) as registry:
        parsed_registry = urlparse(registry.url)
        assert parsed_registry.port is not None
        environment = isolated.subprocess_env()
        environment["APM_TEST_LOOPBACK_PORTS"] = str(parsed_registry.port)
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        environment["MCP_REGISTRY_URL"] = registry.url
        runner = _runner(apm_binary_path)
        runner.run_sequence(
            (
                (
                    "install",
                    "-g",
                    "--mcp",
                    server_name,
                    "--target",
                    "claude",
                    "--no-policy",
                ),
            ),
            expected_returncodes=(0,),
            scenario_id="ambient-registry-initial",
            cwd=project,
            env=environment,
        )
        initial_request_count = len(registry.request_paths)
        environment.pop("MCP_REGISTRY_URL")
        runner.run_sequence(
            (("install", "-g", "--only", "mcp", "--target", "claude", "--no-policy"),),
            expected_returncodes=(0,),
            scenario_id="ambient-registry-replay",
            cwd=project,
            env=environment,
        )
        assert len(registry.request_paths) == initial_request_count

        entry = load_yaml(isolated.home / ".apm" / "apm.yml")["dependencies"]["mcp"][0]
        stored_registry = urlparse(entry["registry"])
        assert (
            stored_registry.scheme,
            stored_registry.hostname,
            stored_registry.port,
        ) == (
            parsed_registry.scheme,
            parsed_registry.hostname,
            parsed_registry.port,
        )


def test_global_direct_mcp_filters_mixed_and_rejects_zero_supported_targets(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """User-scope capability filtering precedes state and survives replay."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-target-capability",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    environment = isolated.subprocess_env()
    runner = _runner(apm_binary_path)
    user_manifest = isolated.home / ".apm" / "apm.yml"

    rejected = runner.run(
        (
            "install",
            "-g",
            "--target",
            "vscode",
            "--mcp",
            "rejected-server",
            "--no-policy",
            "--",
            "echo",
            "rejected",
        ),
        scenario_id="global-zero-supported-target",
        cwd=project,
        env=environment,
    )

    assert rejected.returncode == 2
    assert not user_manifest.exists()

    excluded = runner.run(
        (
            "install",
            "-g",
            "--target",
            "claude",
            "--exclude",
            "claude",
            "--mcp",
            "excluded-server",
            "--no-policy",
            "--",
            "echo",
            "excluded",
        ),
        scenario_id="global-all-targets-excluded",
        cwd=project,
        env=environment,
    )
    assert excluded.returncode == 2
    excluded_output = excluded.stdout + excluded.stderr
    assert "removed by --exclude" in excluded_output
    assert "remove the exclusion" in excluded_output
    assert not user_manifest.exists()

    hermes = runner.run(
        (
            "install",
            "-g",
            "--target",
            "hermes",
            "--mcp",
            "disabled-hermes-server",
            "--no-policy",
            "--",
            "echo",
            "disabled",
        ),
        scenario_id="global-disabled-hermes",
        cwd=project,
        env=environment,
    )
    assert hermes.returncode == 2
    assert not user_manifest.exists()

    mixed, replay = runner.run_sequence(
        (
            (
                "install",
                "-g",
                "--target",
                "vscode,claude",
                "--mcp",
                "mixed-server",
                "--no-policy",
                "--",
                "echo",
                "mixed",
            ),
            (
                "install",
                "-g",
                "--mcp",
                "replay-server",
                "--no-policy",
                "--",
                "echo",
                "replay",
            ),
        ),
        expected_returncodes=(0, 0),
        scenario_id="global-mixed-target-replay",
        cwd=project,
        env=environment,
    )

    assert "Skipped workspace-only runtimes at user scope: vscode" in (mixed.stdout + mixed.stderr)
    assert "Skipped workspace-only runtimes" not in replay.stdout + replay.stderr
    user_config = load_yaml(user_manifest)
    assert user_config["targets"] == ["claude"]
    claude_config = json.loads((isolated.home / ".claude.json").read_text(encoding="utf-8"))
    assert set(claude_config["mcpServers"]) == {"mixed-server", "replay-server"}

    explicitly_excluded = runner.run(
        (
            "install",
            "-g",
            "--target",
            "vscode,claude",
            "--exclude",
            "vscode",
            "--mcp",
            "explicit-exclusion-server",
            "--no-policy",
            "--",
            "echo",
            "excluded",
        ),
        scenario_id="global-workspace-target-explicitly-excluded",
        cwd=project,
        env=environment,
    )
    assert explicitly_excluded.returncode == 0
    assert "Skipped workspace-only runtimes" not in (
        explicitly_excluded.stdout + explicitly_excluded.stderr
    )


def test_global_direct_mcp_dry_run_creates_no_user_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A user-scope preview must not bootstrap any persistent state."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-direct-dry-run",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()

    result = _runner(apm_binary_path).run(
        (
            "install",
            "-g",
            "--target",
            "claude",
            "--mcp",
            "dry-run-server",
            "--dry-run",
            "--no-policy",
            "--",
            "echo",
            "ready",
        ),
        scenario_id="global-direct-mcp-dry-run",
        cwd=project,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 0
    assert not (isolated.home / ".apm" / "apm.yml").exists()
    assert not (isolated.home / ".apm" / "apm.lock.yaml").exists()
    assert not (isolated.home / ".claude.json").exists()


@pytest.mark.parametrize(
    ("ambient_url", "secrets"),
    (
        (
            "https://user:userinfo-secret@registry.example.invalid"
            "?token=query-secret#fragment-secret",
            ("userinfo-secret", "query-secret", "fragment-secret"),
        ),
        (
            "https://user:port-secret@registry.example.invalid:notaport",
            ("port-secret",),
        ),
        (
            "https://registry.example.invalid:bare-port-secret",
            ("bare-port-secret",),
        ),
        (
            "REGISTRY_SECRET_SENTINEL",
            ("REGISTRY_SECRET_SENTINEL",),
        ),
    ),
)
def test_ambient_registry_credentials_never_reach_manifest_or_output(
    tmp_path: Path,
    apm_binary_path: Path,
    ambient_url: str,
    secrets: tuple[str, ...],
) -> None:
    """Ambient registry parse failures redact every credential-bearing component."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"ambient-registry-{len(secrets)}",
        base_env=dict(os.environ),
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    environment = isolated.subprocess_env()
    environment["MCP_REGISTRY_URL"] = ambient_url

    result = _runner(apm_binary_path).run(
        (
            "install",
            "-g",
            "--target",
            "claude",
            "--mcp",
            "ambient-registry-server",
            "--no-policy",
            "--verbose",
        ),
        scenario_id="ambient-registry-redaction",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 2
    combined_output = result.stdout + result.stderr
    user_manifest_path = isolated.home / ".apm" / "apm.yml"
    user_manifest = (
        user_manifest_path.read_text(encoding="utf-8") if user_manifest_path.is_file() else ""
    )
    for secret in secrets:
        assert secret not in combined_output
        assert secret not in user_manifest


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
    assert (project.root / _CLAUDE_LSP_PLUGIN).is_file()

    (project.root / ".mcp.json").unlink()
    (project.root / _CLAUDE_LSP_PLUGIN).unlink()
    runner.run_sequence(
        (("update", "--yes"),),
        expected_returncodes=(0,),
        scenario_id="saved-target-service-only-update-repair",
        cwd=project.root,
        env=environment,
    )
    assert (project.root / ".mcp.json").is_file()
    assert (project.root / _CLAUDE_LSP_PLUGIN).is_file()

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
    assert not (project.root / _CLAUDE_LSP_PLUGIN).exists()


def test_saved_copilot_target_projects_to_copilot_for_direct_mcp(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A saved Copilot profile must use the project-scoped Copilot MCP runtime."""
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

    assert (project / ".github" / "mcp.json").is_file()
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
    (project.root / _CLAUDE_LSP_PLUGIN).mkdir(parents=True)
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
    assert (project.root / ".github" / "mcp.json").is_file()
    assert (project.root / ".github" / "lsp.json").is_file()
    assert not (project.root / ".mcp.json").exists()
    assert not (project.root / _CLAUDE_LSP_PLUGIN).exists()

    runner.run_sequence(
        (("install", "--target", "claude", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="explicit-over-manifest-target",
        cwd=project.root,
        env=environment,
    )
    assert (project.root / ".mcp.json").is_file()
    assert (project.root / _CLAUDE_LSP_PLUGIN).is_file()


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
    assert (project.root / _CLAUDE_LSP_PLUGIN).is_file()
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
    assert (project.root / _CLAUDE_LSP_PLUGIN).is_file()
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
    assert not (fixture.project_root / _CLAUDE_LSP_PLUGIN).exists()


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
    assert not (fixture.project_root / _CLAUDE_LSP_PLUGIN).exists()


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
