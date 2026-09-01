"""Fixture-backed metadata fetch coverage through the real auth resolver."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd
from apm_cli.core.auth import AuthResolver
from apm_cli.marketplace.builder import MarketplaceBuilder, ResolvedPackage

pytestmark = pytest.mark.component

_SHA = "0123456789abcdef0123456789abcdef01234567"


class _Response:
    """Minimal URL response for the metadata reader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _package() -> ResolvedPackage:
    return ResolvedPackage(
        name="remote-tool",
        source_repo="acme/remote-tool",
        subdir=None,
        ref=_SHA,
        sha=_SHA,
        requested_version=None,
        tags=(),
        is_prerelease=False,
    )


@pytest.mark.parametrize("token", [None, "ghp_fixture_token"])
def test_metadata_prefetch_uses_real_resolver_token_and_anonymous_paths(
    token: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public fetch stays anonymous; a hidden repository retries with auth."""
    for name in ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if token is not None:
        monkeypatch.setenv("GITHUB_APM_PAT", token)

    authorization: list[str | None] = []

    def _urlopen(request: urllib.request.Request, timeout: int) -> _Response:
        assert timeout == 5
        header = request.get_header("Authorization")
        authorization.append(header)
        if token is not None and header is None:
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                hdrs=None,
                fp=None,
            )
        return _Response(b"name: remote-tool\ndescription: Fixture\nversion: 1.0.0\n")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    builder = MarketplaceBuilder(
        tmp_path / "apm.yml",
        auth_resolver=AuthResolver(),
    )

    outcome = builder._fetch_remote_metadata_outcome(_package())

    assert outcome.status == "fetched"
    expected = [None] if token is None else [None, f"token {token}"]
    assert authorization == expected


def test_offline_strict_metadata_exits_five_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline metadata remains uncertifiable and blocks every artifact write."""
    (tmp_path / "apm.yml").write_text(
        f"""\
name: offline-metadata
description: Offline strict metadata fixture
version: 1.0.0
dependencies: {{}}
marketplace:
  owner:
    name: APM Tests
  packages:
    - name: remote-tool
      source: acme/remote-tool
      ref: {_SHA}
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        pack_cmd,
        ["--offline", "--strict-metadata", "--json"],
    )

    assert result.exit_code == 5, result.output
    payload = json.loads(result.output)
    assert payload["metadata_enrichment"]["outcomes"] == [
        {
            "package": "remote-tool",
            "status": "offline",
            "cause": "metadata fetch skipped by --offline",
        }
    ]
    assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()
    assert not (tmp_path / "build").exists()
