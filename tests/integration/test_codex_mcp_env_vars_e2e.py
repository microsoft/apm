"""E2E guard for Codex project-scope MCP env forwarding through ``env_vars`` (#3393)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import toml
import yaml

pytestmark = pytest.mark.xdist_group(name="home_env")

SET_SECRET = "codex-e2e-set-secret=canary"
ENV_SET_SECRET = "codex-e2e-envset-secret=canary"
MISMATCH_SECRET = "codex-e2e-mismatch-secret=canary"
EDITED_VALUE = "hand-edited-value"

LEGACY_BINARY = os.environ.get("APM_LEGACY_BINARY_PATH")
needs_legacy = pytest.mark.skipif(
    not LEGACY_BINARY, reason="APM_LEGACY_BINARY_PATH (pre-fix apm binary) not set"
)

ENV_BLOCK = {
    "E2E_SET": "${E2E_SET}",
    "E2E_ENV_SET": "${env:E2E_ENV_SET}",
    "E2E_UNSET": "${E2E_UNSET}",
    "E2E_ENV_UNSET": "${env:E2E_ENV_UNSET}",
    "STATIC_VALUE": "static-value",
    "MISMATCH": "${E2E_OTHER}",
}


def _declaration(env: dict[str, str] | None = None, **extra: object) -> dict:
    server = {
        "name": "env-demo",
        "registry": False,
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "example-mcp", "--region", "<E2E_REGION>"],
        "cwd": "./tools",
        "env": ENV_BLOCK if env is None else env,
    }
    server.update(extra)
    return server


def _write_manifest(root: Path, server: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "codex-mcp-env-vars-e2e",
        "version": "0.0.0",
        "dependencies": {"apm": [], "mcp": [server]},
    }
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _environment(tmp_path: Path, **values: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("E2E_", "CODEX_"))
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "GIT_TERMINAL_PROMPT": "0",
            "APM_NON_INTERACTIVE": "1",
            "E2E_OTHER": MISMATCH_SECRET,
            "E2E_REGION": "eu-west-1",
        }
    )
    env.update(values)
    return env


def _install(
    binary: str, cwd: Path, env: dict[str, str], *flags: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, "install", "--only", "mcp", "--runtime", "codex", "--target", "codex", *flags],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _output(result: subprocess.CompletedProcess) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _flat(text: str) -> str:
    """Collapse the console's line wrapping so messages can be matched."""
    return " ".join(text.split())


def _project(tmp_path: Path, server: dict | None = None) -> Path:
    project = tmp_path / "project"
    _write_manifest(project, server or _declaration())
    (project / ".codex").mkdir()
    return project


def _server(project: Path) -> dict:
    return toml.load(project / ".codex" / "config.toml")["mcp_servers"]["env-demo"]


def _secret_values_set(tmp_path: Path) -> dict[str, str]:
    return _environment(tmp_path, E2E_SET=SET_SECRET, E2E_ENV_SET=ENV_SET_SECRET)


def test_references_are_forwarded_for_set_and_unset_variables(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _project(tmp_path)
    (project / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "other-cmd"\nargs = ["--keep"]\n', encoding="utf-8"
    )

    result = _install(apm_binary_path, project, _secret_values_set(tmp_path))

    assert result.returncode == 0, _output(result)
    config_text = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    server = _server(project)
    assert sorted(server["env_vars"]) == [
        "E2E_ENV_SET",
        "E2E_ENV_UNSET",
        "E2E_SET",
        "E2E_UNSET",
    ]
    # Static values and mismatched references keep today's install-time handling.
    assert server["env"] == {"STATIC_VALUE": "static-value", "MISMATCH": MISMATCH_SECRET}
    # An args placeholder is left exactly as declared.
    assert server["args"] == ["-y", "example-mcp", "--region", "<E2E_REGION>"]
    assert server["cwd"] == "./tools"
    assert toml.load(project / ".codex" / "config.toml")["mcp_servers"]["other"] == {
        "command": "other-cmd",
        "args": ["--keep"],
    }
    for secret in (SET_SECRET, ENV_SET_SECRET):
        assert secret not in config_text
        assert secret not in (project / "apm.lock.yaml").read_text(encoding="utf-8")
        assert secret not in _output(result)
    assert MISMATCH_SECRET not in _output(result)
    assert MISMATCH_SECRET not in (project / "apm.lock.yaml").read_text(encoding="utf-8")


def test_unforwardable_references_warn_with_names_only(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _project(tmp_path)
    env = _secret_values_set(tmp_path)
    result = _install(apm_binary_path, project, env)

    assert result.returncode == 0, _output(result)
    warning = _flat(_output(result))
    assert "Codex cannot forward env MISMATCH" in warning
    assert "KEY: ${KEY}" in warning
    assert "E2E_SET" not in warning.split("Codex cannot forward")[1].split(".codex")[0]
    assert MISMATCH_SECRET not in warning
    assert _server(project)["env"]["MISMATCH"] == MISMATCH_SECRET


def test_embedded_reference_is_resolved_and_named_in_warning(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _project(tmp_path, _declaration({"AUTH": "Bearer ${E2E_OTHER}"}))
    result = _install(apm_binary_path, project, _secret_values_set(tmp_path))

    assert result.returncode == 0, _output(result)
    assert _server(project)["env"] == {"AUTH": f"Bearer {MISMATCH_SECRET}"}
    assert "Codex cannot forward env AUTH" in _flat(_output(result))
    assert MISMATCH_SECRET not in _output(result)


def test_user_scope_install_still_resolves_values(tmp_path: Path, apm_binary_path: str) -> None:
    env = _secret_values_set(tmp_path)
    home = tmp_path / "home"
    _write_manifest(home / ".apm", _declaration())
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "codex-home").mkdir()

    result = _install(apm_binary_path, project, env, "--global")

    assert result.returncode == 0, _output(result)
    config = toml.load(tmp_path / "codex-home" / "config.toml")["mcp_servers"]["env-demo"]
    assert "env_vars" not in config
    assert config["env"]["E2E_SET"] == SET_SECRET
    assert config["env"]["E2E_ENV_SET"] == ENV_SET_SECRET
    assert config["env"]["E2E_UNSET"] == "${E2E_UNSET}"
    assert config["env"]["E2E_ENV_UNSET"] == "${env:E2E_ENV_UNSET}"
    assert config["env"]["MISMATCH"] == MISMATCH_SECRET
    assert "Codex cannot forward" not in _flat(_output(result))
    assert SET_SECRET not in _output(result)


# --- Reinstall migration ---------------------------------------------------


def _migration_declaration() -> dict:
    return _declaration({"E2E_SET": "${E2E_SET}", "STATIC_VALUE": "static-value"})


def _hand_write_legacy_install(project: Path, secret: str) -> None:
    """Reproduce the config.toml an older apm wrote, on top of the current lock state."""
    (project / ".codex" / "config.toml").write_text(
        "[mcp_servers.env-demo]\n"
        'command = "npx"\n'
        'args = ["-y", "example-mcp", "--region", "<E2E_REGION>"]\n'
        'cwd = "./tools"\n\n'
        "[mcp_servers.env-demo.env]\n"
        f'E2E_SET = "{secret}"\n'
        'STATIC_VALUE = "static-value"\n',
        encoding="utf-8",
    )


def _legacy_shaped_project(tmp_path: Path, binary: str) -> Path:
    """Install once with the branch binary for lock state, then rewrite the config.

    The older formatter produced the same lock record (the declaration, with the
    reference kept verbatim) and only differed in the native config it wrote.
    """
    project = _project(tmp_path, _migration_declaration())
    env = _secret_values_set(tmp_path)
    assert _install(binary, project, env).returncode == 0
    _hand_write_legacy_install(project, SET_SECRET)
    return project


def test_reinstall_replaces_apm_written_value_with_env_vars(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _legacy_shaped_project(tmp_path, apm_binary_path)

    result = _install(apm_binary_path, project, _secret_values_set(tmp_path))

    assert result.returncode == 0, _output(result)
    server = _server(project)
    assert server["env_vars"] == ["E2E_SET"]
    assert server["env"] == {"STATIC_VALUE": "static-value"}
    assert SET_SECRET not in (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert SET_SECRET not in _output(result)
    message = _flat(_output(result))
    assert "replaced a previously written value for E2E_SET" in message
    assert "rotate the credential" in message


def test_reinstall_preserves_a_value_that_was_edited_by_hand(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _legacy_shaped_project(tmp_path, apm_binary_path)
    _hand_write_legacy_install(project, EDITED_VALUE)
    before = (project / ".codex" / "config.toml").read_bytes()

    result = _install(apm_binary_path, project, _secret_values_set(tmp_path))

    assert result.returncode != 0
    assert (project / ".codex" / "config.toml").read_bytes() == before
    message = _flat(_output(result))
    assert "Preserved that connection" in message
    assert "Reconcile them by hand" in message
    assert "E2E_SET" in message
    assert "rotate the credential" in message
    assert EDITED_VALUE not in message
    assert SET_SECRET not in message


def test_reinstall_preserves_an_unrecorded_entry(tmp_path: Path, apm_binary_path: str) -> None:
    project = _project(tmp_path, _migration_declaration())
    _hand_write_legacy_install(project, SET_SECRET)

    result = _install(apm_binary_path, project, _secret_values_set(tmp_path))

    assert result.returncode != 0
    assert f'E2E_SET = "{SET_SECRET}"' in (project / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    message = _flat(_output(result))
    assert "Reconcile them by hand" in message
    assert SET_SECRET not in message


@needs_legacy
def test_reinstall_after_a_real_legacy_install_migrates_to_env_vars(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _project(tmp_path, _migration_declaration())
    env = _secret_values_set(tmp_path)
    first = _install(LEGACY_BINARY, project, env)
    assert first.returncode == 0, _output(first)
    assert _server(project)["env"]["E2E_SET"] == SET_SECRET

    result = _install(apm_binary_path, project, env)

    assert result.returncode == 0, _output(result)
    assert _server(project)["env_vars"] == ["E2E_SET"]
    assert SET_SECRET not in (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "replaced a previously written value for E2E_SET" in _flat(_output(result))
    assert SET_SECRET not in _output(result)


@needs_legacy
def test_reinstall_after_a_real_legacy_install_keeps_hand_edit(
    tmp_path: Path, apm_binary_path: str
) -> None:
    project = _project(tmp_path, _migration_declaration())
    env = _secret_values_set(tmp_path)
    assert _install(LEGACY_BINARY, project, env).returncode == 0
    config_path = project / ".codex" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(SET_SECRET, EDITED_VALUE),
        encoding="utf-8",
    )

    result = _install(apm_binary_path, project, env)

    assert result.returncode != 0
    assert f'E2E_SET = "{EDITED_VALUE}"' in config_path.read_text(encoding="utf-8")
    message = _flat(_output(result))
    assert "Reconcile them by hand" in message
    assert "rotate the credential" in message
    assert EDITED_VALUE not in message
    assert SET_SECRET not in message
