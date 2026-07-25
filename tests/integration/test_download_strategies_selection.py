from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.github_rate_limit import GitHubThrottleError
from apm_cli.models.apm_package import DependencyReference


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
    ctx = SimpleNamespace(token=resolved_token, source=source)
    auth_resolver.resolve.return_value = ctx
    auth_resolver.resolve_for_dep.return_value = ctx
    auth_resolver.classify_host.return_value = SimpleNamespace(kind="generic", api_base=api_base)
    auth_resolver.build_error_context.return_value = "Set a token."
    host.auth_resolver = auth_resolver
    host._resolve_dep_auth_ctx = MagicMock(return_value=ctx)
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


class TestResilientGet:
    def test_invalid_reset_header_falls_back_to_backoff(self) -> None:
        delegate = DownloadDelegate(make_host())
        rate_limited = fake_response(429, headers={"X-RateLimit-Reset": "bad"})
        success = fake_response(200, content=b"ok")

        with (
            patch("requests.get", side_effect=[rate_limited, success]),
            patch("time.sleep") as mock_sleep,
        ):
            result = delegate.resilient_get("https://example.com", {}, max_retries=2)

        assert result is success
        assert mock_sleep.called

    def test_all_failures_raise_request_exception_without_last_exc(self) -> None:
        delegate = DownloadDelegate(make_host())

        with patch("requests.get", side_effect=[Exception("boom")]):
            with pytest.raises(Exception, match="boom"):
                delegate.resilient_get("https://example.com", {}, max_retries=1)


class TestBuildRepoUrl:
    def test_gitlab_backend_uses_resolve_for_dep_token(self) -> None:
        host = make_host(resolved_token="gitlab-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        backend = MagicMock()
        backend.kind = "gitlab"
        backend.is_github_family = False
        backend.build_clone_https_url.return_value = "https://gitlab.example.com/group/repo.git"

        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend):
            url = delegate.build_repo_url("group/repo", dep_ref=dep)

        assert url == "https://gitlab.example.com/group/repo.git"
        assert backend.build_clone_https_url.call_args.kwargs["token"] == "gitlab-token"

    def test_ado_without_organization_rebuilds_backend(self) -> None:
        host = make_host(ado_token="ado-token")
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_project="project",
            ado_repo="repo",
        )
        ado_backend = MagicMock()
        ado_backend.kind = "ado"
        ado_backend.is_github_family = False
        generic_backend = MagicMock()
        generic_backend.kind = "github"
        generic_backend.is_github_family = True
        generic_backend.build_clone_https_url.return_value = (
            "https://dev.azure.com/org/project/repo.git"
        )

        with patch(
            "apm_cli.deps.download_strategies.backend_for",
            side_effect=[ado_backend, generic_backend],
        ) as mock_backend_for:
            url = delegate.build_repo_url("org/project/repo", dep_ref=dep)

        assert url == "https://dev.azure.com/org/project/repo.git"
        assert mock_backend_for.call_count == 2


class TestDownloadArtifactoryArchive:
    def test_empty_archive_raises_runtime_error(self, tmp_path: Path) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass
        host._resilient_get.return_value = fake_response(200, content=archive.getvalue())

        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/archive.zip"],
        ):
            with pytest.raises(RuntimeError, match="Empty archive"):
                delegate.download_artifactory_archive(
                    "art.example.com",
                    "proxy",
                    "owner",
                    "repo",
                    "main",
                    tmp_path,
                )

    def test_directory_entries_and_traversal_are_rejected(self, tmp_path: Path) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        archive_bytes = make_zip(
            {
                "nested/": b"",
                "nested/file.txt": b"safe",
                "../escape.txt": b"escape",
            }
        )
        host._resilient_get.return_value = fake_response(200, content=archive_bytes)

        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/archive.zip"],
        ):
            with pytest.raises(RuntimeError, match=r"Unsafe zip archive.*path-traversal"):
                delegate.download_artifactory_archive(
                    "art.example.com",
                    "proxy",
                    "owner",
                    "repo",
                    "main",
                    tmp_path,
                )

        assert not (tmp_path.parent / "escape.txt").exists()

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

    def test_request_exception_becomes_last_error(self, tmp_path: Path) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        host._resilient_get.side_effect = requests.RequestException("network down")

        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/archive.zip"],
        ):
            with pytest.raises(RuntimeError, match="network down"):
                delegate.download_artifactory_archive(
                    "art.example.com",
                    "proxy",
                    "owner",
                    "repo",
                    "main",
                    tmp_path,
                )

    def test_bad_zip_becomes_last_error(self, tmp_path: Path) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        host._resilient_get.return_value = fake_response(200, content=b"not-a-zip")

        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/archive.zip"],
        ):
            with pytest.raises(RuntimeError, match="Invalid zip archive"):
                delegate.download_artifactory_archive(
                    "art.example.com",
                    "proxy",
                    "owner",
                    "repo",
                    "main",
                    tmp_path,
                )


class TestDownloadFileFromArtifactory:
    def test_archive_fallback_skips_non_200_and_reads_plain_name(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        first = fake_response(500, content=b"bad")
        archive = make_zip({"README.md": b"hello"})
        second = fake_response(200, content=archive)
        host._resilient_get.side_effect = [first, second]

        with (
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=["u1", "u2"],
            ),
        ):
            result = delegate.download_file_from_artifactory(
                "art.example.com",
                "proxy",
                "owner",
                "repo",
                "README.md",
                "main",
            )

        assert result == b"hello"

    def test_archive_fallback_ignores_bad_zip_then_succeeds(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        host._resilient_get.side_effect = [
            fake_response(200, content=b"broken"),
            fake_response(200, content=make_zip({"docs/guide.md": b"guide"})),
        ]

        with (
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=["u1", "u2"],
            ),
        ):
            result = delegate.download_file_from_artifactory(
                "art.example.com",
                "proxy",
                "owner",
                "repo",
                "docs/guide.md",
                "main",
            )

        assert result == b"guide"

    def test_archive_fallback_ignores_request_exception_then_succeeds(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        host._resilient_get.side_effect = [
            requests.RequestException("boom"),
            fake_response(200, content=make_zip({"README.md": b"ok"})),
        ]

        with (
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=["u1", "u2"],
            ),
        ):
            result = delegate.download_file_from_artifactory(
                "art.example.com",
                "proxy",
                "owner",
                "repo",
                "README.md",
                "main",
            )

        assert result == b"ok"

    def test_archive_fallback_raises_after_all_urls_fail(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        host._resilient_get.return_value = fake_response(404, content=b"nope")

        with (
            patch(
                "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
                return_value=None,
            ),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=["u1"],
            ),
        ):
            with pytest.raises(RuntimeError, match=r"Failed to download file 'README\.md'"):
                delegate.download_file_from_artifactory(
                    "art.example.com",
                    "proxy",
                    "owner",
                    "repo",
                    "README.md",
                    "main",
                )


class TestDownloadAdoFile:
    def test_non_default_ref_404_raises_specific_error(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        response = fake_response(404)
        host._resilient_get.return_value = response

        with patch("apm_cli.deps.download_strategies.build_ado_api_url", return_value="u"):
            with pytest.raises(RuntimeError, match="at ref 'feature'"):
                delegate.download_ado_file(dep, "README.md", ref="feature")

    def test_default_ref_fallback_404_reports_both_refs(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        host._resilient_get.side_effect = [fake_response(404), fake_response(404)]

        with patch(
            "apm_cli.deps.download_strategies.build_ado_api_url",
            side_effect=["u-main", "u-master"],
        ):
            with pytest.raises(RuntimeError, match="tried refs: main, master"):
                delegate.download_ado_file(dep, "README.md", ref="main")

    def test_auth_failure_without_token_uses_error_context(self) -> None:
        host = make_host(ado_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        host._resilient_get.return_value = fake_response(401)

        with patch("apm_cli.deps.download_strategies.build_ado_api_url", return_value="u"):
            with pytest.raises(RuntimeError, match="Set a token"):
                delegate.download_ado_file(dep, "README.md")

    def test_auth_failure_with_token_mentions_pat_permissions(self) -> None:
        host = make_host(ado_token="ado-token")
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        host._resilient_get.return_value = fake_response(403)

        with patch("apm_cli.deps.download_strategies.build_ado_api_url", return_value="u"):
            with pytest.raises(RuntimeError, match="PAT permissions"):
                delegate.download_ado_file(dep, "README.md")

    def test_other_http_error_becomes_runtime_error(self) -> None:
        host = make_host(ado_token="ado-token")
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        host._resilient_get.return_value = fake_response(500)

        with patch("apm_cli.deps.download_strategies.build_ado_api_url", return_value="u"):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                delegate.download_ado_file(dep, "README.md")

    def test_request_exception_becomes_network_error(self) -> None:
        host = make_host()
        delegate = DownloadDelegate(host)
        dep = make_dep(
            "org/project/repo",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        host._resilient_get.side_effect = requests.RequestException("network")

        with patch("apm_cli.deps.download_strategies.build_ado_api_url", return_value="u"):
            with pytest.raises(RuntimeError, match=r"Network error downloading README\.md"):
                delegate.download_ado_file(dep, "README.md")


class TestDownloadGitlabFile:
    def test_missing_repo_path_raises(self) -> None:
        delegate = DownloadDelegate(make_host())
        dep = make_dep(repo_url="", host="gitlab.example.com")

        with pytest.raises(RuntimeError, match="Missing repository path"):
            delegate.download_gitlab_file(dep, "README.md")

    def test_success_calls_verbose_callback(self) -> None:
        host = make_host(
            resolved_token="gitlab-token", api_base="https://gitlab.example.com/api/v4"
        )
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.return_value = fake_response(200, content=b"ok")
        callback = MagicMock()

        with patch(
            "apm_cli.deps.download_strategies.GitSparseFileTransport",
            return_value=MagicMock(
                fetch_file=MagicMock(side_effect=RuntimeError("git transport unavailable"))
            ),
        ):
            result = delegate.download_gitlab_file(dep, "README.md", verbose_callback=callback)

        assert result == b"ok"
        # The mocked git failure drives the REST fallback path without spawning
        # subprocess/network work. The success note attributes the GitLab REST
        # API as the transport that answered (410 triage).
        callback.assert_any_call("Downloaded file: gitlab.example.com/group/repo/README.md")

    def test_non_default_ref_404_raises_specific_error(self) -> None:
        host = make_host(resolved_token=None, api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.return_value = fake_response(404)

        with pytest.raises(RuntimeError, match="at ref 'feature'"):
            delegate.download_gitlab_file(dep, "README.md", ref="feature")

    def test_fallback_ref_success_calls_verbose_callback(self) -> None:
        host = make_host(resolved_token=None, api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(200, content=b"ok")]
        callback = MagicMock()

        result = delegate.download_gitlab_file(
            dep, "README.md", ref="main", verbose_callback=callback
        )

        assert result == b"ok"
        callback.assert_any_call("Downloaded file: gitlab.example.com/group/repo/README.md")

    def test_fallback_ref_404_reports_both_refs(self) -> None:
        host = make_host(resolved_token=None, api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(404)]

        with pytest.raises(RuntimeError, match="tried refs: main, master"):
            delegate.download_gitlab_file(dep, "README.md", ref="main")

    def test_auth_failure_without_token_uses_error_context(self) -> None:
        host = make_host(resolved_token=None, api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.return_value = fake_response(401)

        with pytest.raises(RuntimeError, match="Set a token"):
            delegate.download_gitlab_file(dep, "README.md")

    def test_auth_failure_with_token_mentions_required_scope(self) -> None:
        host = make_host(
            resolved_token="gitlab-token",
            source="git-credential-fill",
            api_base="https://gitlab.example.com/api/v4",
        )
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.return_value = fake_response(403)

        with pytest.raises(RuntimeError, match="required API scope"):
            delegate.download_gitlab_file(dep, "README.md")

    def test_non_auth_http_error_raises_runtime_error(self) -> None:
        host = make_host(
            resolved_token="gitlab-token", api_base="https://gitlab.example.com/api/v4"
        )
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.return_value = fake_response(500)

        with pytest.raises(RuntimeError, match="HTTP 500"):
            delegate.download_gitlab_file(dep, "README.md")

    def test_http_error_without_response_is_reraised(self) -> None:
        host = make_host(api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        bare_error = requests.exceptions.HTTPError("boom")
        response = MagicMock()
        response.raise_for_status.side_effect = bare_error
        host._resilient_get.return_value = response

        with pytest.raises(requests.exceptions.HTTPError, match="boom"):
            delegate.download_gitlab_file(dep, "README.md")

    def test_request_exception_becomes_network_error(self) -> None:
        host = make_host(api_base="https://gitlab.example.com/api/v4")
        delegate = DownloadDelegate(host)
        dep = make_dep("group/repo", host="gitlab.example.com")
        host._resilient_get.side_effect = requests.RequestException("down")

        with pytest.raises(RuntimeError, match=r"Network error downloading README\.md"):
            delegate.download_gitlab_file(dep, "README.md")


class TestDownloadGithubFile:
    def test_github_raw_success_uses_verbose_callback(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        callback = MagicMock()

        with patch.object(delegate, "try_raw_download", return_value=b"raw") as mock_raw:
            result = delegate.download_github_file(dep, "README.md", verbose_callback=callback)

        assert result == b"raw"
        callback.assert_called_once_with("Downloaded file: github.com/owner/repo/README.md")
        mock_raw.assert_called_once_with("owner", "repo", "main", "README.md")

    def test_github_raw_fallback_branch_succeeds(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        callback = MagicMock()

        with patch.object(delegate, "try_raw_download", side_effect=[None, b"fallback"]):
            result = delegate.download_github_file(
                dep, "README.md", ref="main", verbose_callback=callback
            )

        assert result == b"fallback"
        callback.assert_called_once_with("Downloaded file: github.com/owner/repo/README.md")

    def test_generic_host_raw_success_returns_content(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.return_value = fake_response(200, content=b"raw-host")
        callback = MagicMock()

        with patch(
            "apm_cli.deps.download_strategies.is_github_hostname",
            return_value=False,
        ):
            result = delegate.download_github_file(dep, "README.md", verbose_callback=callback)

        assert result == b"raw-host"
        assert (
            callback.call_args_list[0]
            .args[0]
            .startswith("Trying raw URL on generic host gitea.example.com")
        )
        assert callback.call_args_list[1].args[0] == (
            "Downloaded file: gitea.example.com/owner/repo/README.md"
        )

    def test_generic_host_raw_exception_raises_network_error(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        callback = MagicMock()
        host._resilient_get.side_effect = requests.RequestException("raw failed")

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["api-url"]),
        ):
            with pytest.raises(RuntimeError, match=r"Network error downloading README\.md"):
                delegate.download_github_file(dep, "README.md", verbose_callback=callback)

    def test_generic_host_contents_headers_use_builder(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(200, content=b"ok")]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["api-url"]),
            patch.object(
                delegate, "_build_generic_host_auth_headers", return_value={"X-Test": "1"}
            ) as mock_headers,
        ):
            result = delegate.download_github_file(dep, "README.md")

        assert result == b"ok"
        mock_headers.assert_any_call(
            "gitea.example.com", host.auth_resolver.resolve.return_value, accept=None
        )
        mock_headers.assert_any_call(
            "gitea.example.com",
            host.auth_resolver.resolve.return_value,
            accept="application/json",
        )

    def test_contents_candidate_second_url_succeeds_after_404(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        callback = MagicMock()
        host._resilient_get.side_effect = [
            fake_response(404),
            fake_response(404),
            fake_response(200, content=b"ok", headers={"Content-Type": "text/plain"}),
        ]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["u1", "u2"]),
        ):
            result = delegate.download_github_file(dep, "README.md", verbose_callback=callback)

        assert result == b"ok"
        assert "trying next candidate: u2" in callback.call_args_list[-2].args[0]

    def test_candidate_non_404_error_raises_runtime_error(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(500)]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["u1", "u2"]),
        ):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                delegate.download_github_file(dep, "README.md")

    def test_non_default_ref_404_uses_unsupported_error_builder(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.side_effect = [
            fake_response(404),
            fake_response(404),
            fake_response(404),
        ]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["u1", "u2"]),
            patch.object(
                delegate, "_build_unsupported_or_missing_error", return_value="missing"
            ) as mock_error,
        ):
            with pytest.raises(RuntimeError, match="missing"):
                delegate.download_github_file(dep, "README.md", ref="feature")

        mock_error.assert_called_once()

    def test_fallback_ref_success_logs_download(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        callback = MagicMock()
        host._resilient_get.side_effect = [
            fake_response(404),
            fake_response(404),
            fake_response(200, content=b"ok", headers={"Content-Type": "text/plain"}),
        ]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(
                delegate, "_build_contents_api_urls", side_effect=[["u-main"], ["u-master"]]
            ),
        ):
            result = delegate.download_github_file(
                dep, "README.md", ref="main", verbose_callback=callback
            )

        assert result == b"ok"
        assert callback.call_args_list[-1].args[0] == (
            "Downloaded file: gitea.example.com/owner/repo/README.md"
        )

    def test_fallback_ref_non_404_raises_runtime_error(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(500)]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(
                delegate, "_build_contents_api_urls", side_effect=[["u-main"], ["u-master"]]
            ),
        ):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                delegate.download_github_file(dep, "README.md", ref="main")

    def test_confirmed_throttle_without_token_is_typed(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.return_value = fake_response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
        )

        with pytest.raises(GitHubThrottleError) as exc_info:
            delegate.download_github_file(dep, "README.md")

        assert str(exc_info.value) == "GitHub API throttle for github.com (HTTP 403)"
        assert exc_info.value.throttle.signal == "remaining-zero"

    def test_confirmed_throttle_with_token_is_typed(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.return_value = fake_response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
        )

        with pytest.raises(GitHubThrottleError) as exc_info:
            delegate.download_github_file(dep, "README.md")

        assert str(exc_info.value) == "GitHub API throttle for github.com (HTTP 403)"
        assert exc_info.value.throttle.signal == "remaining-zero"

    def test_auth_failure_retries_without_auth_and_succeeds(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        callback = MagicMock()
        host._resilient_get.side_effect = [
            fake_response(401),
            fake_response(200, content=b"ok", headers={"Content-Type": "text/plain"}),
        ]

        result = delegate.download_github_file(dep, "README.md", verbose_callback=callback)

        assert result == b"ok"
        assert callback.call_args_list[-1].args[0] == (
            "Downloaded file: github.com/owner/repo/README.md"
        )

    def test_auth_failure_without_token_uses_error_context(self) -> None:
        host = make_host(resolved_token=None)
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.return_value = fake_response(401)

        with pytest.raises(RuntimeError, match="Set a token"):
            delegate.download_github_file(dep, "README.md")

    def test_auth_failure_with_token_mentions_both_attempts(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.side_effect = [fake_response(401), fake_response(401)]

        with pytest.raises(RuntimeError, match="Both authenticated and unauthenticated access"):
            delegate.download_github_file(dep, "README.md")

    def test_ghe_auth_failure_mentions_token_permissions(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="tenant.ghe.com")
        host._resilient_get.return_value = fake_response(401)

        with pytest.raises(RuntimeError, match="check your GitHub token permissions"):
            delegate.download_github_file(dep, "README.md")

    def test_generic_host_auth_failure_mentions_generic_guidance(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="gitea.example.com")
        host._resilient_get.side_effect = [fake_response(404), fake_response(403)]

        with (
            patch("apm_cli.deps.download_strategies.is_github_hostname", return_value=False),
            patch.object(delegate, "_build_contents_api_urls", return_value=["u1"]),
        ):
            with pytest.raises(
                RuntimeError, match=r"Host gitea\.example\.com rejected the request"
            ):
                delegate.download_github_file(dep, "README.md")

    def test_other_http_error_becomes_runtime_error(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.return_value = fake_response(500)

        with pytest.raises(RuntimeError, match="HTTP 500"):
            delegate.download_github_file(dep, "README.md")

    def test_request_exception_becomes_network_error(self) -> None:
        host = make_host(resolved_token="gh-token")
        delegate = DownloadDelegate(host)
        dep = make_dep("owner/repo", host="github.com")
        host._resilient_get.side_effect = requests.RequestException("down")

        with pytest.raises(RuntimeError, match=r"Network error downloading README\.md"):
            delegate.download_github_file(dep, "README.md")


class TestDownloadStrategyHelpers:
    def test_build_generic_headers_without_auth_returns_accept_only(self) -> None:
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.example.com",
            SimpleNamespace(token=None, source=""),
            accept="application/json",
        )

        assert headers == {"Accept": "application/json"}

    def test_build_generic_headers_accepts_git_credential_tokens(self) -> None:
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.example.com",
            SimpleNamespace(token="tok", source="git-credential-fill"),
        )

        assert headers == {"Authorization": "token tok"}

    def test_build_generic_headers_accepts_org_scoped_tokens(self) -> None:
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.example.com",
            SimpleNamespace(token="tok", source="GITHUB_APM_PAT_ORG"),
        )

        assert headers == {"Authorization": "token tok"}

    def test_build_generic_headers_accepts_configured_ghes(self) -> None:
        with patch.dict("os.environ", {"GITHUB_HOST": "gitea.example.com"}):
            headers = DownloadDelegate._build_generic_host_auth_headers(
                "gitea.example.com",
                SimpleNamespace(token="tok", source="GITHUB_TOKEN"),
            )

        assert headers == {"Authorization": "token tok"}

    def test_extract_payload_returns_body_for_non_json_content_type_errors(self) -> None:
        response = fake_response(200, content=b"raw-bytes")
        response.headers = 7

        assert DownloadDelegate._extract_contents_api_payload(response, False) == b"raw-bytes"

    def test_extract_payload_invalid_json_returns_raw_body(self) -> None:
        response = fake_response(
            200,
            content=b"{broken",
            headers={"Content-Type": "application/json"},
        )

        assert DownloadDelegate._extract_contents_api_payload(response, False) == b"{broken"

    def test_extract_payload_invalid_base64_returns_body(self) -> None:
        payload = {"content": {"bad": True}, "encoding": "base64"}
        response = fake_response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        assert DownloadDelegate._extract_contents_api_payload(response, False) == response.content

    def test_extract_payload_non_base64_string_is_encoded(self) -> None:
        payload = {"content": "literal", "encoding": ""}
        response = fake_response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        assert DownloadDelegate._extract_contents_api_payload(response, False) == b"literal"

    def test_extract_payload_non_string_content_returns_body(self) -> None:
        payload = {"content": {"x": 1}, "encoding": ""}
        response = fake_response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        assert DownloadDelegate._extract_contents_api_payload(response, False) == response.content

    def test_generic_missing_error_lists_tried_families(self) -> None:
        message = DownloadDelegate._build_unsupported_or_missing_error(
            "gitea.example.com",
            "owner/repo",
            "README.md",
            "main",
            [
                "https://gitea.example.com/api/v1/repos/owner/repo/contents/README.md?ref=main",
                "https://gitea.example.com/api/v3/repos/owner/repo/contents/README.md?ref=main",
            ],
            is_github_host=False,
            fallback_ref="master",
        )

        assert "Tried URL families: raw, v1, v3" in message
        assert "tried refs: main, master" in message
        assert "virtual subdirectory packages" in message
