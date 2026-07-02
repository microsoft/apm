"""Tests for the registry HTTP client.

Uses mocked ``requests.Session`` to avoid network access. Confirms:
- URL construction (path joining, percent-encoding)
- Auth header forwarding
- JSON parsing of /versions responses
- Error mapping (RFC 7807 problem detail extraction; transport errors)
"""

from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

import pytest
import requests

from apm_cli.deps.registry.auth import RegistryAuthContext
from apm_cli.deps.registry.client import (
    RegistryClient,
    RegistryError,
    VersionEntry,
    _quote,
)


def _make_response(
    *,
    status: int = 200,
    json_body=None,
    body: bytes = b"",
    content_type: str = "application/json",
):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.url = "<test>"
    resp.headers = {"Content-Type": content_type}
    # The client reads bodies via a capped iter_content reader (stream-safe),
    # not response.json(). Feed the effective body bytes through iter_content
    # so the fake matches the real streamed contract; keep .content too for the
    # binary download path. A fresh iterator per call (the error + success
    # branches may each read once).
    raw = _json.dumps(json_body).encode("utf-8") if json_body is not None else body
    resp.content = raw
    resp.iter_content = lambda chunk_size=None: iter([raw] if raw else [])
    return resp


def _make_session(response):
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response
    return session


class TestUrlConstruction:
    def test_versions_url(self):
        session = _make_session(_make_response(json_body={"package": "a/b", "versions": []}))
        client = RegistryClient(
            "https://r.example.com/apm/",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        client.list_versions("acme", "web-skills")
        call = session.request.call_args
        assert call.args == ("GET",)
        assert call.kwargs["url"] == (
            "https://r.example.com/apm/v1/packages/acme/web-skills/versions"
        ), call.kwargs

    def test_archive_url_helper(self):
        client = RegistryClient(
            "https://r.example.com/apm",
            RegistryAuthContext(registry_name="x", token=None),
            session=MagicMock(spec=requests.Session),
        )
        url = client.archive_url("acme", "web-skills", "1.2.0")
        assert url == (
            "https://r.example.com/apm/v1/packages/acme/web-skills/versions/1.2.0/download"
        )

    def test_strips_trailing_slash_from_base(self):
        client = RegistryClient(
            "https://r.example.com/apm///",
            RegistryAuthContext(registry_name="x", token=None),
            session=MagicMock(spec=requests.Session),
        )
        assert client.base_url == "https://r.example.com/apm"


class TestAuth:
    def test_anonymous_omits_authorization(self):
        session = _make_session(_make_response(json_body={"versions": []}))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        client.list_versions("a", "b")
        headers = session.request.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_token_sets_bearer_header(self):
        session = _make_session(_make_response(json_body={"versions": []}))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token="tok-1"),
            session=session,
        )
        client.list_versions("a", "b")
        headers = session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok-1"


class TestListVersions:
    def test_parses_versions(self):
        session = _make_session(
            _make_response(
                json_body={
                    "package": "acme/web-skills",
                    "versions": [
                        {
                            "version": "1.2.0",
                            "digest": "sha256:abc",
                            "published_at": "2026-03-01T12:00:00Z",
                        },
                        {
                            "version": "1.1.0",
                            "digest": "sha256:def",
                            "published_at": "2026-02-01T08:00:00Z",
                        },
                    ],
                }
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        result = client.list_versions("acme", "web-skills")
        assert result == [
            VersionEntry("1.2.0", "sha256:abc", "2026-03-01T12:00:00Z"),
            VersionEntry("1.1.0", "sha256:def", "2026-02-01T08:00:00Z"),
        ]

    def test_camel_case_published_at_is_rejected(self):
        # The spec is strict (snake_case throughout). A server emitting
        # ``publishedAt`` (camelCase) is non-conformant; the client MUST NOT
        # silently accept it — that would mask spec drift. Because
        # ``published_at`` is now required, the absent snake_case field causes
        # a RegistryError even when the camelCase variant is present.
        session = _make_session(
            _make_response(
                json_body={
                    "package": "acme/foo",
                    "versions": [
                        {
                            "version": "1.0.0",
                            "digest": "sha256:abc",
                            "publishedAt": "2026-04-26T14:00:00Z",
                        }
                    ],
                }
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError, match="missing 'published_at'"):
            client.list_versions("acme", "foo")

    def test_missing_versions_array_raises(self):
        session = _make_session(_make_response(json_body={"package": "x"}))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError, match="missing 'versions' array"):
            client.list_versions("a", "b")

    def test_malformed_entry_raises(self):
        session = _make_session(_make_response(json_body={"versions": [{"version": "1.0.0"}]}))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError, match="missing 'digest'"):
            client.list_versions("a", "b")


class TestDownloadArchive:
    def test_returns_body_and_gzip_content_type(self):
        session = _make_session(
            _make_response(body=b"\x1f\x8b...", content_type="application/gzip")
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        body, ctype = client.download_archive("acme", "web-skills", "1.2.0")
        assert body == b"\x1f\x8b..."
        assert ctype == "application/gzip"

    def test_returns_body_and_zip_content_type(self):
        session = _make_session(
            _make_response(body=b"PK\x03\x04...", content_type="application/zip")
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        body, ctype = client.download_archive("acme", "skill", "1.0.0")
        assert body.startswith(b"PK")
        assert ctype == "application/zip"

    def test_strips_charset_param_from_content_type(self):
        session = _make_session(
            _make_response(
                body=b"\x1f\x8b...",
                content_type="application/gzip; charset=utf-8",
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        _, ctype = client.download_archive("a", "b", "1.0.0")
        assert ctype == "application/gzip"

    def test_url_path_is_download_not_tarball(self):
        session = _make_session(_make_response(body=b"x", content_type="application/gzip"))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        client.download_archive("acme", "web", "1.0.0")
        url = session.request.call_args.kwargs["url"]
        assert url.endswith("/versions/1.0.0/download")

    def test_404_raises_with_status(self):
        session = _make_session(
            _make_response(
                status=404,
                json_body={"title": "Not Found", "detail": "no such version"},
                content_type="application/problem+json",
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError) as excinfo:
            client.download_archive("a", "b", "9.9.9")
        assert excinfo.value.status == 404
        assert "Not Found" in str(excinfo.value)


class TestFetchFromUrl:
    """Tests for ``fetch_from_url`` — the lockfile-replay absolute-URL fetch."""

    def test_happy_path_returns_bytes_and_content_type(self):
        body = b"\x1f\x8b..."
        session = _make_session(_make_response(body=body, content_type="application/gzip"))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token="tok"),
            session=session,
        )
        got_body, got_ct = client.fetch_from_url(
            "https://r.example.com/v1/packages/a/b/versions/1.2.3/download"
        )
        assert got_body == body
        assert got_ct == "application/gzip"

    def test_passes_auth_header(self):
        session = _make_session(_make_response(body=b"x", content_type="application/gzip"))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token="mytoken"),
            session=session,
        )
        url = "https://r.example.com/v1/packages/a/b/versions/1.0.0/download"
        client.fetch_from_url(url)
        call = session.request.call_args
        assert call.kwargs["url"] == url
        assert call.kwargs["headers"]["Authorization"] == "Bearer mytoken"

    def test_uses_absolute_url_not_base_url(self):
        """fetch_from_url must use the passed URL exactly, not prepend base_url."""
        session = _make_session(_make_response(body=b"x", content_type="application/gzip"))
        client = RegistryClient(
            "https://r.example.com/apm",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        full_url = "https://other-host.example.com/packages/a/b/1.0/download"
        client.fetch_from_url(full_url)
        assert session.request.call_args.kwargs["url"] == full_url

    def test_404_raises_registry_error_with_status(self):
        session = _make_session(
            _make_response(
                status=404,
                json_body={"title": "Not Found", "detail": "gone"},
                content_type="application/problem+json",
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError) as excinfo:
            client.fetch_from_url("https://r.example.com/v1/packages/a/b/1.0/download")
        assert excinfo.value.status == 404

    def test_transport_error_wraps_to_registry_error(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("no route")
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError, match="transport error"):
            client.fetch_from_url("https://r.example.com/v1/packages/a/b/1.0/download")

    def test_strips_charset_from_content_type(self):
        session = _make_session(
            _make_response(body=b"x", content_type="application/gzip; charset=utf-8")
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        _, ct = client.fetch_from_url("https://r.example.com/v1/packages/a/b/1.0/download")
        assert ct == "application/gzip"


class TestPublishVersion:
    """publish_version must upload with Content-Type: application/zip."""

    def _client(self, session):
        return RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )

    def test_sends_application_zip_content_type(self):
        session = _make_session(
            _make_response(
                status=201,
                json_body={"package": "acme/skill", "version": "1.0.0", "digest": "sha256:abc"},
            )
        )
        self._client(session).publish_version("acme", "skill", "1.0.0", b"PK\x03\x04fake")
        _, kwargs = session.request.call_args
        assert kwargs["headers"]["Content-Type"] == "application/zip"

    def test_does_not_send_application_gzip(self):
        session = _make_session(
            _make_response(
                status=201,
                json_body={"package": "acme/skill", "version": "1.0.0", "digest": "sha256:abc"},
            )
        )
        self._client(session).publish_version("acme", "skill", "1.0.0", b"PK\x03\x04fake")
        _, kwargs = session.request.call_args
        assert kwargs["headers"]["Content-Type"] != "application/gzip"

    def test_409_raises_registry_error(self):
        session = _make_session(
            _make_response(
                status=409,
                json_body={"title": "Conflict", "detail": "version already exists"},
            )
        )
        with pytest.raises(RegistryError) as excinfo:
            self._client(session).publish_version("acme", "skill", "1.0.0", b"PK\x03\x04fake")
        assert excinfo.value.status == 409
        assert "already exists" in str(excinfo.value)

    def test_empty_body_201_returns_publish_result_with_computed_digest(self):
        """JFrog and some other registries return 201 with no body."""
        import hashlib

        archive = b"PK\x03\x04fake"
        expected = f"sha256:{hashlib.sha256(archive).hexdigest()}"
        session = _make_session(_make_response(status=201, body=b"", content_type=""))
        result = self._client(session).publish_version("acme", "skill", "1.0.0", archive)
        assert result.digest == expected


class TestErrorMapping:
    def test_transport_error_wraps(self):
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("dns failed")
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError, match="transport error"):
            client.list_versions("a", "b")

    def test_401_includes_problem_detail(self):
        session = _make_session(
            _make_response(
                status=401,
                json_body={"title": "Unauthorized", "detail": "missing token"},
            )
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        with pytest.raises(RegistryError) as excinfo:
            client.list_versions("a", "b")
        assert excinfo.value.status == 401
        assert "missing token" in str(excinfo.value)


class TestQuote:
    """_quote must treat ``_`` as an RFC 3986 unreserved character."""

    def test_underscore_is_not_encoded(self):
        # Regression: previously safe=".-" caused '_' → '%5F'
        assert _quote("my_org") == "my_org"

    def test_hyphen_is_not_encoded(self):
        assert _quote("my-org") == "my-org"

    def test_dot_is_not_encoded(self):
        assert _quote("my.org") == "my.org"

    def test_slash_is_encoded(self):
        assert _quote("a/b") == "a%2Fb"

    def test_space_is_encoded(self):
        assert _quote("a b") == "a%20b"

    def test_mixed_underscore_hyphen_dot(self):
        assert _quote("my_org.corp-main") == "my_org.corp-main"
