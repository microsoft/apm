"""Tests for the registry HTTP client.

Uses mocked ``requests.Session`` to avoid network access. Confirms:
- URL construction (path joining, percent-encoding)
- Auth header forwarding
- JSON parsing of /versions and /search responses
- Error mapping (RFC 7807 problem detail extraction; transport errors)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from apm_cli.deps.registry.auth import RegistryAuthContext
from apm_cli.deps.registry.client import (
    RegistryClient,
    RegistryError,
    SearchResult,
    VersionEntry,
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
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.content = body
    return resp


def _make_session(response):
    session = MagicMock(spec=requests.Session)
    session.request.return_value = response
    return session


class TestUrlConstruction:
    def test_versions_url(self):
        session = _make_session(
            _make_response(json_body={"package": "a/b", "versions": []})
        )
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
            "https://r.example.com/apm/v1/packages/acme/web-skills"
            "/versions/1.2.0/download"
        )

    def test_legacy_tarball_url_alias(self):
        # tarball_url is the deprecated alias kept for in-flight call sites
        # during the rename. It MUST return the same URL as archive_url.
        client = RegistryClient(
            "https://r.example.com/apm",
            RegistryAuthContext(registry_name="x", token=None),
            session=MagicMock(spec=requests.Session),
        )
        assert client.tarball_url("a", "b", "1.0.0") == client.archive_url("a", "b", "1.0.0")

    def test_strips_trailing_slash_from_base(self):
        client = RegistryClient(
            "https://r.example.com/apm///",
            RegistryAuthContext(registry_name="x", token=None),
            session=MagicMock(spec=requests.Session),
        )
        assert client.base_url == "https://r.example.com/apm"


class TestAuth:
    def test_anonymous_omits_authorization(self):
        session = _make_session(
            _make_response(json_body={"versions": []})
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        client.list_versions("a", "b")
        headers = session.request.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_token_sets_bearer_header(self):
        session = _make_session(
            _make_response(json_body={"versions": []})
        )
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
                        {"version": "1.1.0", "digest": "sha256:def"},
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
            VersionEntry("1.1.0", "sha256:def", None),
        ]

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
        session = _make_session(
            _make_response(json_body={"versions": [{"version": "1.0.0"}]})
        )
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
        session = _make_session(
            _make_response(body=b"x", content_type="application/gzip")
        )
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

    def test_legacy_download_tarball_alias_returns_just_bytes(self):
        # Backward-compat alias: returns only the bytes (no content type).
        session = _make_session(
            _make_response(body=b"x", content_type="application/gzip")
        )
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        body = client.download_tarball("a", "b", "1.0.0")
        assert body == b"x"


class TestSearch:
    def test_parses_results(self):
        session = _make_session(
            _make_response(
                json_body={
                    "query": "x",
                    "total": 1,
                    "results": [
                        {
                            "id": "acme/web-skills",
                            "latest_version": "1.2.0",
                            "description": "d",
                            "author": "a",
                            "tags": ["security"],
                            "type": "skill",
                            "score": 0.9,
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
        results = client.search("x", limit=10)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, SearchResult)
        assert r.id == "acme/web-skills"
        assert r.tags == ["security"]
        # Verify query params went on the URL
        call_url = session.request.call_args.kwargs["url"]
        assert "q=x" in call_url and "limit=10" in call_url

    def test_empty_results_returns_empty_list(self):
        session = _make_session(_make_response(json_body={"results": []}))
        client = RegistryClient(
            "https://r.example.com",
            RegistryAuthContext(registry_name="x", token=None),
            session=session,
        )
        assert client.search("x") == []


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
