"""Installed-binary lifecycle contract for MCP Registry v0.1 OCI packages."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import tomllib

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SERVER_NAME = "com.example.lifecycle/oci-server"
_SERVER_KEY = "oci-server"
_IMAGE = "registry.example.invalid/team/oci-server:1.4.0"
_ENV_NAME = "OCI_TEST_ENV"
_ENV_VALUE = "lifecycle-value"
_RUNTIME_ARGS = ["run", "--rm", "-i", "--label", "apm.lifecycle=true", "-e"]
_PACKAGE_ARGS = ["--transport", "stdio"]


@dataclass(frozen=True)
class _TargetCase:
    """One native MCP config surface exercised through the installed binary."""

    runtime: str
    manifest_target: str
    config_path: PurePosixPath
    config_section: str
    env_operand: str
    server_key: str = _SERVER_KEY
    external_config_root: bool = False


_CASES = (
    _TargetCase(
        runtime="copilot",
        manifest_target="copilot",
        config_path=PurePosixPath("mcp-config.json"),
        config_section="mcpServers",
        env_operand=f"{_ENV_NAME}=${{{_ENV_NAME}}}",
        external_config_root=True,
    ),
    _TargetCase(
        runtime="codex",
        manifest_target="codex",
        config_path=PurePosixPath(".codex/config.toml"),
        config_section="mcp_servers",
        env_operand=_ENV_NAME,
    ),
    _TargetCase(
        runtime="gemini",
        manifest_target="gemini",
        config_path=PurePosixPath(".gemini/settings.json"),
        config_section="mcpServers",
        env_operand=f"{_ENV_NAME}={_ENV_VALUE}",
    ),
    _TargetCase(
        runtime="vscode",
        manifest_target="copilot",
        config_path=PurePosixPath(".vscode/mcp.json"),
        config_section="servers",
        env_operand=_ENV_NAME,
        server_key=_SERVER_NAME,
    ),
)


def _server_document() -> dict[str, object]:
    """Return the reported v0.1 package shape with no image runtime argument."""
    return {
        "name": _SERVER_NAME,
        "description": "Lifecycle fixture for OCI launcher canonicalization",
        "version": "1.4.0",
        "packages": [
            {
                "registryType": "oci",
                "identifier": _IMAGE,
                "runtimeHint": "docker",
                "transport": {"type": "stdio"},
                "runtimeArguments": [
                    *({"value": value, "type": "positional"} for value in _RUNTIME_ARGS),
                    {"value": _ENV_NAME, "type": "positional"},
                ],
                "packageArguments": [
                    {"value": value, "type": "positional"} for value in _PACKAGE_ARGS
                ],
                "environmentVariables": [
                    {
                        "name": _ENV_NAME,
                        "description": "Lifecycle fixture environment value",
                        "isRequired": True,
                    }
                ],
            }
        ],
    }


def _config_root(case: _TargetCase, isolated: IsolatedApmEnvironment, project: Path) -> Path:
    """Return the root containing the target's native config."""
    if case.external_config_root:
        return isolated.home / ".copilot"
    return project


def _snapshot(
    case: _TargetCase,
    isolated: IsolatedApmEnvironment,
    project: Path,
) -> LifecycleStateSnapshot:
    """Capture the manifest, lock, and exact native config bytes."""
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


def _native_server_config(
    case: _TargetCase,
    isolated: IsolatedApmEnvironment,
    project: Path,
) -> dict[str, object]:
    """Read the target's generated native server mapping."""
    config_root = _config_root(case, isolated, project)
    path = config_root / case.config_path
    if path.suffix == ".toml":
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
    section = document[case.config_section]
    assert isinstance(section, dict)
    server = section[case.server_key]
    assert isinstance(server, dict)
    return server


def _assert_idempotent_state(
    first: LifecycleStateSnapshot,
    second: LifecycleStateSnapshot,
) -> None:
    """Assert every durable field except the lock generation timestamp is exact."""
    assert second.manifest_bytes == first.manifest_bytes
    assert second.deployment_records == first.deployment_records
    assert second.mcp_state_bytes == first.mcp_state_bytes
    assert second.lsp_state_bytes == first.lsp_state_bytes
    assert second.files == first.files
    assert second.semantic_bytes == first.semantic_bytes


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.runtime)
def test_oci_mcp_install_lifecycle_is_cross_adapter_and_idempotent(
    case: _TargetCase,
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install, reinstall, and audit one OCI package without Docker execution."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"oci-{case.runtime}",
        base_env=dict(os.environ),
    )
    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"oci-{case.runtime}-consumer",
        targets=(case.manifest_target,),
    ).root
    if not case.external_config_root:
        (project / case.config_path.parent).mkdir(parents=True, exist_ok=True)
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    install_env = isolated.subprocess_env(overrides={_ENV_NAME: _ENV_VALUE})
    install_env.pop("PYTHONPATH")
    install_env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    reinstall_args = (
        "install",
        "--runtime",
        case.runtime,
        "--target",
        case.manifest_target,
        "--trust-transitive-mcp",
        "--no-policy",
    )

    with registry_factory.start(_server_document()) as registry:
        source = LocalPackageFactory(isolated.package_root).create(
            f"oci-{case.runtime}-source",
            mcp_dependencies=(
                {
                    "name": _SERVER_NAME,
                    "registry": registry.url,
                },
            ),
        )
        first_command = (
            "install",
            str(source.root),
            "--runtime",
            case.runtime,
            "--target",
            case.manifest_target,
            "--trust-transitive-mcp",
            "--no-policy",
        )
        runner.run_sequence(
            (first_command,),
            expected_returncodes=(0,),
            scenario_id=f"oci-{case.runtime}-install",
            cwd=project,
            env=install_env,
        )
        first = _snapshot(case, isolated, project)
        server = _native_server_config(case, isolated, project)
        assert server["command"] == "docker"
        expected_args = [
            *_RUNTIME_ARGS,
            case.env_operand,
            _IMAGE,
            *_PACKAGE_ARGS,
        ]
        assert server["args"] == expected_args
        assert expected_args.count(_IMAGE) == 1
        assert expected_args.count("-e") == 1
        assert "-y" not in expected_args

        runner.run_sequence(
            (reinstall_args,),
            expected_returncodes=(0,),
            scenario_id=f"oci-{case.runtime}-reinstall",
            cwd=project,
            env=install_env,
        )
        second = _snapshot(case, isolated, project)
        _assert_idempotent_state(first, second)
        assert any(path.startswith("/v0.1/servers?") for path in registry.request_paths)
        assert any(path.endswith("/versions/latest") for path in registry.request_paths)

    audit_env = isolated.subprocess_env(overrides={_ENV_NAME: _ENV_VALUE})
    audit = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        scenario_id=f"oci-{case.runtime}-offline-audit",
        cwd=project,
        env=audit_env,
    )
    assert audit.returncode == 0, f"stdout={audit.stdout!r}\nstderr={audit.stderr!r}"
    audit_payload = json.loads(audit.stdout)
    assert audit_payload["passed"] is True
