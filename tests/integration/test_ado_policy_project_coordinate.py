"""Hermetic lifecycle coverage for Azure DevOps policy coordinates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.policy.discovery import _read_cache_entry

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]

_VALID_POLICY_YAML = (
    "name: test-policy\n"
    "version: '1.0'\n"
    "enforcement: warn\n"
    "dependencies:\n"
    "  deny: [untrusted/package]\n"
)


def _ado_response() -> MagicMock:
    """Return a successful ADO Items API response containing a valid policy."""
    response = MagicMock()
    response.status_code = 200
    response.text = _VALID_POLICY_YAML
    response.headers = {}
    return response


def _init_ado_project(path: Path) -> None:
    """Create a local git project with an Azure DevOps origin."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://dev.azure.com/contoso/application/_git/service",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_policy_status_uses_valid_ado_policy_coordinate_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status fetches and caches ``apm/apm-policy/apm-policy.yml`` for ADO."""
    _init_ado_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    resolver = MagicMock()

    def try_with_fallback(host: str, operation, **kwargs):
        """Run the policy request without credentials against the fake transport."""
        assert host == "dev.azure.com"
        assert kwargs["org"] == "contoso"
        return operation(None, {})

    resolver.try_with_fallback.side_effect = try_with_fallback
    runner = CliRunner()

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch("apm_cli.core.auth.AuthResolver", return_value=resolver),
        patch("apm_cli.policy.discovery.requests.get", return_value=_ado_response()) as request,
    ):
        result = runner.invoke(
            cli,
            ["policy", "status", "--json", "--policy-source", "org", "--no-cache"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["outcome"] == "found"
    assert report["source"] == "org:dev.azure.com/contoso/apm/apm-policy"
    assert request.call_count == 1

    request_url = urlparse(request.call_args.args[0])
    assert request_url.hostname == "dev.azure.com"
    assert unquote(request_url.path) == "/contoso/apm/_apis/git/repositories/apm-policy/items"
    assert parse_qs(request_url.query) == {
        "path": ["apm-policy.yml"],
        "versionDescriptor.version": ["main"],
        "api-version": ["7.0"],
    }

    cache_ref = "dev.azure.com/contoso/apm/apm-policy"
    cache_entry = _read_cache_entry(cache_ref, tmp_path)
    assert cache_entry is not None
    assert cache_entry.source == f"org:{cache_ref}"


def test_policy_status_uses_legacy_ado_coordinate_only_after_primary_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy policy is cached with one migration warning after a primary 404."""
    _init_ado_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    resolver = MagicMock()
    resolver.try_with_fallback.side_effect = lambda _host, operation, **_kwargs: operation(None, {})
    missing = MagicMock(status_code=404, headers={})

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch("apm_cli.core.auth.AuthResolver", return_value=resolver),
        patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=[missing, _ado_response()],
        ) as request,
    ):
        result = CliRunner().invoke(
            cli,
            ["policy", "status", "--json", "--policy-source", "org", "--no-cache"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["outcome"] == "found", report
    assert report["source"] == "org:dev.azure.com/contoso/_apm/_apm"
    assert report["warnings"] == [
        "Azure DevOps policy was discovered at legacy _apm/_apm. Move it to "
        "apm/apm-policy; the legacy coordinate is a temporary compatibility fallback."
    ]
    assert request.call_count == 2
    assert unquote(urlparse(request.call_args_list[0].args[0]).path) == (
        "/contoso/apm/_apis/git/repositories/apm-policy/items"
    )
    assert unquote(urlparse(request.call_args_list[1].args[0]).path) == (
        "/contoso/_apm/_apis/git/repositories/_apm/items"
    )
    assert _read_cache_entry("dev.azure.com/contoso/_apm/_apm", tmp_path) is not None
