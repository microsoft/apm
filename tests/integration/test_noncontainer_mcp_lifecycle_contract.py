"""Installed-binary lifecycle contract for non-container MCP launchers."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_NPM_SERVER = "com.example.lifecycle/npm-server"
_NPM_PACKAGE = "@example/noncontainer-npm"
_PYPI_SERVER = "com.example.lifecycle/pypi-server"
_PYPI_PACKAGE = "noncontainer-pypi"
_GENERIC_SERVER = "com.example.lifecycle/generic-server"
_GENERIC_PACKAGE = "Example.NonContainer"
_CONFIG_VARIABLE = "MCP_LIFECYCLE_CONFIG"
_CONFIG_VALUE = "/fixture/noncontainer.json"
_SECRET_VARIABLE = "MCP_LIFECYCLE_SECRET"
_SECRET_VALUE = "must-not-be-written"


@dataclass(frozen=True)
class _TargetCase:
    """One native config surface driven through an installed APM binary."""

    runtime: str
    config_path: PurePosixPath
    config_section: str
    external_config_root: bool


_TARGET_CASES = (
    _TargetCase(
        runtime="vscode",
        config_path=PurePosixPath(".vscode/mcp.json"),
        config_section="servers",
        external_config_root=False,
    ),
)


def _server_documents() -> tuple[dict[str, object], ...]:
    """Return npm, pypi, and generic registry documents in both arg spellings."""
    return (
        {
            "name": _NPM_SERVER,
            "description": "Typed npm launcher lifecycle fixture",
            "version": "1.0.0",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": _NPM_PACKAGE,
                    "runtimeHint": "npx",
                    "transport": {"type": "stdio"},
                    "runtimeArguments": [
                        {"type": "positional", "value": "--stdio"},
                        {
                            "type": "named",
                            "name": "--config",
                            "value": f"{{{_CONFIG_VARIABLE}}}",
                            "variables": {
                                _CONFIG_VARIABLE: {
                                    "description": "NPM launcher config",
                                    "isRequired": True,
                                }
                            },
                        },
                        {
                            "type": "named",
                            "name": "--cache",
                            "value": "{MCP_LIFECYCLE_OPTIONAL_CACHE}",
                            "isRequired": False,
                            "variables": {
                                "MCP_LIFECYCLE_OPTIONAL_CACHE": {
                                    "description": "Optional cache",
                                    "isRequired": False,
                                }
                            },
                        },
                        {
                            "type": "named",
                            "name": "--port",
                            "value": "{MCP_LIFECYCLE_PORT}",
                            "variables": {
                                "MCP_LIFECYCLE_PORT": {
                                    "description": "Default launcher port",
                                    "default": "8080",
                                }
                            },
                        },
                    ],
                    "packageArguments": [
                        {
                            "type": "named",
                            "name": "--transport",
                            "value": "stdio",
                        }
                    ],
                }
            ],
        },
        {
            "name": _PYPI_SERVER,
            "description": "Legacy pypi launcher lifecycle fixture",
            "version": "1.0.0",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": _PYPI_PACKAGE,
                    "runtimeHint": "uvx",
                    "transport": {"type": "stdio"},
                    "runtimeArguments": [
                        {"value_hint": "--legacy-mode"},
                        {
                            "value_hint": f"{{{_CONFIG_VARIABLE}}}",
                            "variables": {
                                _CONFIG_VARIABLE: {
                                    "description": "PyPI launcher config",
                                    "isRequired": True,
                                }
                            },
                        },
                    ],
                }
            ],
        },
        {
            "name": _GENERIC_SERVER,
            "description": "Typed generic launcher lifecycle fixture",
            "version": "1.0.0",
            "packages": [
                {
                    "registryType": "nuget",
                    "identifier": _GENERIC_PACKAGE,
                    "runtimeHint": "dnx",
                    "transport": {"type": "stdio"},
                    "runtimeArguments": [
                        {
                            "type": "named",
                            "name": "--secret",
                            "value": f"{{{_SECRET_VARIABLE}}}",
                            "variables": {
                                _SECRET_VARIABLE: {
                                    "description": "Generic launcher secret",
                                    "isRequired": True,
                                    "isSecret": True,
                                }
                            },
                        },
                        {
                            "type": "named",
                            "name": "--mode",
                            "value": "strict",
                        },
                    ],
                }
            ],
        },
    )


def _config_root(
    case: _TargetCase,
    isolated: IsolatedApmEnvironment,
    project: Path,
) -> Path:
    """Return the bounded root containing the target's native config."""
    return isolated.home / ".copilot" if case.external_config_root else project


def _snapshot(
    case: _TargetCase,
    isolated: IsolatedApmEnvironment,
    project: Path,
) -> LifecycleStateSnapshot:
    """Capture manifest, lock state, deployment records, and native config."""
    if case.external_config_root:
        return LifecycleStateSnapshot.capture(
            project,
            external_roots=(
                LifecycleStateRoot(
                    root_id="copilot-config",
                    target="copilot",
                    scope="project",
                    path=_config_root(case, isolated, project),
                    config_paths=(case.config_path,),
                ),
            ),
        )
    return LifecycleStateSnapshot.capture(project, config_paths=(case.config_path,))


def _native_document(
    case: _TargetCase,
    isolated: IsolatedApmEnvironment,
    project: Path,
) -> dict[str, Any]:
    """Read the exact JSON document generated for one target."""
    path = _config_root(case, isolated, project) / case.config_path
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _server_key(case: _TargetCase, full_name: str) -> str:
    """Return the target-native key for one registry server."""
    return full_name if case.runtime == "vscode" else full_name.rsplit("/", 1)[-1]


def _assert_idempotent_state(
    first: LifecycleStateSnapshot,
    converged: LifecycleStateSnapshot,
) -> None:
    """Compare every durable field except lock generation time."""
    assert converged.manifest_bytes == first.manifest_bytes
    assert converged.deployment_records == first.deployment_records
    assert converged.mcp_state_bytes == first.mcp_state_bytes
    assert converged.lsp_state_bytes == first.lsp_state_bytes
    assert converged.files == first.files
    assert converged.semantic_bytes == first.semantic_bytes


def _expected_args(case: _TargetCase) -> dict[str, list[str]]:
    """Return exact typed argv for all registry packages on one target."""
    secret = (
        "${input:mcp-lifecycle-secret}" if case.runtime == "vscode" else f"${{{_SECRET_VARIABLE}}}"
    )
    return {
        _NPM_SERVER: [
            "-y",
            _NPM_PACKAGE,
            "--stdio",
            "--config",
            _CONFIG_VALUE,
            "--port",
            "8080",
            "--transport",
            "stdio",
        ],
        _PYPI_SERVER: [
            _PYPI_PACKAGE,
            "--legacy-mode",
            _CONFIG_VALUE,
        ],
        _GENERIC_SERVER: [
            _GENERIC_PACKAGE,
            "--secret",
            secret,
            "--mode",
            "strict",
        ],
    }


def _assert_launcher_document(
    case: _TargetCase,
    document: dict[str, Any],
) -> None:
    """Assert executable fidelity, full argv, and one package identity each."""
    section = document[case.config_section]
    assert isinstance(section, dict)
    expected_args = _expected_args(case)
    expected_commands = {
        _NPM_SERVER: "npx",
        _PYPI_SERVER: "uvx",
        _GENERIC_SERVER: "dnx",
    }
    package_names = {
        _NPM_SERVER: _NPM_PACKAGE,
        _PYPI_SERVER: _PYPI_PACKAGE,
        _GENERIC_SERVER: _GENERIC_PACKAGE,
    }
    actual_args = {
        server_name: section[_server_key(case, server_name)]["args"]
        for server_name in expected_commands
    }
    assert actual_args == expected_args
    for server_name, command in expected_commands.items():
        server = section[_server_key(case, server_name)]
        assert server["command"] == command
        assert server["args"].count(package_names[server_name]) == 1
    assert _SECRET_VALUE not in json.dumps(document, sort_keys=True)


@pytest.mark.parametrize("case", _TARGET_CASES, ids=lambda case: case.runtime)
def test_noncontainer_launchers_converge_across_install_update_and_audit(
    case: _TargetCase,
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Exercise npm, pypi, and generic launchers without real package runtimes."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"noncontainer-{case.runtime}",
        base_env=dict(os.environ),
    )
    project = (
        LocalPackageFactory(isolated.work_root)
        .create(
            f"noncontainer-{case.runtime}-consumer",
            targets=("copilot",),
        )
        .root
    )
    if not case.external_config_root:
        (project / case.config_path.parent).mkdir(parents=True, exist_ok=True)
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=360,
    )
    install_env = isolated.subprocess_env(
        overrides={
            _CONFIG_VARIABLE: _CONFIG_VALUE,
            _SECRET_VARIABLE: _SECRET_VALUE,
        }
    )
    install_env.pop("PYTHONPATH")
    install_env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    repeat_install = (
        "install",
        "--target",
        case.runtime,
        "--trust-transitive-mcp",
        "--no-policy",
    )
    update = ("update", "--yes", "--target", case.runtime)

    with ExitStack() as stack:
        registries: list[LocalMcpRegistry] = [
            stack.enter_context(registry_factory.start(document))
            for document in _server_documents()
        ]
        source = LocalPackageFactory(isolated.package_root).create(
            f"noncontainer-{case.runtime}-source",
            mcp_dependencies=tuple(
                {
                    "name": document["name"],
                    "registry": registry.url,
                }
                for document, registry in zip(
                    _server_documents(),
                    registries,
                    strict=True,
                )
            ),
        )
        initial_install = (
            "install",
            str(source.root),
            "--target",
            case.runtime,
            "--trust-transitive-mcp",
            "--no-policy",
        )
        runner.run_sequence(
            (initial_install,),
            expected_returncodes=(0,),
            scenario_id=f"noncontainer-{case.runtime}-initial-install",
            cwd=project,
            env=install_env,
        )
        first = _snapshot(case, isolated, project)
        _assert_launcher_document(case, _native_document(case, isolated, project))

        runner.run_sequence(
            (repeat_install, update),
            expected_returncodes=(0, 0),
            scenario_id=f"noncontainer-{case.runtime}-repeat-update",
            cwd=project,
            env=install_env,
        )
        converged = _snapshot(case, isolated, project)
        _assert_idempotent_state(first, converged)
        _assert_launcher_document(case, _native_document(case, isolated, project))
        for registry in registries:
            assert any(path.startswith("/v0.1/servers?") for path in registry.request_paths)
            assert any(path.endswith("/versions/latest") for path in registry.request_paths)

    before_audit = _snapshot(case, isolated, project)
    audit = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        scenario_id=f"noncontainer-{case.runtime}-offline-audit",
        cwd=project,
        env=install_env,
    )
    assert audit.returncode == 0, f"stdout={audit.stdout!r}\nstderr={audit.stderr!r}"
    assert json.loads(audit.stdout)["passed"] is True
    assert _snapshot(case, isolated, project) == before_audit


def _malformed_server_document() -> dict[str, object]:
    """Return a registry package with a non-string explicit registry type."""
    return {
        "name": "com.example.lifecycle/malformed-server",
        "description": "Malformed registry type lifecycle fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": ["pypi"],
                "identifier": "malformed-package",
                "runtimeHint": "uvx",
                "transport": {"type": "stdio"},
                "runtimeArguments": [],
            }
        ],
    }


@pytest.mark.parametrize("case", _TARGET_CASES, ids=lambda case: case.runtime)
def test_malformed_registry_type_errors_without_native_config_write(
    case: _TargetCase,
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A malformed registry type fails before either target writes its config."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"malformed-{case.runtime}",
        base_env=dict(os.environ),
    )
    project = (
        LocalPackageFactory(isolated.work_root)
        .create(
            f"malformed-{case.runtime}-consumer",
            targets=("copilot",),
        )
        .root
    )
    if not case.external_config_root:
        (project / case.config_path.parent).mkdir(parents=True, exist_ok=True)
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    install_env = isolated.subprocess_env()
    install_env.pop("PYTHONPATH")
    install_env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    config_path = _config_root(case, isolated, project) / case.config_path

    with registry_factory.start(_malformed_server_document()) as registry:
        source = LocalPackageFactory(isolated.package_root).create(
            f"malformed-{case.runtime}-source",
            mcp_dependencies=(
                {
                    "name": _malformed_server_document()["name"],
                    "registry": registry.url,
                },
            ),
        )
        result = runner.run(
            (
                "install",
                str(source.root),
                "--runtime",
                case.runtime,
                "--target",
                "copilot",
                "--trust-transitive-mcp",
                "--no-policy",
            ),
            scenario_id=f"malformed-{case.runtime}-install",
            cwd=project,
            env=install_env,
        )

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert "registry_name must be a string" in output
    assert not config_path.exists()
