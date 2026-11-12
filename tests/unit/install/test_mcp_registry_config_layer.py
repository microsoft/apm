"""`apm install` must query the registry the precedence chain names (#2740).

``apm config set mcp-registry-url <url>`` was honoured by the ``apm mcp``
commands and by ``apm install --mcp NAME``, but the manifest-driven
``dependencies.mcp`` path built its registry client straight off
``MCP_REGISTRY_URL``. A server that ``apm mcp show`` found on a private
registry was therefore looked up on the public default during ``apm install``
and reported as missing.

These tests drive the real CLI over a recorded HTTP layer and assert on the
hosts actually contacted. Direct helper tests cover the recovery guidance.
Together they fail if any future refactor reintroduces a second resolution
path or silently changes the retry boundary.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner, Result

from apm_cli.cli import cli
from apm_cli.integration.mcp_integrator_install import (
    CHOOSING_MCP_REGISTRY_DOCS_URL,
    _validate_registry_servers,
)
from apm_cli.registry.client import SimpleRegistryClient, is_valid_mcp_server_name
from apm_cli.registry.operations import MCPServerOperations

CONFIG_REGISTRY = "https://registry.internal.example"
ENV_REGISTRY = "https://env-registry.internal.example"
# A second populated registry the user picks by hand when replaying the hint.
CHOSEN_REGISTRY = "https://chosen-registry.example"
DEFAULT_HOST = "api.mcp.github.com"
DOCS_HOST = urlparse(CHOOSING_MCP_REGISTRY_DOCS_URL).hostname

SERVER = {
    "name": "ado-mcp",
    "description": "internal Azure DevOps server",
    "version": "1.0.0",
    "packages": [{"registryType": "npm", "identifier": "@internal/ado-mcp", "version": "1.0.0"}],
}

MANIFEST = (
    "name: registry-layer-probe\n"
    "version: 0.1.0\n"
    "targets: copilot\n"
    "dependencies:\n"
    "  mcp:\n"
    "    - name: ado-mcp\n"
    "      registry: true\n"
)


class _RecordedResponse:
    """Minimal stand-in honouring the capped-stream read contract."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self) -> None:
        return None


@pytest.fixture
def recorded_registry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve ``ado-mcp`` from the config and chosen registries; record hosts."""
    hosts: list[str] = []
    populated_hosts = {
        urlparse(CONFIG_REGISTRY).hostname,
        urlparse(CHOSEN_REGISTRY).hostname,
    }

    def _get(_self: requests.Session, url: str, **_kwargs: object) -> _RecordedResponse:
        hostname = urlparse(url).hostname or ""
        hosts.append(hostname)
        if hostname not in populated_hosts:
            return _RecordedResponse({"servers": []})
        if "/versions/" in url:
            return _RecordedResponse({"server": SERVER})
        return _RecordedResponse({"servers": [{"server": SERVER}]})

    monkeypatch.setattr(requests.Session, "get", _get)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    return hosts


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A temp project declaring one registry-backed MCP dependency."""
    from apm_cli import config as config_mod

    (tmp_path / "apm.yml").write_text(MANIFEST, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config_dir = tmp_path / ".apm"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_dir / "config.json"))
    config_mod._invalidate_config_cache()
    return tmp_path


def _install(args: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(cli, ["install", "--no-policy", *args])


def _printed_hosts(output: str) -> set[str]:
    """Hostnames of every URL in CLI output, parsed rather than substring-matched."""
    return {
        urlparse(token.strip("(),.;'\"")).hostname for token in output.split() if "://" in token
    }


def _extract_choice_argv(output: str, registry_url: str) -> list[str]:
    """Extract the printed example command with ``<URL>`` replaced by the user."""
    match = re.search(
        r"Use another registry \(replace <URL>\): "
        r'(?P<command>apm install --mcp "[^"]+" --registry <URL>[^\r\n]*)',
        output,
    )
    assert match is not None, output
    return shlex.split(match.group("command").replace("<URL>", registry_url))


def _flat(output: str) -> str:
    """Collapse Rich line wrapping for assertions on wrapped error lines."""
    return " ".join(output.split())


def _write_empty_manifest(project: Path) -> None:
    (project / "apm.yml").write_text(
        "name: registry-layer-probe\nversion: 0.1.0\ntargets: copilot\ndependencies:\n  mcp: []\n",
        encoding="utf-8",
    )


def test_manifest_install_uses_configured_registry(monkeypatch, recorded_registry, project):
    """The persisted config layer reaches the manifest-driven install path."""
    from apm_cli.config import set_mcp_registry_url

    set_mcp_registry_url(CONFIG_REGISTRY)

    result = _install()

    assert result.exit_code == 0, result.output
    assert set(recorded_registry) == {urlparse(CONFIG_REGISTRY).hostname}


def test_manifest_install_announces_the_configured_registry(
    monkeypatch, recorded_registry, project
):
    """A redirect away from the public registry is stated, never inferred."""
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: CONFIG_REGISTRY
    )

    result = _install()

    assert "Using MCP registry" in result.output
    assert "from apm config" in result.output


def test_env_layer_still_outranks_the_config_layer(monkeypatch, recorded_registry, project):
    """MCP_REGISTRY_URL keeps precedence, so the lookup misses and fails loudly."""
    monkeypatch.setenv("MCP_REGISTRY_URL", ENV_REGISTRY)
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: CONFIG_REGISTRY
    )

    result = _install()

    assert result.exit_code != 0
    assert set(recorded_registry) == {urlparse(ENV_REGISTRY).hostname}


def test_missing_server_error_names_the_registry_queried(monkeypatch, recorded_registry, project):
    """The failure names the endpoint, so a wrong registry is diagnosable."""
    monkeypatch.setenv("MCP_REGISTRY_URL", ENV_REGISTRY)

    result = _install()

    assert result.exit_code != 0
    assert "not found in registry" in result.output
    assert _printed_hosts(result.output) == {urlparse(ENV_REGISTRY).hostname}


def test_manifest_default_registry_miss_keeps_generic_guidance(
    monkeypatch, recorded_registry, project
):
    """A manifest miss keeps generic guidance and never names another registry."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)

    result = _install()

    assert result.exit_code != 0
    assert "Using MCP registry" not in result.output
    assert set(recorded_registry) == {DEFAULT_HOST}
    assert _printed_hosts(result.output) == {DEFAULT_HOST}
    assert "apm mcp search" in result.output
    assert "<URL>" not in result.output


@pytest.mark.windows_compat
def test_direct_default_miss_names_registry_and_stays_neutral(
    monkeypatch: pytest.MonkeyPatch,
    recorded_registry: list[str],
    project: Path,
) -> None:
    """The hint names the searched registry and endorses no alternative."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)
    _write_empty_manifest(project)

    result = _install(("--mcp", "ado-mcp"))

    assert result.exit_code != 0
    assert set(recorded_registry) == {DEFAULT_HOST}
    assert _printed_hosts(result.output) == {DEFAULT_HOST, DOCS_HOST}
    assert f"not found in registry https://{DEFAULT_HOST}: ado-mcp" in _flat(result.output)
    assert 'Search this registry: apm mcp search "ado-mcp"' in result.output
    assert _extract_choice_argv(result.output, "<URL>") == [
        "apm",
        "install",
        "--mcp",
        "ado-mcp",
        "--registry",
        "<URL>",
    ]
    assert "apm config set mcp-registry-url <URL>" in result.output
    assert CHOOSING_MCP_REGISTRY_DOCS_URL in result.output


EMPTY_REGISTRY = "https://empty-registry.internal.example"


@pytest.mark.parametrize("source", ["flag", "env", "config"])
def test_direct_custom_registry_miss_names_that_registry(
    monkeypatch: pytest.MonkeyPatch,
    recorded_registry: list[str],
    project: Path,
    source: str,
) -> None:
    """A custom registry is named and searched; no other registry is contacted."""
    empty_host = urlparse(EMPTY_REGISTRY).hostname
    config_value = EMPTY_REGISTRY if source == "config" else None
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: config_value
    )
    if source == "env":
        monkeypatch.setenv("MCP_REGISTRY_URL", EMPTY_REGISTRY)
    _write_empty_manifest(project)
    args = ["--mcp", "ado-mcp"]
    if source == "flag":
        args.extend(("--registry", EMPTY_REGISTRY))

    result = _install(args)

    assert result.exit_code != 0
    assert set(recorded_registry) == {empty_host}
    assert _printed_hosts(result.output) == {empty_host, DOCS_HOST}
    assert f"not found in registry {EMPTY_REGISTRY}: ado-mcp" in _flat(result.output)
    if source == "flag":
        assert (
            'Search this registry: apm mcp search "ado-mcp" '
            f"(with MCP_REGISTRY_URL set to {EMPTY_REGISTRY})" in result.output
        )
    else:
        assert 'Search this registry: apm mcp search "ado-mcp"' in result.output
    if source == "env":
        assert "Or point MCP_REGISTRY_URL at <URL>" in result.output
    else:
        assert "apm config set mcp-registry-url <URL>" in result.output
    assert "--registry <URL>" in result.output


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("version", "target", "global_"),
    [
        ("1.2.0", None, False),
        (None, "claude", False),
        ("1.2.0", "claude", False),
        ("1.2.0+build.7", "claude,codex", True),
    ],
)
def test_registry_choice_example_preserves_install_selectors(
    monkeypatch: pytest.MonkeyPatch,
    recorded_registry: list[str],
    project: Path,
    version: str | None,
    target: str | None,
    global_: bool,
) -> None:
    """Replaying the example with a chosen URL retains version, targets, and scope."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.mcp.command import run_mcp_install
    from apm_cli.utils.yaml_io import load_yaml

    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)
    _write_empty_manifest(project)
    install_spy = MagicMock(wraps=run_mcp_install)
    monkeypatch.setattr("apm_cli.commands.install._run_mcp_install", install_spy)
    options = []
    if version:
        options.extend(("--mcp-version", version))
    if target:
        options.extend(("--target", target))
    if global_:
        options.append("--global")
    original_manifest = (project / "apm.yml").read_bytes()

    result = _install(("--mcp", "ado-mcp", *options))

    assert result.exit_code != 0
    assert set(recorded_registry) == {DEFAULT_HOST}
    retry_argv = _extract_choice_argv(result.output, CHOSEN_REGISTRY)
    assert retry_argv == [
        "apm",
        "install",
        "--mcp",
        "ado-mcp",
        "--registry",
        CHOSEN_REGISTRY,
        *options,
    ]
    assert (project / "apm.yml").read_bytes() == original_manifest
    assert not (project / ".apm" / "apm.yml").exists()

    recorded_registry.clear()
    install_spy.reset_mock()
    retry_result = CliRunner().invoke(cli, retry_argv[1:])

    assert retry_result.exit_code == 0, retry_result.output
    assert set(recorded_registry) == {urlparse(CHOSEN_REGISTRY).hostname}
    install_spy.assert_called_once()
    kwargs = install_spy.call_args.kwargs
    assert kwargs["mcp_version"] == version
    assert kwargs["target"] == (
        target.split(",") if target and "," in target else target or ["copilot"]
    )
    assert kwargs["scope"] is (InstallScope.USER if global_ else InstallScope.PROJECT)
    manifest_path = project / ".apm" / "apm.yml" if global_ else project / "apm.yml"
    entry = load_yaml(manifest_path)["dependencies"]["mcp"][0]
    if version:
        assert entry["version"] == version
    else:
        assert "version" not in entry
    assert entry["registry"] == CHOSEN_REGISTRY
    if global_:
        assert (project / "apm.yml").read_bytes() == original_manifest


@pytest.mark.parametrize(
    "version",
    ["1.2.0;echo unsafe", "1.2.0$HOME", "%PATH%", '1.2.0"bad', "1.2.0\n", "1.2.0`whoami`"],
)
def test_registry_choice_hint_omits_unsafe_version_values(
    monkeypatch: pytest.MonkeyPatch,
    recorded_registry: list[str],
    project: Path,
    version: str,
) -> None:
    """An opaque version cannot inject shell syntax into a printed command."""
    monkeypatch.setattr("apm_cli.config.get_mcp_registry_url", lambda *, create_config=True: None)

    result = _install(("--mcp", "ado-mcp", "--mcp-version", version, "--target", "claude"))

    assert result.exit_code != 0
    assert set(recorded_registry) == {DEFAULT_HOST}
    assert "<URL>" not in result.output
    assert "Run 'apm mcp search <query>'" in result.output
    assert _printed_hosts(result.output) == {DEFAULT_HOST}


def _missing_operations(
    server_names: list[str], source: str | None, registry_url: str
) -> MagicMock:
    operations = MagicMock()
    operations.validate_servers_exist.return_value = ([], server_names)
    operations.registry_client = SimpleNamespace(
        registry_url=registry_url,
        registry_url_source=source,
    )
    return operations


def _validate_missing(operations: MagicMock, server_names: list[str], **kwargs) -> MagicMock:
    logger = MagicMock()
    with pytest.raises(RuntimeError, match=f"Cannot install {len(server_names)} missing server"):
        _validate_registry_servers(
            operations,
            server_names,
            dependency_count=len(server_names),
            verbose=False,
            logger=logger,
            **kwargs,
        )
    return logger


def test_default_registry_miss_hint_is_ordered_and_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each missing name gets a search and a placeholder example, in order."""
    server_names = ["io.example.weather/alerts~v2", "io.example.weather/forecast"]
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.setattr(
        "apm_cli.config.get_mcp_registry_url",
        lambda *, create_config=False: None,
    )
    operations = MCPServerOperations()
    monkeypatch.setattr(
        operations.registry_client,
        "find_server_by_reference",
        MagicMock(return_value=None),
    )

    logger = _validate_missing(
        operations,
        server_names,
        suggest_registry_choice=True,
        retry_args=("--target", "claude"),
    )

    expected = []
    for name in server_names:
        expected.append(f'Search this registry: apm mcp search "{name}"')
        expected.append(
            "Use another registry (replace <URL>): "
            f'apm install --mcp "{name}" --registry <URL> --target claude'
        )
    expected.append("Or save it for every lookup: apm config set mcp-registry-url <URL>")
    expected.append(f"Choosing an MCP registry: {CHOOSING_MCP_REGISTRY_DOCS_URL}")
    assert [call.args[0] for call in logger.info.call_args_list] == expected
    assert all(call.kwargs == {"soft_wrap": True} for call in logger.info.call_args_list)
    assert all(is_valid_mcp_server_name(server_name) for server_name in server_names)


@pytest.mark.parametrize(
    "server_name",
    ["bad;name", 'bad"name', "bad\nname", "valid-looking\n"],
)
def test_registry_choice_hint_rejects_unsafe_server_names(server_name: str) -> None:
    """Unvalidated names never cross into a pasted shell command."""
    assert not is_valid_mcp_server_name(server_name)
    with pytest.raises(ValueError, match="Invalid server name"):
        SimpleRegistryClient("https://api.mcp.github.com").get_server(server_name)
    operations = _missing_operations([server_name], "default", "https://api.mcp.github.com")

    logger = _validate_missing(operations, [server_name], suggest_registry_choice=True)

    logger.info.assert_not_called()
    logger.progress.assert_called_once_with(
        "Run 'apm mcp search <query>' to find available servers"
    )


def test_flag_registry_hint_never_echoes_credentials() -> None:
    """The searched-registry line uses the redacted URL only."""
    registry_url = "https://user:super-secret@private.example/api"
    operations = _missing_operations(["io.example.weather/alerts"], "flag", registry_url)

    logger = _validate_missing(
        operations, ["io.example.weather/alerts"], suggest_registry_choice=True
    )

    output = " ".join(
        call.args[0]
        for method in (logger.error, logger.progress, logger.info)
        for call in method.call_args_list
    )
    assert _printed_hosts(output) == {"private.example", DOCS_HOST}
    assert "super-secret" not in output
    assert "(with MCP_REGISTRY_URL set to https://private.example/api)" in output


@pytest.mark.parametrize("source", ["explicit", None])
def test_manifest_registry_miss_keeps_existing_guidance(source: str | None) -> None:
    """Sources a direct install cannot produce never get the choice hint."""
    registry_url = "https://user:super-secret@private.example/api"
    operations = _missing_operations(["io.example.weather/alerts"], source, registry_url)

    logger = _validate_missing(
        operations, ["io.example.weather/alerts"], suggest_registry_choice=True
    )

    output = " ".join(
        call.args[0]
        for method in (logger.error, logger.progress, logger.info)
        for call in method.call_args_list
    )
    assert _printed_hosts(output) == {"private.example"}
    assert "<URL>" not in output
    assert "super-secret" not in output
    logger.info.assert_not_called()
