"""Artifactory archive download and extraction through DownloadDelegate."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.utils.archive import safe_extract_zip
from tests.utils.simulated_ado_chain import install_simulated_ado_chain

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-artifactory-netrc-isolation")]

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_host(
    *,
    github_token: str | None = None,
    ado_token: str | None = None,
    artifactory_token: str | None = None,
    registry_config=None,
    github_host: str = "github.com",
) -> MagicMock:
    """Build a minimal mock GitHubPackageDownloader host."""
    host = MagicMock()
    host.github_token = github_token
    host.ado_token = ado_token
    host.artifactory_token = artifactory_token
    host.registry_config = registry_config
    host.github_host = github_host

    # Shared auth_resolver mock
    auth = MagicMock()
    ctx = MagicMock()
    resolved_token = ado_token if ado_token is not None else github_token
    ctx.token = resolved_token
    ctx.source = (
        "ADO_APM_PAT" if ado_token is not None else ("GITHUB_APM_PAT_ORG" if github_token else "")
    )
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    auth.resolve.return_value = ctx
    auth.resolve_for_dep.return_value = ctx
    install_simulated_ado_chain(auth)
    auth.classify_host.return_value = MagicMock(
        kind="github",
        api_base="https://api.github.com",
    )
    auth.build_error_context.return_value = "Set GITHUB_APM_PAT."

    def _is_generic_dep(dep_ref) -> bool:
        dep_host = getattr(dep_ref, "host", None)
        if not dep_host:
            return False
        return dep_host != "github.com" and not str(dep_host).endswith(".ghe.com")

    host._resolve_dep_token.side_effect = lambda dep_ref=None: (
        None if _is_generic_dep(dep_ref) else github_token
    )
    host._resolve_dep_auth_ctx.side_effect = lambda dep_ref=None: (
        None if _is_generic_dep(dep_ref) else ctx
    )
    host.auth_resolver = auth
    return host


def _fake_response(
    status_code: int = 200,
    content: bytes = b"hello",
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.text = content.decode("utf-8", errors="replace")
    resp.iter_content.return_value = iter([content] if content else [])
    http_error = requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status.side_effect = http_error if status_code >= 400 else None
    return resp


def _make_zip(files: dict[str, bytes], root_prefix: str = "repo-main/") -> bytes:
    """Build an in-memory zip archive with the given files under *root_prefix*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Write the root directory entry first
        zf.mkdir(root_prefix)
        for rel_path, data in files.items():
            zf.writestr(root_prefix + rel_path, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _debug helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resilient_get
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_repo_url
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_artifactory_headers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_artifactory_archive
# ---------------------------------------------------------------------------


class TestDownloadArtifactoryArchive:
    def _delegate(self, resp_sequence=None) -> tuple[DownloadDelegate, MagicMock]:
        host = _make_host()
        d = DownloadDelegate(host)
        if resp_sequence is not None:
            host._resilient_get.side_effect = resp_sequence
        return d, host

    def test_success_extracts_files_stripping_root_prefix(self, tmp_path: Path) -> None:
        zip_bytes = _make_zip({"apm.yml": b"name: test"}, root_prefix="repo-main/")
        resp = _fake_response(200, zip_bytes)
        d, host = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").read_bytes() == b"name: test"
        assert host._resilient_get.call_args.kwargs["stream"] is True
        assert host._resilient_get.call_args.kwargs["allow_netrc"] is False
        assert "allow_redirects" not in host._resilient_get.call_args.kwargs

    def test_http_non_200_tries_next_url(self, tmp_path: Path) -> None:
        zip_bytes = _make_zip({"apm.yml": b"name: test"})
        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([fail_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/fail.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_all_urls_fail_raises_runtime_error(self, tmp_path: Path) -> None:
        fail_resp = _fake_response(404, b"")
        d, _ = self._delegate([fail_resp, fail_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/a.zip",
                "https://art.example.com/b.zip",
            ],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_bad_zip_file_tries_next(self, tmp_path: Path) -> None:
        bad_resp = _fake_response(200, b"not a zip")
        zip_bytes = _make_zip({"apm.yml": b"ok"})
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([bad_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/bad.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_archive_too_large_skips_to_next(self, tmp_path: Path) -> None:
        huge_resp = MagicMock()
        huge_resp.status_code = 200
        huge_resp.headers = {"Content-Length": str(600 * 1024 * 1024)}
        huge_resp.iter_content.return_value = iter(())

        zip_bytes = _make_zip({"apm.yml": b"ok"})
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([huge_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/huge.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_archive_stream_limit_skips_to_next_url(self, tmp_path: Path) -> None:
        huge_resp = MagicMock()
        huge_resp.status_code = 200
        huge_resp.headers = {}
        huge_resp.iter_content.return_value = iter([b"x" * (2 * 1024 * 1024)])

        ok_resp = _fake_response(200, _make_zip({"apm.yml": b"ok"}))
        d, _ = self._delegate([huge_resp, ok_resp])
        with (
            patch.dict("os.environ", {"ARTIFACTORY_MAX_ARCHIVE_MB": "1"}),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=[
                    "https://art.example.com/huge.zip",
                    "https://art.example.com/ok.zip",
                ],
            ),
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_empty_archive_raises_runtime_error(self, tmp_path: Path) -> None:
        """A valid zip that contains no entries raises RuntimeError."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass  # empty zip
        empty_zip = buf.getvalue()
        resp = _fake_response(200, empty_zip)
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/empty.zip"],
        ):
            with pytest.raises((RuntimeError, Exception)):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_single_file_archive_extracted_as_is(self, tmp_path: Path) -> None:
        """A zip whose first entry is a plain file (not a directory) is extracted as-is."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.md", b"readme content")
        single_file_zip = buf.getvalue()
        resp = _fake_response(200, single_file_zip)
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/single.zip"],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "README.md").exists()

    def test_single_file_archive_rejects_path_traversal(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", b"nope")
        resp = _fake_response(200, buf.getvalue())
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/single.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_archive_rejects_uncompressed_size_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        zip_bytes = _make_zip({"big.bin": b"x" * 2048}, root_prefix="repo-main/")
        resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([resp])
        monkeypatch.setitem(safe_extract_zip.__kwdefaults__, "max_uncompressed", 1024)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_request_exception_tries_next(self, tmp_path: Path) -> None:
        d, host_mock = self._delegate()
        host_mock._resilient_get.side_effect = [
            requests.RequestException("network error"),
            _fake_response(200, _make_zip({"f.txt": b"data"})),
        ]
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/fail.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "f.txt").exists()


# ---------------------------------------------------------------------------
# download_file_from_artifactory
# ---------------------------------------------------------------------------


class TestDownloadFileFromArtifactory:
    def test_falls_back_to_archive_download_when_fetch_returns_none(
        self,
    ) -> None:
        cfg = MagicMock()
        cfg.host = "art.example.com"
        client = MagicMock()
        client.fetch_file.return_value = None  # entry not found
        cfg.get_client.return_value = client

        zip_bytes = _make_zip({"apm.yml": b"archive content"})
        resp = _fake_response(200, zip_bytes)
        host = _make_host(registry_config=cfg)
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            result = d.download_file_from_artifactory(
                "art.example.com", "apm", "owner", "repo", "apm.yml", "main"
            )
        assert result == b"archive content"

    def test_raises_when_file_not_in_archive(self) -> None:
        cfg = MagicMock()
        cfg.host = "art.example.com"
        client = MagicMock()
        client.fetch_file.return_value = None
        cfg.get_client.return_value = client

        # Archive does NOT contain the requested file
        zip_bytes = _make_zip({"other.yml": b"other"})
        resp = _fake_response(200, zip_bytes)
        host = _make_host(registry_config=cfg)
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download file"):
                d.download_file_from_artifactory(
                    "art.example.com",
                    "apm",
                    "owner",
                    "repo",
                    "missing.yml",
                    "main",
                )


# ---------------------------------------------------------------------------
# try_raw_download
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_ado_file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_gitlab_file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_github_file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_configured_ghes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_contents_api_urls
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_generic_host_auth_headers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_contents_api_payload
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_unsupported_or_missing_error
# ---------------------------------------------------------------------------
