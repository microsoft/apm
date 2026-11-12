"""Process-boundary coverage for SSH marketplace fetches."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.marketplace.client import _fetch_git
from apm_cli.marketplace.models import MarketplaceSource

pytestmark = pytest.mark.component


def test_ssh_marketplace_process_preserves_port_without_http_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client-to-process path preserves SSH identity and strips HTTP auth."""
    secrets = {
        "GITHUB_APM_PAT": "platform-secret",
        "GIT_TOKEN": "git-secret",
        "GIT_HTTP_EXTRAHEADER": "Authorization: secret",
        "GIT_ASKPASS": "unsafe-askpass",
        "APM_REGISTRY_TOKEN_CORP": "registry-token",
        "APM_REGISTRY_USER_CORP": "registry-user",
        "APM_REGISTRY_PASS_CORP": "registry-password",
        "PROXY_REGISTRY_TOKEN": "proxy-token",
        "ARTIFACTORY_APM_TOKEN": "legacy-proxy-token",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    source_url = "ssh://git@gitea.example.com:2222/org/repo.git"
    source = MarketplaceSource(name="private", url=source_url)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marketplace.json").write_text(
        json.dumps({"name": "private", "plugins": []}),
        encoding="utf-8",
    )
    record_path = tmp_path / "process-record.json"

    class RecordingGitCache:
        def __init__(self, _root: Path, *, refresh: bool) -> None:
            assert refresh is False

        def get_checkout(
            self,
            url: str,
            ref: str,
            *,
            env: dict[str, str],
            sparse_paths: list[str] | None,
        ) -> Path:
            probe = (
                "import json, os, pathlib, sys;"
                "names=sys.argv[3:];"
                "data={'url':sys.argv[2],"
                "'env':{name:os.environ.get(name) for name in names}};"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps(data),encoding='utf-8')"
            )
            subprocess.run(
                [sys.executable, "-c", probe, str(record_path), url, *secrets],
                check=True,
                env=env,
            )
            assert ref == "main"
            assert sparse_paths is None
            return checkout

    resolver = AuthResolver(token_manager=MagicMock())
    with (
        patch.object(
            resolver,
            "hardened_git_base_env",
            return_value=dict(os.environ),
        ),
        patch("apm_cli.cache.git_cache.GitCache", RecordingGitCache),
        patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path / "cache"),
    ):
        result = _fetch_git(
            source,
            "marketplace.json",
            host_info=AuthResolver.classify_host(source.host, port=source.port),
            auth_resolver=resolver,
        )

    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    parsed = urlparse(recorded["url"])
    assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (
        "ssh",
        "gitea.example.com",
        2222,
        "/org/repo.git",
    )
    assert all(recorded["env"][name] in (None, "") for name in secrets)
    assert result == {"name": "private", "plugins": []}
