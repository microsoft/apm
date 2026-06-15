"""Integration tests for deps/registry/ and deps/revision_pins.py coverage.

Exercises RegistryClient, RegistryAuthContext, RegistryPackageResolver,
check_registry_locked_dep, RevisionPinUpdate helpers, extract_archive, and
the bare_cache scrub utility — all with hermetic mocking (no live network).

Run with::

    uv run --extra dev pytest tests/integration/test_deps_registry_coverage.py -x -q
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps.registry.auth import (
    RegistryAuthContext,
    lookup_name_for_url,
    make_auth_context,
    registry_token_env_var,
    resolve_for_url,
)
from apm_cli.deps.registry.client import (
    PublishResult,
    RegistryClient,
    RegistryError,
    VersionEntry,
)
from apm_cli.deps.registry.extractor import (
    HashMismatchError,
    UnknownArchiveFormatError,
    extract_archive,
    verify_sha256,
)
from apm_cli.deps.registry.resolver import (
    RegistryResolutionError,
    _split_owner_repo,
)
from apm_cli.deps.revision_pins import (
    RevisionPinUpdate,
    abbreviate_sha,
    apply_revision_pin_updates,
    find_latest_annotated_tag,
    is_full_revision_pin,
    render_revision_pin_update_plan,
    resolve_revision_pin_updates,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SHA = "a" * 40
_FAKE_SHA2 = "b" * 40


def _make_fake_session(
    *,
    status_code: int = 200,
    json_body: Any = None,
    content: bytes = b"",
    content_type: str = "application/json",
    raise_exc: Exception | None = None,
) -> MagicMock:
    """Return a mock requests.Session whose .request() returns a canned response."""
    session = MagicMock(spec=requests.Session)
    if raise_exc is not None:
        session.request.side_effect = raise_exc
        return session

    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.url = "https://registry.example.com/v1/test"
    response.headers = {"Content-Type": content_type}
    if json_body is not None:
        response.json.return_value = json_body
        response.content = b""
    else:
        response.json.side_effect = ValueError("no json")
        response.content = content
    return session


def _anon_auth() -> RegistryAuthContext:
    return RegistryAuthContext(registry_name=None, token=None)


def _bearer_auth(token: str = "tok123") -> RegistryAuthContext:  # noqa: S107
    return RegistryAuthContext(registry_name="corp", token=token)


def _basic_auth(user: str = "alice", pwd: str = "s3cr3t") -> RegistryAuthContext:  # noqa: S107
    return RegistryAuthContext(registry_name="corp", token=None, username=user, password=pwd)


def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    """Return a minimal in-memory tar.gz containing *files*."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Return a minimal in-memory zip containing *files*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------


class TestRegistryAuthContext:
    """Unit tests for RegistryAuthContext.auth_header()."""

    def test_bearer_header(self) -> None:
        """Bearer token produces 'Authorization: Bearer <token>'."""
        ctx = _bearer_auth("my-token")
        assert ctx.auth_header() == "Bearer my-token"

    def test_basic_header(self) -> None:
        """Username+password produces 'Authorization: Basic <b64>'."""
        ctx = _basic_auth("user", "pass")
        header = ctx.auth_header()
        assert header is not None
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header[len("Basic ") :]).decode()
        assert decoded == "user:pass"

    def test_anonymous_header_is_none(self) -> None:
        """No credentials → auth_header() returns None."""
        ctx = _anon_auth()
        assert ctx.auth_header() is None

    def test_bearer_beats_basic(self) -> None:
        """When both token and username/password are set, Bearer wins."""
        ctx = RegistryAuthContext(
            registry_name="corp",
            token="tok",
            username="alice",
            password="pw",
        )
        assert ctx.auth_header() == "Bearer tok"

    def test_basic_requires_both_user_and_pass(self) -> None:
        """Only username without password → anonymous (None)."""
        ctx = RegistryAuthContext(registry_name="corp", token=None, username="alice", password=None)
        assert ctx.auth_header() is None


class TestRegistryTokenEnvVar:
    """Tests for registry_token_env_var() name mangling."""

    def test_hyphens_become_underscores(self) -> None:
        assert registry_token_env_var("corp-main") == "APM_REGISTRY_TOKEN_CORP_MAIN"

    def test_dots_become_underscores(self) -> None:
        assert registry_token_env_var("corp.main") == "APM_REGISTRY_TOKEN_CORP_MAIN"

    def test_uppercase_applied(self) -> None:
        assert registry_token_env_var("myregistry") == "APM_REGISTRY_TOKEN_MYREGISTRY"


class TestMakeAuthContext:
    """Tests for make_auth_context() reading env vars."""

    def test_bearer_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bearer token is read from APM_REGISTRY_TOKEN_<NAME> env var."""
        monkeypatch.setenv("APM_REGISTRY_TOKEN_MYREGISTRY", "envtok")
        with patch("apm_cli.config.get_registry_config", return_value=None):
            ctx = make_auth_context("myregistry")
        assert ctx.token == "envtok"
        assert ctx.auth_header() == "Bearer envtok"

    def test_basic_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Username + password are read from APM_REGISTRY_USER/PASS env vars."""
        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "")
        monkeypatch.setenv("APM_REGISTRY_USER_CORP", "alice")
        monkeypatch.setenv("APM_REGISTRY_PASS_CORP", "s3cr3t")
        with patch("apm_cli.config.get_registry_config", return_value=None):
            ctx = make_auth_context("corp")
        assert ctx.username == "alice"
        assert ctx.password == "s3cr3t"

    def test_anonymous_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env vars → anonymous context."""
        # Ensure no stray env vars leak in.
        for key in (
            "APM_REGISTRY_TOKEN_TESTPKG",
            "APM_REGISTRY_USER_TESTPKG",
            "APM_REGISTRY_PASS_TESTPKG",
        ):
            monkeypatch.delenv(key, raising=False)
        with patch("apm_cli.config.get_registry_config", return_value=None):
            ctx = make_auth_context("testpkg")
        assert ctx.token is None
        assert ctx.auth_header() is None


class TestLookupNameForUrl:
    """Tests for lookup_name_for_url() longest-prefix matching."""

    _REGS: dict[str, str] = {  # noqa: RUF012
        "corp": "https://registry.corp.com/apm",
        "corp-teamA": "https://registry.corp.com/apm/team-a",
    }

    def test_exact_match(self) -> None:
        result = lookup_name_for_url("https://registry.corp.com/apm", self._REGS)
        assert result == "corp"

    def test_longer_prefix_wins(self) -> None:
        result = lookup_name_for_url(
            "https://registry.corp.com/apm/team-a/v1/packages/owner/repo/versions",
            self._REGS,
        )
        assert result == "corp-teamA"

    def test_no_match_returns_none(self) -> None:
        result = lookup_name_for_url("https://other.example.com/registry", self._REGS)
        assert result is None

    def test_empty_registries_returns_none(self) -> None:
        result = lookup_name_for_url("https://registry.corp.com/apm", {})
        assert result is None


class TestResolveForUrl:
    """Tests for resolve_for_url() end-to-end auth resolution."""

    def test_matched_registry_uses_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a registry matches the URL, its token is read from env."""
        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "mytoken")
        registries = {"corp": "https://registry.corp.com/apm"}
        with patch("apm_cli.config.get_registry_config", return_value=None):
            ctx = resolve_for_url("https://registry.corp.com/apm/v1/packages/o/r", registries)
        assert ctx.token == "mytoken"

    def test_unmatched_url_returns_anonymous(self) -> None:
        """No registry prefix match → anonymous context."""
        registries = {"corp": "https://registry.corp.com/apm"}
        ctx = resolve_for_url("https://other.host.com/packages", registries)
        assert ctx.token is None
        assert ctx.registry_name is None


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------


class TestVersionEntryFromDict:
    """Tests for VersionEntry.from_dict() parsing."""

    def test_valid_entry(self) -> None:
        entry = VersionEntry.from_dict(
            {"version": "1.2.3", "digest": "sha256:abc", "published_at": "2024-01-01T00:00:00Z"}
        )
        assert entry.version == "1.2.3"
        assert entry.digest == "sha256:abc"

    def test_missing_version_raises(self) -> None:
        with pytest.raises(RegistryError, match="missing 'version'"):
            VersionEntry.from_dict({"digest": "sha256:abc", "published_at": "2024-01-01T00:00:00Z"})

    def test_missing_digest_raises(self) -> None:
        with pytest.raises(RegistryError, match="missing 'digest'"):
            VersionEntry.from_dict({"version": "1.0.0", "published_at": "2024-01-01T00:00:00Z"})

    def test_missing_published_at_raises(self) -> None:
        with pytest.raises(RegistryError, match="missing 'published_at'"):
            VersionEntry.from_dict({"version": "1.0.0", "digest": "sha256:abc"})


class TestPublishResultFromDict:
    """Tests for PublishResult.from_dict() parsing."""

    def test_valid_result(self) -> None:
        result = PublishResult.from_dict(
            {
                "package": "owner/repo",
                "version": "1.0.0",
                "digest": "sha256:abc",
                "published_at": "2024-01-01T00:00:00Z",
            }
        )
        assert result.package == "owner/repo"
        assert result.version == "1.0.0"

    def test_missing_package_raises(self) -> None:
        with pytest.raises(RegistryError, match="missing 'package'"):
            PublishResult.from_dict({"version": "1.0.0", "digest": "sha256:abc"})

    def test_missing_version_raises(self) -> None:
        with pytest.raises(RegistryError, match="missing 'version'"):
            PublishResult.from_dict({"package": "o/r", "digest": "sha256:abc"})

    def test_optional_published_at(self) -> None:
        result = PublishResult.from_dict(
            {"package": "owner/repo", "version": "1.0.0", "digest": "sha256:abc"}
        )
        assert result.published_at is None


class TestRegistryClientListVersions:
    """Tests for RegistryClient.list_versions()."""

    def test_success_returns_version_entries(self) -> None:
        """list_versions parses a valid response into VersionEntry list."""
        payload = {
            "versions": [
                {
                    "version": "1.0.0",
                    "digest": "sha256:aaa",
                    "published_at": "2024-01-01T00:00:00Z",
                },
                {
                    "version": "1.1.0",
                    "digest": "sha256:bbb",
                    "published_at": "2024-02-01T00:00:00Z",
                },
            ]
        }
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        versions = client.list_versions("acme", "tool")
        assert len(versions) == 2
        assert versions[0].version == "1.0.0"
        assert versions[1].version == "1.1.0"

    def test_404_raises_registry_error(self) -> None:
        """HTTP 404 surfaces as RegistryError with status=404."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"title": "Not Found", "detail": "Package does not exist"}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.list_versions("acme", "tool")
        assert exc_info.value.status == 404

    def test_401_raises_registry_error(self) -> None:
        """HTTP 401 surfaces as RegistryError with status=401."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"title": "Unauthorized"}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.list_versions("acme", "tool")
        assert exc_info.value.status == 401

    def test_transport_error_raises_registry_error(self) -> None:
        """Connection-level failures become RegistryError with no status."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("network down")

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.list_versions("acme", "tool")
        assert exc_info.value.status is None

    def test_missing_versions_array_raises(self) -> None:
        """Response without 'versions' key raises RegistryError."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"data": []}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        with pytest.raises(RegistryError, match="missing 'versions'"):
            client.list_versions("acme", "tool")

    def test_bearer_header_sent(self) -> None:
        """Auth header is forwarded when a bearer token is configured."""
        payload = {"versions": []}
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        session.request.return_value = resp

        client = RegistryClient(
            "https://registry.example.com", _bearer_auth("tok-xyz"), session=session
        )
        client.list_versions("acme", "tool")

        call_kwargs = session.request.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer tok-xyz"

    def test_url_uses_percent_encoded_owner_repo(self) -> None:
        """list_versions percent-encodes owner/repo path segments."""
        import urllib.parse

        payload = {"versions": []}
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.url = "https://registry.example.com/v1/packages/my%2Borg/my%2Btool/versions"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        client.list_versions("my+org", "my+tool")

        call_url: str = session.request.call_args[1]["url"]
        parsed = urllib.parse.urlparse(call_url)
        segments = parsed.path.split("/")
        # Segments: ['', 'v1', 'packages', encoded_owner, encoded_repo, 'versions']
        assert urllib.parse.unquote(segments[3]) == "my+org"
        assert urllib.parse.unquote(segments[4]) == "my+tool"


class TestRegistryClientDownloadArchive:
    """Tests for RegistryClient.download_archive()."""

    def test_returns_bytes_and_content_type(self) -> None:
        """download_archive returns raw bytes and stripped content-type."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0/download"
        resp.headers = {"Content-Type": "application/gzip; charset=binary"}
        resp.content = b"\x1f\x8b fake gzip"
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        data, ctype = client.download_archive("acme", "tool", "1.0.0")
        assert data == b"\x1f\x8b fake gzip"
        assert ctype == "application/gzip"

    def test_http_error_raises(self) -> None:
        """HTTP 403 raises RegistryError with status=403."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 403
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0/download"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"title": "Forbidden"}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _anon_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.download_archive("acme", "tool", "1.0.0")
        assert exc_info.value.status == 403


class TestRegistryClientPublishVersion:
    """Tests for RegistryClient.publish_version()."""

    def test_success_with_json_body(self) -> None:
        """Publish returns a PublishResult when server sends 201 JSON."""
        payload = {
            "package": "acme/tool",
            "version": "1.0.0",
            "digest": "sha256:abc123",
            "published_at": "2024-01-01T00:00:00Z",
        }
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 201
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0"
        resp.headers = {"Content-Type": "application/json"}
        resp.content = b'{"package":"acme/tool","version":"1.0.0","digest":"sha256:abc123"}'
        resp.json.return_value = payload
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        result = client.publish_version("acme", "tool", "1.0.0", b"fake-zip-bytes")
        assert result.package == "acme/tool"
        assert result.version == "1.0.0"

    def test_success_with_empty_body_computes_digest(self) -> None:
        """publish_version computes sha256 locally when server returns empty body."""
        archive = b"fake archive content"
        expected_digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"

        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 201
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0"
        resp.headers = {"Content-Type": "application/json"}
        resp.content = b""
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        result = client.publish_version("acme", "tool", "1.0.0", archive)
        assert result.digest == expected_digest
        assert result.published_at is None

    def test_409_conflict_raises(self) -> None:
        """HTTP 409 (version already exists) raises RegistryError with status=409."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 409
        resp.url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0"
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"title": "Conflict", "detail": "version already exists"}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.publish_version("acme", "tool", "1.0.0", b"data")
        assert exc_info.value.status == 409

    def test_transport_error_raises(self) -> None:
        """Network error during publish raises RegistryError with no status."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.Timeout("timed out")

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.publish_version("acme", "tool", "1.0.0", b"data")
        assert exc_info.value.status is None


class TestRegistryClientFetchFromUrl:
    """Tests for RegistryClient.fetch_from_url()."""

    def test_success(self) -> None:
        """fetch_from_url returns bytes and content-type from absolute URL."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/zip"}
        resp.content = b"PK\x03\x04 fake zip"
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        data, ctype = client.fetch_from_url(
            "https://registry.example.com/v1/packages/o/r/versions/1.0.0/download"
        )
        assert ctype == "application/zip"
        assert data == b"PK\x03\x04 fake zip"

    def test_http_error_raises(self) -> None:
        """HTTP 404 from fetch_from_url raises RegistryError."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"title": "Not Found"}
        session.request.return_value = resp

        client = RegistryClient("https://registry.example.com", _bearer_auth(), session=session)
        with pytest.raises(RegistryError) as exc_info:
            client.fetch_from_url(
                "https://registry.example.com/v1/packages/o/r/versions/1.0.0/download"
            )
        assert exc_info.value.status == 404


# ---------------------------------------------------------------------------
# resolver.py
# ---------------------------------------------------------------------------


class TestSplitOwnerRepo:
    """Tests for _split_owner_repo() path parsing."""

    def test_two_segments(self) -> None:
        assert _split_owner_repo("owner/repo") == ("owner", "repo")

    def test_three_segments(self) -> None:
        """Three segments: first N-1 are owner, last is repo."""
        assert _split_owner_repo("group/subgroup/repo") == ("group/subgroup", "repo")

    def test_single_segment_raises(self) -> None:
        with pytest.raises(RegistryResolutionError, match="owner/repo"):
            _split_owner_repo("just-a-repo")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(RegistryResolutionError):
            _split_owner_repo("")


class TestRegistryPackageResolverPickVersion:
    """Tests for version selection logic inside RegistryPackageResolver."""

    def _make_resolver(self, base_url: str = "https://registry.example.com") -> Any:
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        return RegistryPackageResolver({"myregistry": base_url})

    def test_no_version_constraint_raises(self) -> None:
        """A dep with no reference raises RegistryResolutionError."""
        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference=None,
        )
        versions = [
            VersionEntry(version="1.0.0", digest="sha256:a", published_at="2024-01-01T00:00:00Z")
        ]
        resolver = self._make_resolver()
        with pytest.raises(RegistryResolutionError, match="no version constraint"):
            resolver._pick_version(dep_ref, versions)

    def test_non_semver_constraint_raises(self) -> None:
        """A non-semver reference raises RegistryResolutionError."""
        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="main",
        )
        versions = [
            VersionEntry(version="1.0.0", digest="sha256:a", published_at="2024-01-01T00:00:00Z")
        ]
        resolver = self._make_resolver()
        with pytest.raises(RegistryResolutionError, match="not a valid semver range"):
            resolver._pick_version(dep_ref, versions)

    def test_no_matching_version_raises(self) -> None:
        """When no version matches the range, RegistryResolutionError is raised."""
        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference=">=2.0.0",
        )
        versions = [
            VersionEntry(version="1.0.0", digest="sha256:a", published_at="2024-01-01T00:00:00Z"),
            VersionEntry(version="1.9.9", digest="sha256:b", published_at="2024-01-02T00:00:00Z"),
        ]
        resolver = self._make_resolver()
        with pytest.raises(RegistryResolutionError, match="no version of"):
            resolver._pick_version(dep_ref, versions)

    def test_best_version_selected(self) -> None:
        """pick_best selects the highest satisfying version."""
        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )
        versions = [
            VersionEntry(version="1.0.0", digest="sha256:a", published_at="2024-01-01T00:00:00Z"),
            VersionEntry(version="1.2.5", digest="sha256:b", published_at="2024-02-01T00:00:00Z"),
            VersionEntry(version="2.0.0", digest="sha256:c", published_at="2024-03-01T00:00:00Z"),
        ]
        resolver = self._make_resolver()
        chosen = resolver._pick_version(dep_ref, versions)
        assert chosen.version == "1.2.5"

    def test_missing_registry_raises(self) -> None:
        """Dep that references an unconfigured registry raises RegistryResolutionError."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        resolver = RegistryPackageResolver({"other": "https://other.com"})
        with pytest.raises(RegistryResolutionError, match="not configured"):
            resolver._resolve_registry_url("myregistry")

    def test_missing_registry_name_raises(self) -> None:
        """Dep with no registry_name raises RegistryResolutionError."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        resolver = RegistryPackageResolver({"myregistry": "https://registry.example.com"})
        with pytest.raises(RegistryResolutionError, match="missing registry_name"):
            resolver._resolve_registry_url(None)


class TestRaiseForHttp:
    """Tests for RegistryPackageResolver._raise_for_http() error routing."""

    def _make_resolver(self) -> Any:
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        return RegistryPackageResolver({"myregistry": "https://registry.example.com"})

    def _dep_ref(self) -> DependencyReference:
        return DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )

    def test_401_includes_remediation(self) -> None:
        """401 error includes the §6.2 remediation message."""
        resolver = self._make_resolver()
        exc = RegistryError("unauthorized", status=401, url="https://registry.example.com")
        with pytest.raises(RegistryResolutionError, match="no credentials"):
            resolver._raise_for_http(exc, self._dep_ref(), "https://registry.example.com")

    def test_403_includes_remediation(self) -> None:
        """403 error includes the §6.2 remediation message."""
        resolver = self._make_resolver()
        exc = RegistryError("forbidden", status=403, url="https://registry.example.com")
        with pytest.raises(RegistryResolutionError, match="no credentials"):
            resolver._raise_for_http(exc, self._dep_ref(), "https://registry.example.com")

    def test_404_mentions_package(self) -> None:
        """404 error message includes the package URL."""
        resolver = self._make_resolver()
        exc = RegistryError("not found", status=404, url="https://registry.example.com/v1/x")
        with pytest.raises(RegistryResolutionError, match="HTTP 404"):
            resolver._raise_for_http(exc, self._dep_ref(), "https://registry.example.com")

    def test_500_raises_generic(self) -> None:
        """5xx errors surface as plain RegistryResolutionError."""
        resolver = self._make_resolver()
        exc = RegistryError("server error", status=500, url="https://registry.example.com")
        with pytest.raises(RegistryResolutionError):
            resolver._raise_for_http(exc, self._dep_ref(), "https://registry.example.com")


# ---------------------------------------------------------------------------
# outdated.py
# ---------------------------------------------------------------------------


class TestCheckRegistryLockedDep:
    """Tests for check_registry_locked_dep() outdated comparisons."""

    def _make_locked(
        self,
        *,
        repo_url: str = "acme/tool",
        version: str | None = "1.0.0",
        source: str = "registry",
    ) -> Any:
        from apm_cli.deps.lockfile import LockedDependency

        return LockedDependency(
            repo_url=repo_url,
            resolved_commit=None,
            version=version,
            source=source,
        )

    def _make_ctx(
        self,
        *,
        range_str: str = "^1.0.0",
        versions: list[str] | None = None,
        registry_name: str = "myregistry",
    ) -> Any:
        from apm_cli.deps.registry.outdated import RegistryOutdatedContext

        versions = versions or ["1.0.0", "1.1.0", "1.2.0"]
        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name=registry_name,
            reference=range_str,
        )
        return RegistryOutdatedContext(
            manifest_index={"acme/tool": dep_ref},
            registries={"myregistry": "https://registry.example.com"},
            default_registry=None,
        ), versions

    def test_up_to_date(self) -> None:
        """Dep at latest in-range version is reported as up-to-date."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version="1.2.0")
        ctx, version_strings = self._make_ctx(range_str="^1.0.0", versions=["1.0.0", "1.2.0"])

        entries = [VersionEntry(v, f"sha256:{v}", "2024-01-01T00:00:00Z") for v in version_strings]

        def _fake_client(url: str, auth: Any) -> Any:
            client = MagicMock()
            client.list_versions.return_value = entries
            return client

        with patch("apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=True):
            row = check_registry_locked_dep(locked, ctx, client_factory=_fake_client)
        assert row.status == "up-to-date"
        assert row.current == "1.2.0"

    def test_outdated(self) -> None:
        """Dep below latest in-range version is reported as outdated."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version="1.0.0")
        ctx, version_strings = self._make_ctx(range_str="^1.0.0", versions=["1.0.0", "1.2.0"])

        entries = [VersionEntry(v, f"sha256:{v}", "2024-01-01T00:00:00Z") for v in version_strings]

        def _fake_client(url: str, auth: Any) -> Any:
            client = MagicMock()
            client.list_versions.return_value = entries
            return client

        with patch("apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=True):
            row = check_registry_locked_dep(locked, ctx, client_factory=_fake_client)
        assert row.status == "outdated"
        assert row.latest == "1.2.0"

    def test_none_context_returns_unknown(self) -> None:
        """None ctx produces an 'unknown' row without hitting the registry."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version="1.0.0")
        row = check_registry_locked_dep(locked, None)
        assert row.status == "unknown"

    def test_feature_disabled_returns_unknown(self) -> None:
        """When feature gate is off, row is 'unknown' with disabled note."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version="1.0.0")
        ctx, _ = self._make_ctx()
        with patch(
            "apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=False
        ):
            row = check_registry_locked_dep(locked, ctx)
        assert row.status == "unknown"
        assert "feature disabled" in row.source

    def test_registry_error_returns_unknown(self) -> None:
        """RegistryError during list_versions returns 'unknown' status."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version="1.0.0")
        ctx, _ = self._make_ctx()

        def _fail_client(url: str, auth: Any) -> Any:
            client = MagicMock()
            client.list_versions.side_effect = RegistryError("timeout")
            return client

        with patch("apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=True):
            row = check_registry_locked_dep(locked, ctx, client_factory=_fail_client)
        assert row.status == "unknown"

    def test_missing_locked_version_returns_unknown(self) -> None:
        """Dep with no locked version returns 'unknown' status."""
        from apm_cli.deps.registry.outdated import check_registry_locked_dep

        locked = self._make_locked(version=None)
        ctx, _ = self._make_ctx()
        with patch("apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=True):
            row = check_registry_locked_dep(locked, ctx)
        assert row.status == "unknown"

    def test_lockfile_only_dep_uses_highest_semver(self) -> None:
        """A dep present in lockfile but not manifest uses highest available version."""
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.deps.registry.outdated import (
            RegistryOutdatedContext,
            check_registry_locked_dep,
        )

        locked = LockedDependency(
            repo_url="acme/tool",
            resolved_commit=None,
            version="1.0.0",
            source="registry",
        )
        ctx = RegistryOutdatedContext(
            manifest_index={},  # not in manifest
            registries={"myregistry": "https://registry.example.com"},
            default_registry="myregistry",
        )
        entries = [
            VersionEntry("1.0.0", "sha256:a", "2024-01-01T00:00:00Z"),
            VersionEntry("2.5.0", "sha256:b", "2024-06-01T00:00:00Z"),
        ]

        def _fake_client(url: str, auth: Any) -> Any:
            c = MagicMock()
            c.list_versions.return_value = entries
            return c

        with patch("apm_cli.deps.registry.outdated.is_package_registry_enabled", return_value=True):
            row = check_registry_locked_dep(locked, ctx, client_factory=_fake_client)
        assert row.status == "outdated"
        assert row.latest == "2.5.0"


# ---------------------------------------------------------------------------
# extractor.py
# ---------------------------------------------------------------------------


class TestVerifySha256:
    """Tests for verify_sha256()."""

    def test_match_returns_hex(self) -> None:
        data = b"hello world"
        digest = hashlib.sha256(data).hexdigest()
        result = verify_sha256(data, digest)
        assert result == digest

    def test_match_with_sha256_prefix(self) -> None:
        data = b"hello world"
        digest = hashlib.sha256(data).hexdigest()
        result = verify_sha256(data, f"sha256:{digest}")
        assert result == digest

    def test_mismatch_raises(self) -> None:
        with pytest.raises(HashMismatchError):
            verify_sha256(b"hello", "sha256:" + "0" * 64)


class TestExtractArchive:
    """Tests for extract_archive() dispatcher (tar.gz and zip)."""

    def test_extract_tar_gz_by_content_type(self, tmp_path: Path) -> None:
        """extract_archive dispatches to tar extractor when content-type is gzip."""
        files = {"apm.yml": b"name: test-pkg\nversion: 1.0.0\n"}
        data = _make_tar_gz(files)
        digest = _sha256(data)

        extract_archive(data, digest, tmp_path, content_type="application/gzip")

        assert (tmp_path / "apm.yml").exists()

    def test_extract_zip_by_content_type(self, tmp_path: Path) -> None:
        """extract_archive dispatches to zip extractor when content-type is zip."""
        files = {"apm.yml": b"name: test-pkg\nversion: 1.0.0\n"}
        data = _make_zip(files)
        digest = _sha256(data)

        extract_archive(data, digest, tmp_path, content_type="application/zip")

        assert (tmp_path / "apm.yml").exists()

    def test_hash_mismatch_raises_before_extraction(self, tmp_path: Path) -> None:
        """Hash mismatch raises HashMismatchError before any files are written."""
        files = {"apm.yml": b"name: test\n"}
        data = _make_tar_gz(files)
        bad_digest = "sha256:" + "0" * 64

        with pytest.raises(HashMismatchError):
            extract_archive(data, bad_digest, tmp_path, content_type="application/gzip")

        # Nothing should have been extracted.
        assert not list(tmp_path.iterdir())

    def test_unknown_format_raises(self, tmp_path: Path) -> None:
        """Unknown content-type with no matching magic raises UnknownArchiveFormatError."""
        data = b"this is not a real archive at all"
        digest = _sha256(data)

        with pytest.raises(UnknownArchiveFormatError):
            extract_archive(data, digest, tmp_path, content_type="application/octet-stream")

    def test_extract_infers_tar_by_magic_bytes(self, tmp_path: Path) -> None:
        """Without content-type, tar.gz is inferred from \\x1f\\x8b magic bytes."""
        files = {"apm.yml": b"name: test-pkg\n"}
        data = _make_tar_gz(files)
        assert data[:2] == b"\x1f\x8b"
        digest = _sha256(data)

        extract_archive(data, digest, tmp_path, content_type=None)

        assert (tmp_path / "apm.yml").exists()

    def test_extract_infers_zip_by_magic_bytes(self, tmp_path: Path) -> None:
        """Without content-type, zip is inferred from PK magic bytes."""
        files = {"apm.yml": b"name: test-pkg\n"}
        data = _make_zip(files)
        assert data[:4] == b"PK\x03\x04"
        digest = _sha256(data)

        extract_archive(data, digest, tmp_path, content_type=None)

        assert (tmp_path / "apm.yml").exists()


# ---------------------------------------------------------------------------
# revision_pins.py
# ---------------------------------------------------------------------------


class TestIsFullRevisionPin:
    """Tests for is_full_revision_pin()."""

    def test_full_40_char_sha(self) -> None:
        assert is_full_revision_pin(_FAKE_SHA) is True

    def test_short_sha_is_false(self) -> None:
        assert is_full_revision_pin("abc1234") is False

    def test_branch_name_is_false(self) -> None:
        assert is_full_revision_pin("main") is False

    def test_semver_tag_is_false(self) -> None:
        assert is_full_revision_pin("v1.2.3") is False

    def test_none_is_false(self) -> None:
        assert is_full_revision_pin(None) is False


class TestAbbreviateSha:
    """Tests for abbreviate_sha()."""

    def test_full_sha_returns_first_8(self) -> None:
        result = abbreviate_sha(_FAKE_SHA)
        assert result == "a" * 8

    def test_none_returns_empty_string(self) -> None:
        assert abbreviate_sha(None) == ""


class TestFindLatestAnnotatedTag:
    """Tests for find_latest_annotated_tag()."""

    def _ref(self, name: str, commit: str, *, annotated: bool = True) -> RemoteRef:
        return RemoteRef(
            name=name, ref_type=GitReferenceType.TAG, commit_sha=commit, annotated=annotated
        )

    def test_finds_highest_version(self) -> None:
        """Returns highest semver annotated tag."""
        refs = [
            self._ref("my-tool-v1.0.0", "a" * 40),
            self._ref("my-tool-v1.2.0", "b" * 40),
            self._ref("my-tool-v1.1.0", "c" * 40),
        ]
        candidate = find_latest_annotated_tag(refs, package_name="my-tool")
        assert candidate.tag == "my-tool-v1.2.0"
        assert candidate.commit_sha == "b" * 40

    def test_ignores_lightweight_tags(self) -> None:
        """Lightweight tags (annotated=False) are excluded."""
        refs = [
            self._ref("my-tool-v1.0.0", "a" * 40, annotated=False),
            self._ref("my-tool-v1.2.0", "b" * 40, annotated=True),
        ]
        candidate = find_latest_annotated_tag(refs, package_name="my-tool")
        assert candidate.tag == "my-tool-v1.2.0"

    def test_no_candidates_raises(self) -> None:
        """Empty or non-tag refs raise RevisionPinResolutionError."""
        from apm_cli.deps.revision_pins import RevisionPinResolutionError

        refs = [
            RemoteRef(
                name="main",
                ref_type=GitReferenceType.BRANCH,
                commit_sha="a" * 40,
                annotated=False,
            )
        ]
        with pytest.raises(RevisionPinResolutionError, match="No annotated tag"):
            find_latest_annotated_tag(refs, package_name="my-tool")


class TestResolveRevisionPinUpdates:
    """Tests for resolve_revision_pin_updates()."""

    def _dep(self, sha: str = _FAKE_SHA) -> DependencyReference:
        return DependencyReference(
            repo_url="owner/my-tool",
            source=None,
            reference=sha,
        )

    def test_update_found(self) -> None:
        """Returns a RevisionPinUpdate when remote tag SHA differs from pinned SHA."""
        dep = self._dep(_FAKE_SHA)
        new_sha = _FAKE_SHA2

        refs = [
            RemoteRef(
                name="my-tool-v2.0.0",
                ref_type=GitReferenceType.TAG,
                commit_sha=new_sha,
                annotated=True,
            )
        ]

        downloader = MagicMock()
        downloader.list_remote_tag_refs.return_value = refs

        updates = resolve_revision_pin_updates([dep], downloader)
        assert len(updates) == 1
        assert updates[0].new_sha == new_sha.lower()
        assert updates[0].tag == "my-tool-v2.0.0"

    def test_already_up_to_date_returns_empty(self) -> None:
        """No update when the remote SHA matches the pinned SHA."""
        dep = self._dep(_FAKE_SHA)
        refs = [
            RemoteRef(
                name="my-tool-v1.0.0",
                ref_type=GitReferenceType.TAG,
                commit_sha=_FAKE_SHA,
                annotated=True,
            )
        ]

        downloader = MagicMock()
        downloader.list_remote_tag_refs.return_value = refs

        updates = resolve_revision_pin_updates([dep], downloader)
        assert updates == []

    def test_registry_dep_skipped(self) -> None:
        """Registry-sourced deps are not eligible for revision-pin updates."""
        dep = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference=_FAKE_SHA,
        )
        downloader = MagicMock()
        updates = resolve_revision_pin_updates([dep], downloader)
        assert updates == []
        downloader.list_remote_tag_refs.assert_not_called()

    def test_local_dep_skipped(self) -> None:
        """Local deps are not eligible for revision-pin updates."""
        dep = DependencyReference(
            repo_url="./local-tool",
            source=None,
            reference=_FAKE_SHA,
            is_local=True,
            local_path="./local-tool",
        )
        downloader = MagicMock()
        updates = resolve_revision_pin_updates([dep], downloader)
        assert updates == []


class TestApplyRevisionPinUpdates:
    """Tests for apply_revision_pin_updates()."""

    def test_rewrites_sha_in_manifest(self, tmp_path: Path) -> None:
        """apply_revision_pin_updates replaces the old SHA in apm.yml."""
        manifest = tmp_path / "apm.yml"
        old_sha = "a" * 40
        new_sha = "b" * 40
        manifest.write_text(
            f"name: my-project\ndependencies:\n  - owner/my-tool#{old_sha}\n",
            encoding="utf-8",
        )

        update = RevisionPinUpdate(
            dep_key="owner/my-tool",
            old_sha=old_sha,
            new_sha=new_sha,
            tag="v2.0.0",
            display_name="owner/my-tool",
        )
        apply_revision_pin_updates(manifest, [update])

        content = manifest.read_text(encoding="utf-8")
        assert new_sha in content
        assert old_sha not in content
        assert "# v2.0.0" in content

    def test_wrong_filename_raises(self, tmp_path: Path) -> None:
        """apply_revision_pin_updates rejects files not named apm.yml."""
        from apm_cli.deps.revision_pins import RevisionPinResolutionError

        manifest = tmp_path / "package.json"
        manifest.write_text("{}", encoding="utf-8")
        update = RevisionPinUpdate(
            dep_key="owner/tool",
            old_sha="a" * 40,
            new_sha="b" * 40,
            tag="v1.0.0",
            display_name="owner/tool",
        )
        with pytest.raises(RevisionPinResolutionError, match=r"apm\.yml manifest"):
            apply_revision_pin_updates(manifest, [update])

    def test_no_updates_does_not_modify_file(self, tmp_path: Path) -> None:
        """Empty update list leaves the manifest unmodified."""
        manifest = tmp_path / "apm.yml"
        original = "name: my-project\n"
        manifest.write_text(original, encoding="utf-8")
        apply_revision_pin_updates(manifest, [])
        assert manifest.read_text(encoding="utf-8") == original


class TestRenderRevisionPinUpdatePlan:
    """Tests for render_revision_pin_update_plan()."""

    def test_single_update_rendered(self) -> None:
        """Plan contains the dep name, old/new SHA abbreviations, and tag."""
        update = RevisionPinUpdate(
            dep_key="owner/tool",
            old_sha="a" * 40,
            new_sha="b" * 40,
            tag="v2.0.0",
            display_name="owner/tool",
        )
        plan = render_revision_pin_update_plan([update])
        assert "owner/tool" in plan
        assert "v2.0.0" in plan
        assert "aaaaaaaa" in plan  # abbreviated old SHA
        assert "bbbbbbbb" in plan  # abbreviated new SHA

    def test_empty_updates_returns_empty_string(self) -> None:
        """No updates produces an empty string."""
        assert render_revision_pin_update_plan([]) == ""

    def test_plural_count_label(self) -> None:
        """Plan uses plural 'updates' for multiple entries."""
        updates = [
            RevisionPinUpdate("owner/a", "a" * 40, "b" * 40, "v1.0.0", "owner/a"),
            RevisionPinUpdate("owner/b", "c" * 40, "d" * 40, "v2.0.0", "owner/b"),
        ]
        plan = render_revision_pin_update_plan(updates)
        assert "2 revision pin updates" in plan


# ---------------------------------------------------------------------------
# outdated.py — additional coverage for _highest_semver / _semver_lt /
# load_registry_outdated_context / _add_registry_manifest_deps
# ---------------------------------------------------------------------------


class TestHighestSemver:
    """Tests for _highest_semver()."""

    def test_picks_highest(self) -> None:
        from apm_cli.deps.registry.outdated import _highest_semver

        result = _highest_semver(["1.0.0", "2.1.0", "1.5.0"])
        assert result == "2.1.0"

    def test_empty_returns_none(self) -> None:
        from apm_cli.deps.registry.outdated import _highest_semver

        assert _highest_semver([]) is None

    def test_non_semver_entries_ignored(self) -> None:
        from apm_cli.deps.registry.outdated import _highest_semver

        result = _highest_semver(["not-semver", "1.0.0", "also-bad"])
        assert result == "1.0.0"


class TestSemverLt:
    """Tests for _semver_lt()."""

    def test_older_is_less_than_newer(self) -> None:
        from apm_cli.deps.registry.outdated import _semver_lt

        assert _semver_lt("1.0.0", "1.1.0") is True

    def test_same_version_is_not_less_than(self) -> None:
        from apm_cli.deps.registry.outdated import _semver_lt

        assert _semver_lt("1.0.0", "1.0.0") is False

    def test_newer_is_not_less_than_older(self) -> None:
        from apm_cli.deps.registry.outdated import _semver_lt

        assert _semver_lt("2.0.0", "1.0.0") is False


class TestLoadRegistryOutdatedContext:
    """Tests for load_registry_outdated_context()."""

    def _write_apm_yml(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_returns_context_with_no_apm_yml(self, tmp_path: Path) -> None:
        """load_registry_outdated_context works when no apm.yml is present."""
        from apm_cli.deps.registry.outdated import load_registry_outdated_context

        with (
            patch(
                "apm_cli.deps.registry.config_loader._load_config_json_registries", return_value={}
            ),
            patch("apm_cli.config.get_config_json_default_registry", return_value=None),
        ):
            ctx = load_registry_outdated_context(tmp_path)
        assert ctx.manifest_index == {}

    def test_returns_context_with_registry_deps(self, tmp_path: Path) -> None:
        """load_registry_outdated_context picks up registry config from apm.yml."""
        from apm_cli.deps.registry.outdated import load_registry_outdated_context

        apm_yml = tmp_path / "apm.yml"
        self._write_apm_yml(
            apm_yml,
            "name: my-project\nversion: 1.0.0\n"
            "registries:\n  corp:\n    url: https://registry.corp.com\n"
            "default_registry: corp\n",
        )
        with (
            patch(
                "apm_cli.deps.registry.config_loader._load_config_json_registries", return_value={}
            ),
            patch("apm_cli.config.get_config_json_default_registry", return_value=None),
            patch(
                "apm_cli.deps.registry.feature_gate.is_package_registry_enabled", return_value=True
            ),
        ):
            ctx = load_registry_outdated_context(tmp_path)
        # The context should have parsed the registries block
        assert "corp" in ctx.registries


class TestAddRegistryManifestDeps:
    """Tests for _add_registry_manifest_deps()."""

    def test_missing_file_is_a_noop(self, tmp_path: Path) -> None:
        """A missing apm.yml does not raise; manifest_index is unchanged."""
        from apm_cli.deps.registry.outdated import _add_registry_manifest_deps

        manifest_index: dict = {}
        _add_registry_manifest_deps(tmp_path / "nonexistent.yml", manifest_index, None)
        assert manifest_index == {}

    def test_invalid_yaml_is_a_noop(self, tmp_path: Path) -> None:
        """A broken apm.yml does not crash; manifest_index unchanged."""
        from apm_cli.deps.registry.outdated import _add_registry_manifest_deps

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(":: invalid yaml ::", encoding="utf-8")
        manifest_index: dict = {}
        _add_registry_manifest_deps(apm_yml, manifest_index, None)
        assert manifest_index == {}


# ---------------------------------------------------------------------------
# resolver.py — additional coverage for _clear_install_target and
# download_package / download_from_lockfile (mocked end-to-end)
# ---------------------------------------------------------------------------


class TestClearInstallTarget:
    """Tests for _clear_install_target()."""

    def test_creates_empty_directory(self, tmp_path: Path) -> None:
        """_clear_install_target creates target directory when absent."""
        from apm_cli.deps.registry.resolver import _clear_install_target

        target = tmp_path / "pkg" / "tool"
        _clear_install_target(target)
        assert target.is_dir()

    def test_removes_existing_contents(self, tmp_path: Path) -> None:
        """_clear_install_target removes files from an existing directory."""
        from apm_cli.deps.registry.resolver import _clear_install_target

        target = tmp_path / "tool"
        target.mkdir()
        (target / "old_file.txt").write_text("stale", encoding="utf-8")
        (target / "old_dir").mkdir()

        _clear_install_target(target)

        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_already_empty_is_a_noop(self, tmp_path: Path) -> None:
        """_clear_install_target is a no-op for an already-empty directory."""
        from apm_cli.deps.registry.resolver import _clear_install_target

        target = tmp_path / "empty_dir"
        target.mkdir()
        _clear_install_target(target)
        assert target.is_dir()
        assert list(target.iterdir()) == []


class TestRegistryPackageResolverDownloadPackage:
    """Smoke tests for RegistryPackageResolver.download_package() (mocked)."""

    def _make_fake_archive(self) -> tuple[bytes, str]:
        """Return a tar.gz with a minimal apm.yml inside."""
        files = {"apm.yml": b"name: acme-tool\nversion: 1.0.0\nauthor: Acme\n"}
        data = _make_tar_gz(files)
        return data, "application/gzip"

    def test_download_package_success(self, tmp_path: Path) -> None:
        """download_package fetches, extracts, and returns PackageInfo."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.validation import ValidationResult

        archive_data, archive_ctype = self._make_fake_archive()
        archive_digest = _sha256(archive_data)

        versions = [
            VersionEntry(
                version="1.0.0",
                digest=f"sha256:{archive_digest}",
                published_at="2024-01-01T00:00:00Z",
            )
        ]

        fake_client = MagicMock()
        fake_client.list_versions.return_value = versions
        fake_client.download_archive.return_value = (archive_data, archive_ctype)
        fake_client.archive_url.return_value = (
            "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0/download"
        )

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )

        # Build a valid ValidationResult
        pkg = MagicMock(spec=APMPackage)
        pkg.source = None

        val_result = ValidationResult()
        val_result.package = pkg
        val_result.package_type = None

        def _fake_factory(url: str, auth: Any) -> Any:
            return fake_client

        resolver = RegistryPackageResolver(
            {"myregistry": "https://registry.example.com"},
            client_factory=_fake_factory,
        )

        target_path = tmp_path / "acme" / "tool"

        with (
            patch("apm_cli.deps.registry.resolver.extract_archive", return_value=archive_digest),
            patch("apm_cli.deps.registry.resolver.validate_apm_package", return_value=val_result),
            patch("apm_cli.config.get_registry_config", return_value=None),
        ):
            pkg_info = resolver.download_package(dep_ref, target_path)

        assert pkg_info is not None
        # Resolution metadata recorded
        assert dep_ref.get_unique_key() in resolver.last_resolutions

    def test_download_package_no_versions_raises(self, tmp_path: Path) -> None:
        """download_package raises RegistryResolutionError when no versions exist."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        fake_client = MagicMock()
        fake_client.list_versions.return_value = []

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )

        resolver = RegistryPackageResolver(
            {"myregistry": "https://registry.example.com"},
            client_factory=lambda url, auth: fake_client,
        )
        with (
            patch("apm_cli.config.get_registry_config", return_value=None),
            pytest.raises(RegistryResolutionError, match="no versions"),
        ):
            resolver.download_package(dep_ref, tmp_path / "tool")

    def test_download_package_non_registry_source_raises(self, tmp_path: Path) -> None:
        """download_package rejects non-registry sources."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        dep_ref = DependencyReference(
            repo_url="owner/repo",
            source=None,  # git source
            reference="main",
        )
        resolver = RegistryPackageResolver({"myregistry": "https://registry.example.com"})
        with pytest.raises(RegistryResolutionError, match="non-registry"):
            resolver.download_package(dep_ref, tmp_path / "tool")


class TestRegistryPackageResolverDownloadFromLockfile:
    """Tests for RegistryPackageResolver.download_from_lockfile()."""

    def test_download_from_lockfile_success(self, tmp_path: Path) -> None:
        """download_from_lockfile verifies hash and returns PackageInfo."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.validation import ValidationResult

        archive_data = b"\x1f\x8b fake_gz"
        archive_digest = _sha256(archive_data)
        resolved_url = "https://registry.example.com/v1/packages/acme/tool/versions/1.0.0/download"

        fake_client = MagicMock()
        fake_client.fetch_from_url.return_value = (archive_data, "application/gzip")

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )

        pkg = MagicMock(spec=APMPackage)
        pkg.source = None

        val_result = ValidationResult()
        val_result.package = pkg
        val_result.package_type = None

        resolver = RegistryPackageResolver(
            {"myregistry": "https://registry.example.com"},
            client_factory=lambda url, auth: fake_client,
        )

        with (
            patch("apm_cli.deps.registry.resolver.extract_archive", return_value=archive_digest),
            patch("apm_cli.deps.registry.resolver.validate_apm_package", return_value=val_result),
            patch("apm_cli.deps.registry.auth.resolve_for_url") as mock_rfu,
        ):
            mock_rfu.return_value = _anon_auth()
            pkg_info = resolver.download_from_lockfile(
                dep_ref,
                tmp_path / "tool",
                resolved_url=resolved_url,
                resolved_hash=f"sha256:{archive_digest}",
                version="1.0.0",
            )

        assert pkg_info is not None

    def test_download_from_lockfile_401_raises_with_remediation(self, tmp_path: Path) -> None:
        """401 during lockfile replay surfaces the §6.2 remediation message."""
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        fake_client = MagicMock()
        fake_client.fetch_from_url.side_effect = RegistryError(
            "unauthorized", status=401, url="https://registry.example.com/..."
        )

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="myregistry",
            reference="^1.0.0",
        )

        resolver = RegistryPackageResolver(
            {"myregistry": "https://registry.example.com"},
            client_factory=lambda url, auth: fake_client,
        )

        with (
            patch("apm_cli.deps.registry.auth.resolve_for_url", return_value=_anon_auth()),
            pytest.raises(RegistryResolutionError, match="no credentials"),
        ):
            resolver.download_from_lockfile(
                dep_ref,
                tmp_path / "tool",
                resolved_url="https://registry.example.com/v1/packages/acme/tool/versions/1.0.0/download",
                resolved_hash="sha256:abc",
                version="1.0.0",
            )


# ---------------------------------------------------------------------------
# auth.py — dependency_ref_with_registry_name_from_lockfile
# ---------------------------------------------------------------------------


class TestDependencyRefWithRegistryNameFromLockfile:
    """Tests for dependency_ref_with_registry_name_from_lockfile()."""

    def test_sets_registry_name_when_url_matches(self) -> None:
        """Registry name is populated from lockfile URL prefix match."""
        from apm_cli.deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name=None,
        )
        locked = MagicMock()
        locked.resolved_url = (
            "https://registry.corp.com/apm/v1/packages/acme/tool/versions/1.0.0/download"
        )

        registries = {"corp": "https://registry.corp.com/apm"}
        result = dependency_ref_with_registry_name_from_lockfile(
            dep_ref, registries, locked_dep=locked
        )
        assert result.registry_name == "corp"

    def test_no_op_when_registry_name_already_set(self) -> None:
        """dep_ref with existing registry_name is returned unchanged."""
        from apm_cli.deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name="existing",
        )
        result = dependency_ref_with_registry_name_from_lockfile(dep_ref, {})
        assert result.registry_name == "existing"

    def test_no_op_for_git_source(self) -> None:
        """Non-registry source dep is returned unchanged."""
        from apm_cli.deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

        dep_ref = DependencyReference(
            repo_url="owner/repo",
            source=None,
        )
        result = dependency_ref_with_registry_name_from_lockfile(dep_ref, {"corp": "https://..."})
        assert result.registry_name is None

    def test_no_op_when_no_matching_registry(self) -> None:
        """No match in registries map returns dep unchanged (no registry_name)."""
        from apm_cli.deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

        dep_ref = DependencyReference(
            repo_url="acme/tool",
            source="registry",
            registry_name=None,
        )
        locked = MagicMock()
        locked.resolved_url = "https://other.host.com/v1/packages/acme/tool/versions/1.0.0/download"

        registries = {"corp": "https://registry.corp.com/apm"}
        result = dependency_ref_with_registry_name_from_lockfile(
            dep_ref, registries, locked_dep=locked
        )
        assert result.registry_name is None
