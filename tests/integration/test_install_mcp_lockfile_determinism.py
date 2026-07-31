"""Installed-CLI lifecycle proofs for MCP lockfile determinism."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest
import tomlkit

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SERVER_NAME = "local-lifecycle-server"
_REGISTRY_SERVER = "com.example/registry-lifecycle"
_INSTALL_BOTH = (
    "install",
    "--target",
    "copilot,codex",
    "--trust-transitive-mcp",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_INSTALL_COPILOT = (
    "install",
    "--target",
    "copilot",
    "--trust-transitive-mcp",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_FROZEN_BOTH = (*_INSTALL_BOTH, "--frozen")
_UPDATE_BOTH = (
    "update",
    "--yes",
    "--target",
    "copilot,codex",
    "--parallel-downloads",
    "0",
)
_AUDIT = ("audit", "--ci", "--no-policy", "--format", "json")
_TARGET_SERVERS = {
    "codex": [_SERVER_NAME],
    "vscode": [_SERVER_NAME],
}
_CONFIG_PATHS = (
    PurePosixPath(".vscode/mcp.json"),
    PurePosixPath(".codex/config.toml"),
    PurePosixPath(".agents/skills/local-lifecycle/SKILL.md"),
)


@dataclass(frozen=True)
class _Scenario:
    """One isolated installed-binary project with a local MCP package."""

    isolated: IsolatedApmEnvironment
    packages: LocalPackageFactory
    project: LocalPackage
    runner: ApmLifecycleRunner
    environment: dict[str, str]


def _self_defined_server(name: str = _SERVER_NAME) -> dict[str, object]:
    """Return one deterministic self-defined stdio MCP declaration."""
    return {
        "name": name,
        "registry": False,
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "local_lifecycle_server"],
    }


def _new_scenario(root: Path, apm_binary_path: Path) -> _Scenario:
    """Create a mixed local-package project with user-authored MCP config."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    project = packages.create(
        "mcp-lock-consumer",
        targets=("copilot", "codex"),
    )
    dependency_factory = LocalPackageFactory(project.root / "packages")
    dependency = dependency_factory.create(
        "local-mcp",
        mcp_dependencies=(_self_defined_server(),),
    )
    dependency_factory.add_skill(
        dependency,
        "local-lifecycle",
        (
            "---\n"
            "name: local-lifecycle\n"
            "description: Mixed non-MCP lifecycle fixture\n"
            "---\n"
            "# Local lifecycle\n"
        ),
    )
    packages.replace_apm_dependencies(
        project,
        ({"path": "./packages/local-mcp", "alias": dependency.name},),
    )
    _write_user_configs(project.root)
    return _Scenario(
        isolated=isolated,
        packages=packages,
        project=project,
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=90,
            scenario_timeout_seconds=360,
        ),
        environment=isolated.subprocess_env(),
    )


def _write_user_configs(project_root: Path) -> None:
    """Seed native target configs with entries APM must preserve."""
    vscode_config = project_root / ".vscode" / "mcp.json"
    vscode_config.parent.mkdir(parents=True)
    vscode_config.write_text(
        json.dumps(
            {
                "servers": {
                    "user-authored": {
                        "type": "http",
                        "url": "https://user.example.invalid/mcp",
                    }
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    codex_config = project_root / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        r"[projects.'c:\src\project']"
        "\n"
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.user-authored]\n"
        'command = "user-command"\n',
        encoding="utf-8",
    )


def _run_ok(
    scenario: _Scenario,
    args: tuple[str, ...],
    scenario_id: str,
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    """Run one installed command and retain complete failure evidence."""
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=cwd or scenario.project.root,
        env=environment or scenario.environment,
    )
    assert result.returncode == 0, (
        f"command={result.command!r}\n"
        f"cwd={result.cwd!s}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    return result


def _snapshot(project_root: Path) -> LifecycleStateSnapshot:
    """Capture exact lock, MCP, deployment, and native-config state."""
    return LifecycleStateSnapshot.capture(
        project_root,
        targets=("copilot", "codex"),
        config_paths=_CONFIG_PATHS,
    )


def _assert_exact_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    """Assert every durable lifecycle surface remains byte-identical."""
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _lock(project_root: Path) -> LockFile:
    lock = LockFile.read(project_root / "apm.lock.yaml")
    assert lock is not None
    return lock


def _file_identity(path: Path) -> tuple[int, int, int]:
    """Return metadata that changes when atomic replacement occurs."""
    metadata = path.stat()
    return metadata.st_mtime_ns, metadata.st_size, metadata.st_ino


def _assert_user_configs(project_root: Path) -> None:
    """Verify APM-managed transitions never remove user-authored config."""
    vscode = json.loads((project_root / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    user_url = urlparse(vscode["servers"]["user-authored"]["url"])
    assert (user_url.scheme, user_url.hostname, user_url.path) == (
        "https",
        "user.example.invalid",
        "/mcp",
    )
    codex = tomlkit.parse((project_root / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex["mcp_servers"]["user-authored"]["command"] == "user-command"
    assert codex["projects"][r"c:\src\project"]["trust_level"] == "trusted"


@pytest.mark.lifecycle_smoke
def test_installed_mcp_lifecycle_is_no_write_until_real_target_change(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install, update, frozen, and audit converge; one target change writes."""
    scenario = _new_scenario(tmp_path / "installed-lifecycle", apm_binary_path)
    project_root = scenario.project.root
    lock_path = project_root / "apm.lock.yaml"

    _run_ok(scenario, _INSTALL_BOTH, "mcp-lock-install-first")
    baseline = _snapshot(project_root)
    baseline_lock = _lock(project_root)
    baseline_identity = _file_identity(lock_path)
    assert baseline_lock.mcp_target_servers == _TARGET_SERVERS
    assert baseline_lock.mcp_config_provenance == {_SERVER_NAME: "local-mcp"}
    assert baseline_lock.mcp_configs[_SERVER_NAME]["registry"] is False
    assert baseline_lock.deployment_ledger.records
    assert baseline.file(".agents/skills/local-lifecycle/SKILL.md").kind == "file"
    _assert_user_configs(project_root)

    time.sleep(0.02)
    _run_ok(scenario, _INSTALL_BOTH, "mcp-lock-install-unchanged")
    repeated = _snapshot(project_root)
    _assert_exact_state(baseline, repeated)
    assert _file_identity(lock_path) == baseline_identity

    _run_ok(scenario, _UPDATE_BOTH, "mcp-lock-update-unchanged")
    updated = _snapshot(project_root)
    _assert_exact_state(baseline, updated)
    assert _file_identity(lock_path) == baseline_identity

    _run_ok(scenario, _FROZEN_BOTH, "mcp-lock-frozen-unchanged")
    frozen = _snapshot(project_root)
    _assert_exact_state(baseline, frozen)
    assert _file_identity(lock_path) == baseline_identity

    audit = _run_ok(scenario, _AUDIT, "mcp-lock-audit-unchanged")
    audit_payload = json.loads(audit.stdout)
    assert audit_payload["passed"] is True
    audited = _snapshot(project_root)
    _assert_exact_state(baseline, audited)
    assert _file_identity(lock_path) == baseline_identity

    scenario.packages.set_targets(scenario.project, ("copilot",))
    time.sleep(0.02)
    _run_ok(scenario, _INSTALL_COPILOT, "mcp-lock-target-change")
    changed = _snapshot(project_root)
    changed_lock = _lock(project_root)
    changed_identity = _file_identity(lock_path)
    assert changed.lockfile_bytes != baseline.lockfile_bytes
    assert changed_lock.generated_at != baseline_lock.generated_at
    assert changed_lock.mcp_target_servers == {"copilot": [_SERVER_NAME]}
    assert changed_identity != baseline_identity
    assert {
        record.locator.runtime
        for record in changed.deployment_records
        if record.locator.target == "mcp"
    } == {"copilot"}
    _assert_user_configs(project_root)

    time.sleep(0.02)
    _run_ok(scenario, _INSTALL_COPILOT, "mcp-lock-target-converged")
    converged = _snapshot(project_root)
    _assert_exact_state(changed, converged)
    assert _file_identity(lock_path) == changed_identity


@pytest.mark.lifecycle_smoke
def test_redirected_root_registry_install_converges_without_rewrite(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A local registry install under --root preserves exact lock bytes."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "redirected-registry",
        base_env=dict(os.environ),
    )
    registry_factory = LocalMcpRegistryFactory(isolated.root / "mcp-registries")
    server_document = {
        "name": _REGISTRY_SERVER,
        "description": "Redirected-root registry fixture",
        "version": "1.0.0",
        "remotes": [
            {
                "transport_type": "http",
                "url": "https://mcp.example.invalid/registry-lifecycle",
            }
        ],
    }
    with registry_factory.start(server_document) as registry:
        project_factory = LocalPackageFactory(isolated.work_root)
        project = project_factory.create(
            "registry-source",
            mcp_dependencies=({"name": _REGISTRY_SERVER, "registry": registry.url},),
            targets=("copilot", "codex"),
        )
        dependency_factory = LocalPackageFactory(project.root / "packages")
        regular_dependency = dependency_factory.create("regular-package")
        dependency_factory.add_skill(
            regular_dependency,
            "redirected-regular",
            (
                "---\n"
                "name: redirected-regular\n"
                "description: Redirected mixed-dependency fixture\n"
                "---\n"
                "# Redirected regular package\n"
            ),
        )
        project_factory.replace_apm_dependencies(
            project,
            ({"path": "./packages/regular-package", "alias": regular_dependency.name},),
        )
        output_root = isolated.root / "redirected-output"
        output_root.mkdir()
        environment = isolated.subprocess_env(
            overrides={"MCP_REGISTRY_ALLOW_HTTP": "1"},
        )
        parsed_registry = urlparse(registry.url)
        assert parsed_registry.hostname == "127.0.0.1"
        assert parsed_registry.port is not None
        environment["APM_TEST_LOOPBACK_PORTS"] = str(parsed_registry.port)
        scenario = _Scenario(
            isolated=isolated,
            packages=LocalPackageFactory(isolated.package_root),
            project=project,
            runner=ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=90),
            environment=environment,
        )
        args = (
            "install",
            "--root",
            str(output_root),
            "--target",
            "copilot,codex",
            "--no-policy",
            "--parallel-downloads",
            "0",
        )
        source_manifest = project.manifest_path.read_bytes()

        _run_ok(scenario, args, "redirected-registry-first")
        first = LifecycleStateSnapshot.capture(
            output_root,
            config_paths=(
                PurePosixPath(".vscode/mcp.json"),
                PurePosixPath(".codex/config.toml"),
            ),
        )
        lock_path = output_root / "apm.lock.yaml"
        first_identity = _file_identity(lock_path)

        time.sleep(0.02)
        _run_ok(scenario, args, "redirected-registry-unchanged")
        second = LifecycleStateSnapshot.capture(
            output_root,
            config_paths=(
                PurePosixPath(".vscode/mcp.json"),
                PurePosixPath(".codex/config.toml"),
            ),
        )

        _assert_exact_state(first, second)
        assert _file_identity(lock_path) == first_identity
        assert project.manifest_path.read_bytes() == source_manifest
        assert registry.request_paths


def test_installed_mcp_write_failure_exits_nonzero_without_partial_lock(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A real MCP persistence failure is explicit and leaves the lock intact."""
    scenario = _new_scenario(tmp_path / "installed-write-failure", apm_binary_path)
    project_root = scenario.project.root
    lock_path = project_root / "apm.lock.yaml"
    _run_ok(scenario, _INSTALL_BOTH, "mcp-write-failure-baseline")
    baseline_bytes = lock_path.read_bytes()

    scenario.packages.set_targets(scenario.project, ("copilot",))
    failing_environment = dict(scenario.environment)
    failing_environment["APM_TEST_FAIL_LOCK_REPLACE"] = "1"
    result = scenario.runner.run(
        _INSTALL_COPILOT,
        scenario_id="mcp-write-failure-injected",
        cwd=project_root,
        env=failing_environment,
    )

    assert result.returncode != 0
    assert lock_path.read_bytes() == baseline_bytes
    assert list(project_root.glob("apm-atomic-*")) == []
