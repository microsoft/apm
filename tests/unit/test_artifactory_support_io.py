"""Tests for JFrog Artifactory VCS repository support (filesystem, process and CLI boundary cases)."""

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.deps.artifactory_orchestrator import ArtifactoryOrchestrator
from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.drift import build_download_ref
from apm_cli.models.apm_package import (
    DependencyReference,
    GitReferenceType,
)
from apm_cli.utils.archive import safe_extract_zip
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.github_host import (
    build_artifactory_archive_url,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-host-credential-resolution", "P-OWN-git-transport-selection"),
]


def _mock_stream_response(
    status_code: int = 200,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.iter_content.return_value = iter([content] if content else [])
    return resp


def _archive_response(status_code: int, content: bytes = b"") -> MagicMock:
    """Return a response double compatible with streamed archive downloads."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Content-Length": str(len(content))}
    response.iter_content.return_value = iter([content] if content else [])
    return response


# ── github_host.py: Artifactory path helpers ──


# ── apm_package.py: DependencyReference Artifactory parsing ──


# ── DependencyReference shorthand under registry-only mode ──


# ── Artifactory boundary probe (mirrors native GitLab probing pattern) ──


# ── ArtifactoryOrchestrator: multi-segment repo support ──


# -- token_manager.py: Artifactory token support --


# ── github_downloader.py: Artifactory download methods ──


class TestArtifactoryDownloader:
    """Test GitHubPackageDownloader Artifactory methods."""

    def setup_method(self):
        """Set up test fixtures."""
        self.downloader = GitHubPackageDownloader()
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_artifactory_token_setup(self):
        """Downloader picks up ARTIFACTORY_APM_TOKEN from environment."""
        with patch.dict(os.environ, {"ARTIFACTORY_APM_TOKEN": "art-token-123"}, clear=True):
            dl = GitHubPackageDownloader()
            assert dl.has_artifactory_token is True
            assert dl.artifactory_token == "art-token-123"

    def test_no_artifactory_token(self):
        """Downloader handles missing Artifactory token gracefully."""
        with patch.dict(os.environ, {}, clear=True):
            dl = GitHubPackageDownloader()
            assert dl.has_artifactory_token is False
            assert dl.artifactory_token is None

    def test_get_artifactory_headers_with_token(self):
        """Headers carry the registry token only for the configured registry origin."""
        env = {
            "PROXY_REGISTRY_URL": "https://art.example.com/artifactory/github",
            "PROXY_REGISTRY_TOKEN": "my-token",
        }
        with patch.dict(os.environ, env, clear=True):
            dl = GitHubPackageDownloader()
            assert dl._get_artifactory_headers(scheme="https", host="art.example.com") == {
                "Authorization": "Bearer my-token"
            }
            assert dl._get_artifactory_headers(scheme="https", host="other.example.com") == {}
            assert dl._get_artifactory_headers(scheme="http", host="art.example.com") == {}

    def test_get_artifactory_headers_token_without_registry_url(self):
        """A registry token without a configured registry URL is never attached."""
        with patch.dict(os.environ, {"ARTIFACTORY_APM_TOKEN": "my-token"}, clear=True):
            dl = GitHubPackageDownloader()
            assert dl._get_artifactory_headers(scheme="https", host="art.example.com") == {}

    def test_get_artifactory_headers_without_token(self):
        """Headers are empty when no token is set."""
        with patch.dict(os.environ, {}, clear=True):
            dl = GitHubPackageDownloader()
            headers = dl._get_artifactory_headers(scheme="https", host="art.example.com")
            assert headers == {}

    def test_should_use_artifactory_proxy_github(self):
        """GitHub-hosted deps should route through Artifactory proxy."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        assert self.downloader._should_use_artifactory_proxy(dep)

    def test_should_not_proxy_ado(self):
        """Azure DevOps deps should NOT route through Artifactory proxy."""
        dep = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")
        assert not self.downloader._should_use_artifactory_proxy(dep)

    def test_should_not_proxy_already_artifactory(self):
        """Already-Artifactory deps should NOT be double-proxied."""
        dep = DependencyReference.parse("art.example.com/artifactory/github/owner/repo")
        assert not self.downloader._should_use_artifactory_proxy(dep)

    def test_should_not_proxy_non_github_fqdn(self):
        """Non-GitHub FQDN hosts should NOT route through Artifactory."""
        dep = DependencyReference.parse("gitlab.com/owner/repo")
        assert not self.downloader._should_use_artifactory_proxy(dep)

    def test_parse_artifactory_base_url_valid(self):
        """Parse valid PROXY_REGISTRY_URL."""
        with patch.dict(
            os.environ,
            {"PROXY_REGISTRY_URL": "https://art.example.com/artifactory/github"},
        ):
            result = self.downloader._parse_artifactory_base_url()
            assert result is not None
            host, prefix, scheme = result
            assert host == "art.example.com"
            assert prefix == "artifactory/github"
            assert scheme == "https"

    def test_parse_artifactory_base_url_trailing_slash(self):
        """Trailing slash in URL should be stripped."""
        with patch.dict(
            os.environ,
            {"PROXY_REGISTRY_URL": "https://art.example.com/artifactory/github/"},
        ):
            result = self.downloader._parse_artifactory_base_url()
            assert result is not None
            host, prefix, scheme = result  # noqa: RUF059
            assert prefix == "artifactory/github"

    def test_parse_artifactory_base_url_empty(self):
        """Returns None for empty string."""
        with patch.dict(os.environ, {"PROXY_REGISTRY_URL": ""}, clear=True):
            result = self.downloader._parse_artifactory_base_url()
            assert result is None


class TestArtifactoryArchiveDownload:
    """Test _download_artifactory_archive zip handling."""

    def setup_method(self):
        self.downloader = GitHubPackageDownloader()
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_zip_bytes(self, root_prefix="repo-main/", files=None):
        """Create a zip archive in memory mimicking GitHub archive structure."""
        if files is None:
            files = {
                "apm.yml": b"name: test-package\nversion: 1.0.0\n",
                "README.md": b"# Test\n",
            }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(root_prefix, "")  # root directory entry
            for name, content in files.items():
                zf.writestr(f"{root_prefix}{name}", content)
        return buf.getvalue()

    def test_successful_extraction(self):
        """Archive is downloaded and extracted with root prefix stripped."""
        zip_bytes = self._make_zip_bytes()
        mock_resp = _mock_stream_response(200, zip_bytes)

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp) as mock_get:
            self.downloader._download_artifactory_archive(
                "art.example.com",
                "artifactory/github",
                "owner",
                "repo",
                "main",
                target,
            )

        assert (target / "apm.yml").exists()
        assert (target / "README.md").exists()
        # Root prefix directory should NOT appear as a nested folder
        assert not (target / "repo-main").exists()
        assert mock_get.call_args.kwargs["stream"] is True

    def test_falls_back_to_tags_url(self):
        """When heads URL returns 404, falls back to tags URL."""
        zip_bytes = self._make_zip_bytes()
        mock_resp_404 = _mock_stream_response(404)
        mock_resp_200 = _mock_stream_response(200, zip_bytes)

        target = self.temp_dir / "pkg"
        with patch.object(
            self.downloader,
            "_resilient_get",
            side_effect=[mock_resp_404, mock_resp_200],
        ):
            self.downloader._download_artifactory_archive(
                "art.example.com",
                "artifactory/github",
                "owner",
                "repo",
                "v1.0.0",
                target,
            )

        assert (target / "apm.yml").exists()

    def test_raises_on_all_failures(self):
        """Raises RuntimeError when both URLs fail."""
        mock_resp = _mock_stream_response(404)

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Failed to download"):
                self.downloader._download_artifactory_archive(
                    "art.example.com",
                    "artifactory/github",
                    "owner",
                    "repo",
                    "main",
                    target,
                )

    def test_raises_on_empty_archive(self):
        """Raises RuntimeError for empty zip archive."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass  # empty zip
        mock_resp = _mock_stream_response(200, buf.getvalue())

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Empty archive"):
                self.downloader._download_artifactory_archive(
                    "art.example.com",
                    "artifactory/github",
                    "owner",
                    "repo",
                    "main",
                    target,
                )

    def _make_symlink_zip_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("repo-main/", "")
            zf.writestr("repo-main/apm.yml", b"name: test\nversion: 1.0.0\n")
            link = zipfile.ZipInfo("repo-main/link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            zf.writestr(link, "apm.yml")
        return buf.getvalue()

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                "symlink",
                "Unsafe zip archive from https://art.example.com/artifactory/github/owner/"
                "repo/archive/refs/heads/main.zip: Refusing to extract symlink: repo-main/link",
                id="unsafe-member",
            ),
            pytest.param(
                b"<!DOCTYPE html><html></html>",
                "Invalid zip archive from https://art.example.com/artifactory/github/owner/"
                "repo/archive/refs/heads/main.zip",
                id="invalid-zip",
            ),
        ],
    )
    def test_archive_rejection_not_masked_by_later_404(self, body, expected):
        """A rejected 200 archive stays the reported cause when later URLs 404 (#3110)."""
        content = self._make_symlink_zip_bytes() if body == "symlink" else body
        responses = [_mock_stream_response(200, content)] + [
            _mock_stream_response(404) for _ in range(4)
        ]

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", side_effect=responses):
            with pytest.raises(RuntimeError) as exc_info:
                self.downloader._download_artifactory_archive(
                    "art.example.com",
                    "artifactory/github",
                    "owner",
                    "repo",
                    "main",
                    target,
                )

        message = str(exc_info.value)
        assert message.endswith(expected)
        assert "HTTP 404" not in message

    @pytest.mark.parametrize(
        ("bodies", "expected"),
        [
            pytest.param(
                [None, "symlink"],
                "Archive error: Unsafe zip archive from https://art.example.com/artifactory/"
                "github/owner/repo/-/archive/main/repo-main.zip: Refusing to extract symlink: "
                "repo-main/link",
                id="404-then-rejection",
            ),
            pytest.param(
                ["symlink", b"<!DOCTYPE html><html></html>"],
                "Archive error: Unsafe zip archive from https://art.example.com/artifactory/"
                "github/owner/repo/archive/refs/heads/main.zip: Refusing to extract symlink: "
                "repo-main/link",
                id="first-rejection-wins",
            ),
        ],
    )
    def test_first_archive_rejection_reported_regardless_of_order(self, bodies, expected):
        """The first rejected archive is reported even after earlier 404s or later rejections (#3110)."""
        responses = []
        for body in bodies:
            if body is None:
                responses.append(_mock_stream_response(404))
            elif body == "symlink":
                responses.append(_mock_stream_response(200, self._make_symlink_zip_bytes()))
            else:
                responses.append(_mock_stream_response(200, body))
        responses += [_mock_stream_response(404) for _ in range(5 - len(bodies))]

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", side_effect=responses):
            with pytest.raises(RuntimeError) as exc_info:
                self.downloader._download_artifactory_archive(
                    "art.example.com",
                    "artifactory/github",
                    "owner",
                    "repo",
                    "main",
                    target,
                )

        message = str(exc_info.value)
        assert message.endswith(expected)
        assert "HTTP 404" not in message

    def test_rejected_archive_then_valid_archive_succeeds(self):
        """A later valid archive still wins after an earlier archive is rejected."""
        responses = [
            _mock_stream_response(200, self._make_symlink_zip_bytes()),
            _mock_stream_response(404),
            _mock_stream_response(200, self._make_zip_bytes()),
        ]

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", side_effect=responses):
            self.downloader._download_artifactory_archive(
                "art.example.com",
                "artifactory/github",
                "owner",
                "repo",
                "main",
                target,
            )

        assert (target / "README.md").exists()

    def test_nested_directories_extracted(self):
        """Nested directories within the archive are properly extracted."""
        files = {
            "apm.yml": b"name: test\nversion: 1.0.0\n",
            "skills/review.prompt.md": b"# Review\n",
            "skills/debug.prompt.md": b"# Debug\n",
        }
        zip_bytes = self._make_zip_bytes(files=files)
        mock_resp = _mock_stream_response(200, zip_bytes)

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            self.downloader._download_artifactory_archive(
                "art.example.com",
                "artifactory/github",
                "owner",
                "repo",
                "main",
                target,
            )

        assert (target / "skills" / "review.prompt.md").exists()
        assert (target / "skills" / "debug.prompt.md").exists()


# ── Edge case and security tests ──


class TestArtifactoryEdgeCases:
    """Test edge cases and security fail-safes."""

    def setup_method(self):
        self.downloader = GitHubPackageDownloader()
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_zip_bytes(self, root_prefix="repo-main/", files=None):
        if files is None:
            files = {"apm.yml": b"name: test\nversion: 1.0.0\n"}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(root_prefix, "")
            for name, content in files.items():
                zf.writestr(f"{root_prefix}{name}", content)
        return buf.getvalue()

    def test_zip_path_traversal_rejected(self):
        """Zip entries with ../ path traversal are rejected (CWE-22)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("repo-main/", "")
            zf.writestr("repo-main/apm.yml", b"name: test\nversion: 1.0.0\n")
            zf.writestr("repo-main/../../../etc/passwd", b"root:x:0:0")
        mock_resp = _mock_stream_response(200, buf.getvalue())

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Failed to download"):
                self.downloader._download_artifactory_archive(
                    "art.example.com", "artifactory/github", "owner", "repo", "main", target
                )
        assert not (self.temp_dir / "etc").exists()

    def test_single_file_zip_path_traversal_rejected(self):
        """Single-file archives also reject traversal instead of using extractall()."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", b"nope")
        mock_resp = _mock_stream_response(200, buf.getvalue())

        target = self.temp_dir / "pkg"
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Failed to download"):
                self.downloader._download_artifactory_archive(
                    "art.example.com", "artifactory/github", "owner", "repo", "main", target
                )
        assert not (self.temp_dir / "escape.txt").exists()

    def test_uncompressed_archive_limit_enforced(self, monkeypatch: pytest.MonkeyPatch):
        """The shared safe zip limits apply to Artifactory archive extraction."""
        zip_bytes = self._make_zip_bytes(files={"big.bin": b"x" * 2048})
        mock_resp = _mock_stream_response(200, zip_bytes)

        target = self.temp_dir / "pkg"
        monkeypatch.setitem(safe_extract_zip.__kwdefaults__, "max_uncompressed", 1024)
        with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Failed to download"):
                self.downloader._download_artifactory_archive(
                    "art.example.com", "artifactory/github", "owner", "repo", "main", target
                )

    def test_oversized_archive_rejected(self):
        """Archives exceeding ARTIFACTORY_MAX_ARCHIVE_MB are rejected."""
        mock_resp = _mock_stream_response(200, b"x")

        target = self.temp_dir / "pkg"
        # Set limit to 0 MB so any archive is too large
        with patch.dict(os.environ, {"ARTIFACTORY_MAX_ARCHIVE_MB": "0"}):
            with patch.object(self.downloader, "_resilient_get", return_value=mock_resp):
                with pytest.raises(RuntimeError, match="Failed to download"):
                    self.downloader._download_artifactory_archive(
                        "art.example.com",
                        "artifactory/github",
                        "owner",
                        "repo",
                        "main",
                        target,
                    )

    def test_parse_base_url_rejects_ftp_scheme(self):
        """PROXY_REGISTRY_URL with non-http(s) scheme is rejected."""
        with patch.dict(
            os.environ,
            {"PROXY_REGISTRY_URL": "ftp://art.example.com/artifactory/github"},
        ):
            result = self.downloader._parse_artifactory_base_url()
            assert result is None

    def test_parse_base_url_rejects_no_scheme(self):
        """PROXY_REGISTRY_URL without scheme is rejected."""
        with patch.dict(
            os.environ,
            {"PROXY_REGISTRY_URL": "art.example.com/artifactory/github"},
        ):
            result = self.downloader._parse_artifactory_base_url()
            assert result is None

    def test_parse_base_url_accepts_http(self):
        """PROXY_REGISTRY_URL with http scheme is accepted (local dev)."""
        with patch.dict(
            os.environ,
            {"PROXY_REGISTRY_URL": "http://localhost:8081/artifactory/github"},
        ):
            result = self.downloader._parse_artifactory_base_url()
            assert result is not None
            assert result[0] == "localhost"
            assert result[2] == "http"

    def test_malformed_repo_url_raises(self):
        """Malformed repo_url without owner/repo raises ValueError."""
        dep = DependencyReference.parse("art.example.com/artifactory/github/owner/repo")
        # Manually corrupt the repo_url to simulate edge case
        dep.repo_url = "single-segment"
        with pytest.raises(ValueError, match="expected 'owner/repo' format"):
            self.downloader._download_package_from_artifactory(dep, self.temp_dir / "pkg")

    def test_no_corporate_values_in_source(self):
        """Verify no corporate/internal hostnames leak into Artifactory-related source files."""
        import pathlib

        src_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "apm_cli"
        target_files = [
            src_dir / "utils" / "github_host.py",
            src_dir / "deps" / "github_downloader.py",
            src_dir / "models" / "dependency.py",
            src_dir / "commands" / "install.py",
            src_dir / "core" / "token_manager.py",
            src_dir / "install" / "artifactory_resolver.py",
            src_dir / "install" / "package_resolution.py",
            src_dir / "models" / "dependency" / "reference.py",
        ]
        forbidden = ["checkpoint", "chkp"]
        for py_file in target_files:
            if not py_file.exists():
                continue
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for term in forbidden:
                assert term.lower() not in content.lower(), (
                    f"Found forbidden term '{term}' in {py_file}"
                )


class TestFrozenArtifactoryCommitReplay:
    """Regression coverage for immutable Artifactory archive replay."""

    def test_frozen_replay_reaches_generic_commit_archive_via_proxy(self, tmp_path: Path) -> None:
        """A locked tag replays its SHA through the immutable proxy candidate."""
        commit = "a" * 40
        owner = "testorg"
        repo = "testrepo"
        prefix = "artifactory/github"
        host_name = "art.example.com"
        manifest_ref = DependencyReference(
            repo_url=f"{owner}/{repo}",
            host="github.com",
            reference="v1.2.3",
        )

        source = tmp_path / "source"
        (source / ".apm" / "instructions").mkdir(parents=True)
        (source / "apm.yml").write_bytes(b"name: immutable-proxy-test\nversion: 1.0.0\n")
        (source / ".apm" / "instructions" / "immutable.md").write_bytes(b"immutable bytes\n")
        content_hash = compute_package_hash(source)

        locked = LockedDependency(
            repo_url=f"{owner}/{repo}",
            host=host_name,
            registry_prefix=prefix,
            resolved_ref="v1.2.3",
            resolved_commit=commit,
            content_hash=content_hash,
            package_type="apm_package",
            depth=1,
        )
        lockfile = LockFile()
        lockfile.add_dependency(locked)
        lock_path = tmp_path / "apm.lock.yaml"
        lockfile.write(lock_path)
        lock_before = lock_path.read_bytes()

        frozen_ref = build_download_ref(
            manifest_ref,
            lockfile,
            update_refs=False,
            ref_changed=False,
        )
        assert frozen_ref.reference == commit
        assert frozen_ref.host == host_name
        assert frozen_ref.artifactory_prefix == prefix

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as archive_zip:
            archive_zip.writestr(f"{repo}-{commit}/", "")
            archive_zip.writestr(
                f"{repo}-{commit}/apm.yml",
                b"name: immutable-proxy-test\nversion: 1.0.0\n",
            )
            archive_zip.writestr(
                f"{repo}-{commit}/.apm/instructions/immutable.md",
                b"immutable bytes\n",
            )
        archive_bytes = archive.getvalue()

        expected_urls = build_artifactory_archive_url(
            host_name,
            prefix,
            owner,
            repo,
            commit,
        )
        request_calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

        def proxy_get(url: str, headers: dict[str, str], **kwargs: object) -> MagicMock:
            request_calls.append((url, headers, kwargs))
            if url == expected_urls[0]:
                return _archive_response(200, archive_bytes)
            return _archive_response(404)

        registry_config = MagicMock()
        registry_config.headers_for.return_value = {"Authorization": "Bearer proxy-test-token"}
        proxy_host = MagicMock()
        proxy_host.registry_config = registry_config
        proxy_host.artifactory_token = None
        proxy_host._resilient_get.side_effect = proxy_get

        result = ArtifactoryOrchestrator(DownloadDelegate(proxy_host)).download_package(
            frozen_ref,
            tmp_path / "cold-cache",
            proxy_info=(host_name, prefix, "https"),
        )

        assert [url for url, _, _ in request_calls] == [expected_urls[0]]
        assert urlparse(request_calls[0][0]).path == (
            f"/{prefix}/{owner}/{repo}/archive/{commit}.zip"
        )
        assert {urlparse(url).hostname for url, _, _ in request_calls} == {host_name}
        assert all(
            headers == {"Authorization": "Bearer proxy-test-token"}
            for _, headers, _ in request_calls
        )
        assert all("allow_redirects" not in kwargs for _, _, kwargs in request_calls)
        assert all(kwargs["stream"] is True for _, _, kwargs in request_calls)
        assert result.resolved_reference.ref_type == GitReferenceType.COMMIT
        assert result.resolved_reference.resolved_commit == commit
        assert result.package.resolved_commit == commit
        assert compute_package_hash(result.install_path) == content_hash
        assert (result.install_path / ".apm" / "instructions" / "immutable.md").read_bytes() == (
            b"immutable bytes\n"
        )
        assert lock_path.read_bytes() == lock_before

        replayed = LockedDependency.from_dependency_ref(
            manifest_ref,
            result.resolved_reference.resolved_commit,
            depth=1,
            resolved_by=None,
            registry_config=SimpleNamespace(host=host_name, prefix=prefix),
        )
        replayed.content_hash = content_hash
        replayed.package_type = "apm_package"
        replayed_lockfile = LockFile(generated_at=lockfile.generated_at)
        replayed_lockfile.add_dependency(replayed)
        replayed_lock_path = tmp_path / "replayed.lock.yaml"
        replayed_lockfile.write(replayed_lock_path)
        assert replayed_lock_path.read_bytes() == lock_before


# -- PROXY_REGISTRY_ONLY mode tests --


# -- RegistryConfig: FQDN / prefix split and generic registry --


# -- drift.py: build_download_ref with registry_prefix --


# -- RegistryConfig.validate_lockfile_deps: conflict detection --


# -- RegistryConfig.find_missing_hashes: supply chain integrity --


# -- RegistryClient protocol and ArtifactoryRegistryClient --


# -- Archive Entry Download: fetch individual files from zip --


# -- Fix A: _parse_artifactory_base_url reads PROXY_REGISTRY_URL --


# -- Fix B: virtual subdirectory uses lockfile FQDN (Mode 1) --


# -- Backward compat: deprecated ARTIFACTORY_ONLY still works --
