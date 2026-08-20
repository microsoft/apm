"""Installed-CLI lifecycle contract for dependency development MCP isolation."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import tomlkit

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_ROOT_PROD = "com.example/root-prod"
_ROOT_DEV = "com.example/root-dev"
_DEP_PROD = "dep-prod"
_DEP_DEV = "dep-dev"
_EXPECTED_NAMES = (_ROOT_PROD, _ROOT_DEV, _DEP_PROD)
_REMOTE_URLS = {
    _ROOT_PROD: "https://mcp.example.invalid/root-prod",
    _ROOT_DEV: "https://mcp.example.invalid/root-dev",
    _DEP_PROD: "https://mcp.example.invalid/dep-prod",
    _DEP_DEV: "https://mcp.example.invalid/dep-dev",
}


@dataclass(frozen=True)
class _TargetCase:
    """One native MCP target projection of the shared current view."""

    runtime: str
    manifest_target: str
    state_target: str
    config_path: PurePosixPath
    config_section: str
    signal_directory: PurePosixPath
    trailing_newline: bool


_TARGET_CASES = (
    _TargetCase(
        runtime="claude",
        manifest_target="claude",
        state_target="claude",
        config_path=PurePosixPath(".mcp.json"),
        config_section="mcpServers",
        signal_directory=PurePosixPath(".claude"),
        trailing_newline=True,
    ),
    _TargetCase(
        runtime="codex",
        manifest_target="codex",
        state_target="codex",
        config_path=PurePosixPath(".codex/config.toml"),
        config_section="mcp_servers",
        signal_directory=PurePosixPath(".codex"),
        trailing_newline=True,
    ),
)


@dataclass(frozen=True)
class _Scenario:
    """One isolated nested-package consumer and its local registries."""

    isolated: IsolatedApmEnvironment
    project: Path
    environment: dict[str, str]
    registries: dict[str, LocalMcpRegistry]
    case: _TargetCase


def _server_document(name: str) -> dict[str, object]:
    """Return one deterministic remote-only MCP Registry v0.1 document."""
    return {
        "name": name,
        "description": f"Lifecycle fixture for {name}",
        "version": "1.0.0",
        "remotes": [
            {
                "transport_type": "http",
                "url": _REMOTE_URLS[name],
            }
        ],
    }


def _registry_entry(name: str, registry: LocalMcpRegistry) -> dict[str, object]:
    """Return a manifest entry pinned to one scenario-local registry."""
    return {"name": name, "registry": registry.url}


def _dependency_entry(name: str) -> dict[str, object]:
    """Return one self-defined dependency-package server."""
    return {
        "name": name,
        "registry": False,
        "transport": "http",
        "url": _REMOTE_URLS[name],
    }


def _native_key(case: _TargetCase, name: str) -> str:
    """Return the adapter key derived from a registry reference."""
    return name.rsplit("/", maxsplit=1)[-1]


def _start_registries(
    isolated: IsolatedApmEnvironment,
    stack: ExitStack,
) -> dict[str, LocalMcpRegistry]:
    """Start one isolated endpoint per declared server."""
    factory = LocalMcpRegistryFactory(isolated.root / "mcp-registries")
    return {
        name: stack.enter_context(factory.start(_server_document(name)))
        for name in (_ROOT_PROD, _ROOT_DEV)
    }


def _create_scenario(
    root: Path,
    case: _TargetCase,
    stack: ExitStack,
) -> _Scenario:
    """Create root -> Git aggregator -> local nested dependency."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    registries = _start_registries(isolated, stack)

    source_factory = LocalPackageFactory(isolated.package_root)
    aggregator = source_factory.create("aggregator")
    dependency = LocalPackageFactory(aggregator.root / "packages").create(
        "dep",
        mcp_dependencies=(_dependency_entry(_DEP_PROD),),
        dev_mcp_dependencies=(_dependency_entry(_DEP_DEV),),
    )
    source_factory.replace_apm_dependencies(
        aggregator,
        ({"path": "./packages/dep", "alias": dependency.name},),
    )

    base_environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=base_environment,
    )
    repository = repositories.create("aggregator", source_tree=aggregator.root)
    repositories.commit(repository, message="seed nested MCP dependency")
    source_url = "https://gitlab.example.invalid/contracts/aggregator.git"
    environment = repositories.url_rewrite_subprocess_env(repository, source_url)
    environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    environment.pop("PYTHONPATH", None)

    project = LocalPackageFactory(isolated.work_root).create(
        f"{case.runtime}-consumer",
        dependencies=(
            {
                "git": source_url,
                "type": "gitlab",
                "ref": "main",
                "alias": "aggregator",
            },
        ),
        mcp_dependencies=(_registry_entry(_ROOT_PROD, registries[_ROOT_PROD]),),
        dev_mcp_dependencies=(_registry_entry(_ROOT_DEV, registries[_ROOT_DEV]),),
        targets=(case.manifest_target,),
    )
    project.root.joinpath(*case.signal_directory.parts).mkdir(parents=True)
    return _Scenario(
        isolated=isolated,
        project=project.root,
        environment=environment,
        registries=registries,
        case=case,
    )


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return a bounded installed-binary runner."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=480,
    )


def _install_args(case: _TargetCase) -> tuple[str, ...]:
    """Return the normal install command for one target."""
    return (
        "install",
        "--runtime",
        case.runtime,
        "--target",
        case.manifest_target,
        "--trust-transitive-mcp",
        "--no-policy",
        "--verbose",
    )


def _run_ok(
    runner: ApmLifecycleRunner,
    scenario: _Scenario,
    args: tuple[str, ...],
    scenario_id: str,
) -> CommandResult:
    """Run one command and retain complete failure evidence."""
    result = runner.run(
        args,
        scenario_id=scenario_id,
        cwd=scenario.project,
        env=scenario.environment,
    )
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _snapshot(scenario: _Scenario) -> LifecycleStateSnapshot:
    """Capture exact lock-derived MCP state and native config bytes."""
    return LifecycleStateSnapshot.capture(
        scenario.project,
        config_paths=(scenario.case.config_path,),
    )


def _native_document(
    case: _TargetCase,
    include_user_entry: bool = False,
) -> dict[str, object]:
    """Return the exact managed native server mapping."""
    servers: dict[str, object] = {}
    for name in _EXPECTED_NAMES:
        if case.runtime == "codex":
            config = {"url": _REMOTE_URLS[name], "id": ""}
        else:
            config = {"type": "http", "url": _REMOTE_URLS[name]}
        servers[_native_key(case, name)] = config
    if include_user_entry:
        servers["user-authored"] = {
            "type": "http",
            "url": "https://user.example.invalid/mcp",
        }
    return servers


def _expected_native_bytes(
    case: _TargetCase,
    *,
    include_user_entry: bool = False,
) -> bytes:
    """Serialize the exact target-native bytes in adapter write order."""
    document: dict[str, object] = {
        case.config_section: _native_document(case, include_user_entry),
    }
    if case.runtime == "codex":
        return tomlkit.dumps(document).encode("ascii")
    suffix = "\n" if case.trailing_newline else ""
    return f"{json.dumps(document, indent=2)}{suffix}".encode("ascii")


def _expected_mcp_state_bytes(scenario: _Scenario) -> bytes:
    """Return the exact canonical lock-derived MCP state."""
    state = {
        "servers": sorted(_EXPECTED_NAMES),
        "configs": {
            name: (
                {
                    "name": name,
                    "registry": scenario.registries[name].url,
                }
                if name in scenario.registries
                else _dependency_entry(name)
            )
            for name in sorted(_EXPECTED_NAMES)
        },
        "target_servers": {
            scenario.case.state_target: sorted(_EXPECTED_NAMES),
        },
        "provenance": {
            _DEP_PROD: "dep",
        },
    }
    return json.dumps(
        state,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _assert_current_state(
    scenario: _Scenario,
    snapshot: LifecycleStateSnapshot,
    *,
    include_user_entry: bool = False,
) -> None:
    """Assert exact native bytes, provenance, and lock-derived server state."""
    config = snapshot.file(scenario.case.config_path.as_posix())
    assert config.content == _expected_native_bytes(
        scenario.case,
        include_user_entry=include_user_entry,
    )
    assert snapshot.mcp_state_bytes == _expected_mcp_state_bytes(scenario)
    assert _native_key(scenario.case, _DEP_DEV).encode("ascii") not in config.content
    assert _DEP_DEV.encode("ascii") not in snapshot.mcp_state_bytes
    assert snapshot.lockfile_bytes is not None
    assert _DEP_DEV.encode("ascii") not in snapshot.lockfile_bytes


def _assert_byte_idempotent(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
    config_path: PurePosixPath,
) -> None:
    """Compare exact config/provenance bytes and timestamp-neutral durable state."""
    assert (
        actual.file(config_path.as_posix()).content == expected.file(config_path.as_posix()).content
    )
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.semantic_bytes == expected.semantic_bytes


def _assert_nested_lock(project: Path) -> None:
    """Prove the MCP declarer came from a depth-two package."""
    lock = load_yaml(project / "apm.lock.yaml")
    assert isinstance(lock, dict)
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    rows = {
        row["name"]: row
        for row in dependencies
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    assert rows["aggregator"].get("depth", 1) == 1
    assert rows["dep"]["depth"] == 2
    assert lock["mcp_config_provenance"] == {_DEP_PROD: "dep"}


def _assert_no_dev_log(*results: CommandResult) -> None:
    """Prove fresh lifecycle output never names an excluded dev server."""
    for result in results:
        assert _DEP_DEV not in f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("case", _TARGET_CASES, ids=lambda case: case.runtime)
def test_dependency_dev_mcp_isolated_across_installed_lifecycle(
    case: _TargetCase,
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Fresh install, repeat, update, reinstall, and audit stay isolated."""
    with ExitStack() as stack:
        scenario = _create_scenario(tmp_path / case.runtime, case, stack)
        runner = _runner(apm_binary_path)
        install_args = _install_args(case)

        installed = _run_ok(runner, scenario, install_args, f"{case.runtime}-install")
        first = _snapshot(scenario)
        _assert_current_state(scenario, first)
        _assert_nested_lock(scenario.project)

        repeated = _run_ok(runner, scenario, install_args, f"{case.runtime}-repeat")
        repeat_state = _snapshot(scenario)
        _assert_current_state(scenario, repeat_state)
        _assert_byte_idempotent(first, repeat_state, case.config_path)

        updated = _run_ok(
            runner,
            scenario,
            ("update", "--yes", "--target", case.manifest_target),
            f"{case.runtime}-update",
        )
        update_state = _snapshot(scenario)
        _assert_current_state(scenario, update_state)
        _assert_byte_idempotent(first, update_state, case.config_path)

        reinstalled = _run_ok(
            runner,
            scenario,
            install_args,
            f"{case.runtime}-reinstall",
        )
        reinstall_state = _snapshot(scenario)
        _assert_current_state(scenario, reinstall_state)
        _assert_byte_idempotent(first, reinstall_state, case.config_path)

        before_audit = _snapshot(scenario)
        audited = _run_ok(
            runner,
            scenario,
            ("audit", "--ci", "--no-policy", "--format", "json"),
            f"{case.runtime}-audit",
        )
        assert json.loads(audited.stdout)["passed"] is True
        assert _snapshot(scenario) == before_audit
        _assert_no_dev_log(installed, repeated, updated, reinstalled, audited)
        assert scenario.registries[_ROOT_PROD].request_paths
        assert scenario.registries[_ROOT_DEV].request_paths


def _plant_stale_dependency_dev_state(scenario: _Scenario) -> None:
    """Mimic the pre-fix config and provenance written by a normal install."""
    config_path = scenario.project.joinpath(*scenario.case.config_path.parts)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config[scenario.case.config_section]
    servers["user-authored"] = _native_document(
        scenario.case,
        include_user_entry=True,
    )["user-authored"]
    servers[_native_key(scenario.case, _DEP_DEV)] = {
        "type": "http",
        "url": _REMOTE_URLS[_DEP_DEV],
    }
    config_path.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")

    lock_path = scenario.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    assert isinstance(lock, dict)
    lock["mcp_servers"].append(_DEP_DEV)
    lock["mcp_configs"][_DEP_DEV] = _dependency_entry(_DEP_DEV)
    lock["mcp_target_servers"][scenario.case.state_target].append(_DEP_DEV)
    lock["mcp_config_provenance"][_DEP_DEV] = "dep"
    dump_yaml(lock, lock_path)


def test_stale_dependency_dev_mcp_repair_and_frozen_no_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Frozen dry-run rejects without writes; install and update repair stale state."""
    case = _TARGET_CASES[0]
    with ExitStack() as stack:
        scenario = _create_scenario(tmp_path / "repair", case, stack)
        runner = _runner(apm_binary_path)
        install_args = _install_args(case)
        _run_ok(runner, scenario, install_args, "repair-seed-install")

        _plant_stale_dependency_dev_state(scenario)
        stale = _snapshot(scenario)
        frozen = runner.run(
            (*install_args, "--frozen", "--dry-run"),
            scenario_id="repair-frozen-dry-run",
            cwd=scenario.project,
            env=scenario.environment,
        )
        assert frozen.returncode == 1
        frozen_output = f"{frozen.stdout}\n{frozen.stderr}"
        assert "--frozen" in frozen_output
        assert _DEP_DEV in frozen_output
        assert _snapshot(scenario) == stale

        _run_ok(runner, scenario, install_args, "repair-normal-install")
        repaired = _snapshot(scenario)
        _assert_current_state(scenario, repaired, include_user_entry=True)

        _plant_stale_dependency_dev_state(scenario)
        _run_ok(
            runner,
            scenario,
            ("update", "--yes", "--target", case.manifest_target),
            "repair-normal-update",
        )
        updated = _snapshot(scenario)
        _assert_current_state(scenario, updated, include_user_entry=True)
        _assert_byte_idempotent(repaired, updated, case.config_path)

        before_audit = _snapshot(scenario)
        audit = _run_ok(
            runner,
            scenario,
            ("audit", "--ci", "--no-policy", "--format", "json"),
            "repair-audit",
        )
        assert json.loads(audit.stdout)["passed"] is True
        assert _snapshot(scenario) == before_audit
