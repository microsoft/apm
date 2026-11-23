from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.transport_selection import (
    NoOpInsteadOfResolver,
    ProtocolPreference,
    TransportSelector,
)
from apm_cli.models.apm_package import DependencyReference
from tests.utils.simulated_ado_chain import install_simulated_ado_chain

pytestmark = pytest.mark.component


def make_host(
    *,
    github_token: str | None = None,
    ado_token: str | None = None,
    artifactory_token: str | None = None,
    registry_config: object | None = None,
    github_host: str = "github.com",
    resolved_token: str | None = None,
    source: str = "",
    api_base: str = "https://api.github.com",
) -> MagicMock:
    host = MagicMock()
    host.github_token = github_token
    host.ado_token = ado_token
    host.artifactory_token = artifactory_token
    host.registry_config = registry_config
    host.github_host = github_host
    host._resilient_get = MagicMock()

    auth_resolver = MagicMock()
    effective_token = resolved_token if resolved_token is not None else ado_token
    ctx = SimpleNamespace(token=effective_token, source=source, auth_scheme="basic")
    auth_resolver.resolve.return_value = ctx
    auth_resolver.resolve_for_dep.return_value = ctx
    auth_resolver.resolve_for_remote.return_value = ctx
    auth_resolver.git_env_for_remote.return_value = {}
    auth_resolver.build_native_git_credential_env.return_value = {}
    auth_resolver.classify_host.return_value = SimpleNamespace(kind="generic", api_base=api_base)
    auth_resolver.build_error_context.return_value = (
        "Check PAT permissions." if effective_token else "Set a token."
    )
    install_simulated_ado_chain(auth_resolver)
    host.auth_resolver = auth_resolver
    host._resolve_dep_auth_ctx = MagicMock(return_value=ctx)
    host._protocol_pref = ProtocolPreference.NONE
    host._allow_fallback = False
    host._transport_selector = TransportSelector(NoOpInsteadOfResolver())
    return host


def make_dep(
    repo_url: str = "owner/repo",
    host: str | None = "github.com",
    *,
    ado_organization: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    is_insecure: bool = False,
    port: int | None = None,
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        is_insecure=is_insecure,
        port=port,
    )


def fake_response(
    status_code: int = 200,
    *,
    content: bytes = b"data",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = headers or {}
    response.text = content.decode("utf-8", errors="replace")
    response.iter_content.return_value = iter([content] if content else [])
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    return response


def make_zip(entries: dict[str, bytes], *, root_prefix: str = "repo-main/") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.mkdir(root_prefix)
        for name, payload in entries.items():
            full_name = root_prefix + name
            if name.endswith("/"):
                zf.mkdir(full_name)
            else:
                zf.writestr(full_name, payload)
    return buffer.getvalue()


def make_zip_with_file_modes(
    entries: dict[str, tuple[bytes, int]],
    *,
    root_prefix: str = "repo-main/",
) -> bytes:
    buffer = io.BytesIO()
    root_info = zipfile.ZipInfo(root_prefix)
    root_info.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(root_info, b"")
        for name, (payload, mode) in entries.items():
            file_info = zipfile.ZipInfo(root_prefix + name)
            file_info.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(file_info, payload)
    return buffer.getvalue()


class TestDownloadArtifactoryArchive:
    def test_executable_bits_survive_archive_extraction(self, tmp_path: Path) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        archive_bytes = make_zip_with_file_modes(
            {
                "bin/run-driver": (b"#!/bin/sh\n", 0o755),
                "README.md": (b"hello", 0o644),
            }
        )
        host._resilient_get.return_value = fake_response(200, content=archive_bytes)

        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/archive.zip"],
        ):
            delegate.download_artifactory_archive(
                "art.example.com",
                "proxy",
                "owner",
                "repo",
                "main",
                tmp_path,
            )

        script = tmp_path / "bin" / "run-driver"
        readme = tmp_path / "README.md"
        assert script.read_bytes() == b"#!/bin/sh\n"
        assert os.stat(script).st_mode & 0o111 == 0o111
        assert os.stat(readme).st_mode & 0o111 == 0
