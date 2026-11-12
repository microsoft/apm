"""Regression contract for non-container MCP package launcher arguments."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter

pytestmark = pytest.mark.unit

_TARGETS = ("vscode", "copilot")


def _render(
    target: str,
    package: dict[str, object],
    tmp_path: Path,
    *,
    runtime_vars: dict[str, object] | None = None,
    env_overrides: dict[str, object] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Render one package through a concrete adapter with registry I/O mocked."""
    server = {
        "id": "com.example/noncontainer",
        "name": "com.example/noncontainer",
        "packages": [package],
    }
    if target == "vscode":
        with (
            patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
        ):
            adapter = VSCodeClientAdapter(project_root=tmp_path)
        config, inputs = adapter._format_server_config(
            server,
            runtime_vars=runtime_vars,
            env_overrides=env_overrides,
        )
        return config, inputs

    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        adapter = CopilotClientAdapter(project_root=tmp_path)
    return (
        adapter._format_server_config(
            server,
            runtime_vars=runtime_vars,
            env_overrides=env_overrides,
        ),
        [],
    )


@pytest.mark.parametrize("target", _TARGETS)
def test_npm_value_arguments_survive_with_runtime_hint(
    target: str,
    tmp_path: Path,
) -> None:
    """Typed v0.1 values remain ordered behind one npm package operand."""
    name = "@scope/mcp-server"
    package = {
        "name": name,
        "registry_name": "npm",
        "runtime_hint": "bunx",
        "runtime_arguments": [
            {"value": "--stdio", "type": "positional"},
            {"name": "--config", "value": "./mcp.yaml", "type": "named"},
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["command"] == "bunx"
    assert config["args"] == ["-y", name, "--stdio", "--config", "./mcp.yaml"]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize("target", _TARGETS)
def test_pypi_value_groups_are_atomic_and_resolved(
    target: str,
    tmp_path: Path,
) -> None:
    """Named values stay paired and unresolved optional groups disappear whole."""
    name = "mcp-server-fetch"
    package = {
        "name": name,
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "name": "--config",
                "value": "{config_path}",
                "type": "named",
                "variables": {"config_path": {"description": "config"}},
            },
            {
                "name": "--cache",
                "value": "{cache_path}",
                "type": "named",
                "is_required": False,
                "variables": {"cache_path": {"description": "cache"}},
            },
            {
                "name": "--temp",
                "value": "{temp_path}",
                "type": "named",
                "is_required": False,
                "variables": {"temp_path": {"description": "temp"}},
            },
            {"name": "--verbose", "type": "named"},
            {"value": "--stdio", "type": "positional"},
        ],
    }

    config, _ = _render(
        target,
        package,
        tmp_path,
        runtime_vars={
            "config_path": "/workspace/mcp.json",
            "cache_path": "/workspace/.cache",
        },
    )

    assert config["command"] == "uvx"
    assert config["args"] == [
        name,
        "--config",
        "/workspace/mcp.json",
        "--cache",
        "/workspace/.cache",
        "--verbose",
        "--stdio",
    ]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize("target", _TARGETS)
def test_generic_value_arguments_preserve_executable_and_order(
    target: str,
    tmp_path: Path,
) -> None:
    """Generic launchers use runtime_hint rather than an invented npx command."""
    name = "Azure.Mcp"
    package = {
        "name": name,
        "registry_name": "nuget",
        "runtime_hint": "dnx",
        "runtime_arguments": [
            {"value": "--stdio", "type": "positional"},
            {"name": "--mode", "value": "strict", "type": "named"},
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["command"] == "dnx"
    assert config["args"] == [name, "--stdio", "--mode", "strict"]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize(
    ("registry_name", "runtime_hint", "name", "prefix"),
    (
        ("npm", "npx", "@scope/empty", ["-y"]),
        ("pypi", "uvx", "mcp-server-empty", []),
        ("nuget", "dnx", "Empty.Mcp", []),
    ),
)
@pytest.mark.parametrize("target", _TARGETS)
def test_empty_arguments_still_name_the_package_once(
    target: str,
    registry_name: str,
    runtime_hint: str,
    name: str,
    prefix: list[str],
    tmp_path: Path,
) -> None:
    """An empty registry argv still yields one executable package operand."""
    package = {
        "name": name,
        "registry_name": registry_name,
        "runtime_hint": runtime_hint,
        "runtime_arguments": [],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == [*prefix, name]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize("target", _TARGETS)
def test_legacy_full_argv_keeps_existing_package_position(
    target: str,
    tmp_path: Path,
) -> None:
    """Legacy value_hint argv with the package already present is not prefixed."""
    name = "mcp-server-legacy"
    package = {
        "name": name,
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {"value_hint": name},
            {"value_hint": "--legacy-mode"},
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == [name, "--legacy-mode"]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize("target", _TARGETS)
def test_legacy_argv_without_package_gets_one_package_operand(
    target: str,
    tmp_path: Path,
) -> None:
    """Legacy value_hint remains compatible when it contains flags only."""
    name = "mcp-server-legacy"
    package = {
        "name": name,
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {"value_hint": "--legacy-mode"},
            {
                "value_hint": "{legacy_config}",
                "variables": {"legacy_config": {"description": "config"}},
            },
        ],
    }

    config, _ = _render(
        target,
        package,
        tmp_path,
        runtime_vars={"legacy_config": "/workspace/legacy.json"},
    )

    assert config["args"] == [name, "--legacy-mode", "/workspace/legacy.json"]
    assert config["args"].count(name) == 1


@pytest.mark.parametrize("target", _TARGETS)
def test_package_identity_merge_does_not_remove_named_flag_value(
    target: str,
    tmp_path: Path,
) -> None:
    """Only a positional package operand is identity, not an equal named value."""
    name = "@scope/repeated"
    package = {
        "name": name,
        "registry_name": "npm",
        "runtime_hint": "npx",
        "runtime_arguments": [
            {"value": "-y", "type": "positional"},
            {"value": name, "type": "positional"},
            {"name": "--label", "value": name, "type": "named"},
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == ["-y", name, "--label", name]


@pytest.mark.parametrize("target", _TARGETS)
def test_package_arguments_follow_runtime_arguments(
    target: str,
    tmp_path: Path,
) -> None:
    """Package argument groups remain after runtime groups and package identity."""
    name = "mcp-server-ordered"
    package = {
        "name": name,
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [{"name": "--python", "value": "3.12", "type": "named"}],
        "package_arguments": [
            {"name": "--transport", "value": "stdio", "type": "named"},
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == [
        name,
        "--python",
        "3.12",
        "--transport",
        "stdio",
    ]


def test_vscode_argument_secret_uses_existing_input_contract(tmp_path: Path) -> None:
    """Argument placeholders reuse the VS Code prompt input, never the secret."""
    package = {
        "name": "mcp-server-secret",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "value": "{MCP_TEST_SECRET}",
                "type": "positional",
                "variables": {
                    "MCP_TEST_SECRET": {
                        "description": "Secret used by the launcher",
                        "isRequired": True,
                        "isSecret": True,
                    },
                    "UNUSED_SECRET": {
                        "description": "Not referenced by the template",
                        "isRequired": True,
                        "isSecret": True,
                    },
                },
            }
        ],
    }

    config, inputs = _render(
        "vscode",
        package,
        tmp_path,
        runtime_vars={"MCP_TEST_SECRET": "must-not-be-written"},
    )
    input_id = VSCodeClientAdapter._vscode_argument_secret_input_id(
        "com.example/noncontainer",
        "MCP_TEST_SECRET",
    )

    assert config["args"] == ["mcp-server-secret", f"${{input:{input_id}}}"]
    assert "must-not-be-written" not in repr(config)
    assert inputs == [
        {
            "type": "promptString",
            "id": input_id,
            "description": "Secret used by the launcher",
            "password": True,
        }
    ]


def test_copilot_argument_secret_uses_runtime_placeholder(tmp_path: Path) -> None:
    """Copilot-family configs retain runtime secret substitution."""
    package = {
        "name": "mcp-server-secret",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "value": "{MCP_TEST_SECRET}",
                "type": "positional",
                "variables": {
                    "MCP_TEST_SECRET": {
                        "description": "Secret used by the launcher",
                        "isRequired": True,
                        "isSecret": True,
                    }
                },
            }
        ],
    }

    config, _ = _render(
        "copilot",
        package,
        tmp_path,
        runtime_vars={"MCP_TEST_SECRET": "must-not-be-written"},
    )

    assert config["args"] == ["mcp-server-secret", "${MCP_TEST_SECRET}"]
    assert "must-not-be-written" not in repr(config)


@pytest.mark.parametrize("target", _TARGETS)
def test_registry_variable_default_resolves_without_runtime_input(
    target: str,
    tmp_path: Path,
) -> None:
    """A v0.1 variable default participates before target fallbacks."""
    package = {
        "name": "mcp-server-default",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "name": "--port",
                "value": "{port}",
                "type": "named",
                "variables": {
                    "port": {
                        "description": "Server port",
                        "default": "8080",
                    }
                },
            }
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == ["mcp-server-default", "--port", "8080"]


@pytest.mark.parametrize("target", _TARGETS)
def test_legacy_package_argument_equal_to_name_is_not_launcher_identity(
    target: str,
    tmp_path: Path,
) -> None:
    """Binary arguments cannot suppress the separate package operand."""
    name = "mcp-server-package-arg"
    package = {
        "name": name,
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [],
        "package_arguments": [{"value_hint": name}],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == [name, name]


def test_vscode_python_launcher_keeps_module_execution(tmp_path: Path) -> None:
    """An empty Python argv continues to launch the derived module."""
    package = {
        "name": "mcp-server-fetch",
        "registry_name": "pypi",
        "runtime_hint": "python",
        "runtime_arguments": [],
    }

    config, _ = _render("vscode", package, tmp_path)

    assert config["command"] == "python3"
    assert config["args"] == ["-m", "mcp_server_fetch"]


def test_vscode_python_typed_flags_follow_module_execution(tmp_path: Path) -> None:
    """Typed Python flags cannot replace the derived module entrypoint."""
    package = {
        "name": "mcp-server-fetch",
        "registry_name": "pypi",
        "runtime_hint": "python",
        "runtime_arguments": [{"name": "-u", "type": "named"}],
    }

    config, _ = _render("vscode", package, tmp_path)

    assert config["command"] == "python3"
    assert config["args"] == ["-m", "mcp_server_fetch", "-u"]


def test_vscode_python_typed_command_is_an_explicit_entrypoint(
    tmp_path: Path,
) -> None:
    """Python -c stays authoritative instead of gaining a derived module."""
    package = {
        "name": "mcp-server-inline",
        "registry_name": "pypi",
        "runtime_hint": "python",
        "runtime_arguments": [
            {"value": "-c", "type": "positional"},
            {"value": "print('ready')", "type": "positional"},
        ],
    }

    config, _ = _render("vscode", package, tmp_path)

    assert config["args"] == ["-c", "print('ready')"]


def test_vscode_python_package_args_do_not_claim_runtime_entrypoint(
    tmp_path: Path,
) -> None:
    """A package-level -m value cannot suppress the runtime module."""
    package = {
        "name": "mcp-server-fetch",
        "registry_name": "pypi",
        "runtime_hint": "python",
        "runtime_arguments": [{"name": "-u", "type": "named"}],
        "package_arguments": [{"value": "-m", "type": "positional"}],
    }

    config, _ = _render("vscode", package, tmp_path)

    assert config["args"] == ["-m", "mcp_server_fetch", "-u", "-m"]


@pytest.mark.parametrize("target", _TARGETS)
def test_empty_optional_positional_argument_is_skipped(
    target: str,
    tmp_path: Path,
) -> None:
    """Only explicitly optional positional entries may omit their value."""
    package = {
        "name": "mcp-server-optional",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "type": "positional",
                "isRequired": False,
            },
            {
                "type": "named",
                "name": "--optional-flag",
                "isRequired": False,
                "variables": {},
            },
        ],
    }

    config, _ = _render(target, package, tmp_path)

    assert config["args"] == ["mcp-server-optional"]


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize(
    "runtime_arguments",
    (
        [{"value": "--stdio", "type": "unsupported"}],
        [{"value": "--stdio", "type": ["positional"]}],
        ["not-a-registry-entry"],
        [{"value": "{path}", "type": "positional", "variables": ["path"]}],
        [{"value": "stdio", "type": "named"}],
        [{"value": 7, "type": "positional"}],
        [{"name": "--port", "value": 7, "type": "named"}],
        [{"type": "positional"}],
        [
            {
                "value": "{path}",
                "type": "positional",
                "variables": {"path": "not-metadata"},
            }
        ],
        [
            {
                "value": "{token}",
                "type": "positional",
                "variables": {"token": {"isSecret": "yes"}},
            }
        ],
        [
            {
                "value": "{port}",
                "type": "positional",
                "variables": {"port": {"default": 8080}},
            }
        ],
    ),
)
def test_malformed_argument_entries_fail_closed(
    target: str,
    runtime_arguments: list[object],
    tmp_path: Path,
) -> None:
    """Malformed registry argument metadata must not produce a partial argv."""
    package = {
        "name": "mcp-server-malformed",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": runtime_arguments,
    }

    with pytest.raises(ValueError, match=r"MCP package argument"):
        _render(target, package, tmp_path)


@pytest.mark.parametrize("target", _TARGETS)
def test_unresolved_required_argument_variable_fails_closed(
    target: str,
    tmp_path: Path,
) -> None:
    """A required group cannot be written with an unresolved registry variable."""
    package = {
        "name": "mcp-server-required",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [
            {
                "name": "--config",
                "value": "{config_path}",
                "type": "named",
                "variables": {"config_path": {"description": "config"}},
            }
        ],
    }

    with pytest.raises(ValueError, match=r"required MCP package argument") as error:
        _render(target, package, tmp_path)
    assert "'config_path'" in str(error.value)
    assert "'mcp-server-required'" in str(error.value)


@pytest.mark.parametrize("target", _TARGETS)
def test_missing_required_positional_names_package(
    target: str,
    tmp_path: Path,
) -> None:
    """Malformed positional metadata identifies the affected package."""
    package = {
        "name": "mcp-server-missing",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [{"type": "positional"}],
    }

    with pytest.raises(
        ValueError,
        match=r"missing a required positional value",
    ) as error:
        _render(target, package, tmp_path)
    assert "'mcp-server-missing'" in str(error.value)


def test_copilot_noncontainer_groups_are_not_processed_twice(
    tmp_path: Path,
) -> None:
    """The typed parser is the sole non-container resolution pass."""
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        adapter = CopilotClientAdapter(project_root=tmp_path)
    package = {
        "name": "mcp-server-single-pass",
        "registry_name": "pypi",
        "runtime_hint": "uvx",
        "runtime_arguments": [{"value": "--stdio", "type": "positional"}],
    }

    with patch.object(adapter, "_process_arguments", wraps=adapter._process_arguments) as process:
        config: dict[str, object] = {}
        adapter._select_and_dispatch_best_package(
            config,
            [package],
            env_overrides={},
            runtime_vars={},
        )

    process.assert_not_called()
    assert config["args"] == ["mcp-server-single-pass", "--stdio"]


@pytest.mark.parametrize("target", _TARGETS)
def test_malformed_registry_type_still_fails_closed(
    target: str,
    tmp_path: Path,
) -> None:
    """The #2387 registry type boundary remains ahead of launcher selection."""
    package = {
        "name": "mcp-server-malformed",
        "registry_name": ["pypi"],
        "runtime_hint": "uvx",
        "runtime_arguments": [],
    }

    with pytest.raises(ValueError, match=r"registry_name must be a string"):
        _render(target, package, tmp_path)
