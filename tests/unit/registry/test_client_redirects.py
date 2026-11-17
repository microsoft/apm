"""Registry requests follow redirects only inside the configured registry (#3726).

A gateway in front of a registry may answer ``307`` to a decoded, same-origin
path (Envoy ``UNESCAPE_AND_REDIRECT``). The client follows that hop with its
credential, refuses any hop that leaves the configured origin or base path,
and never re-sends a publish body anywhere else.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.deps.registry.auth import RegistryAuthContext
from apm_cli.deps.registry.client import RegistryClient, RegistryError

pytestmark = pytest.mark.unit

BASE = "https://registry.example.com/apm"
TOKEN = "registry-sentinel-token"


def _response(status: int, *, location: str | None = None, body: bytes = b"", ctype: str = ""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.url = "<test>"
    resp.headers = {}
    if location is not None:
        resp.headers["Location"] = location
    if ctype:
        resp.headers["Content-Type"] = ctype
    resp.content = body
    resp.iter_content = lambda chunk_size=None: iter([body] if body else [])
    return resp


def _session(*responses):
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = list(responses)
    return session


def _client(session, *, token: str | None = TOKEN) -> RegistryClient:
    return RegistryClient(
        BASE, RegistryAuthContext(registry_name="corp", token=token), session=session
    )


def _calls(session) -> list[tuple[str, str, str | None]]:
    return [
        (
            call.args[0],
            call.kwargs["url"],
            call.kwargs["headers"].get("Authorization"),
        )
        for call in session.request.call_args_list
    ]


_VERSIONS = b'{"versions": [{"version": "1.0.0", "digest": "sha256:aa", "published_at": "t"}]}'


def test_versions_follow_same_origin_307_to_decoded_path() -> None:
    encoded = f"{BASE}/v1/packages/gitlab.com%2Facme/infra-prompts/versions"
    decoded = "/apm/v1/packages/gitlab.com/acme/infra-prompts/versions"
    session = _session(
        _response(307, location=decoded),
        _response(200, body=_VERSIONS, ctype="application/json"),
    )

    versions = _client(session).list_versions("gitlab.com/acme", "infra-prompts")

    assert [entry.version for entry in versions] == ["1.0.0"]
    calls = _calls(session)
    assert calls[0] == ("GET", encoded, f"Bearer {TOKEN}")
    assert urlparse(calls[1][1]).path == decoded
    assert calls[1][2] == f"Bearer {TOKEN}"
    assert all(call.kwargs["allow_redirects"] is False for call in session.request.call_args_list)


@pytest.mark.parametrize(
    "location",
    [
        "https://elsewhere.example.com/apm/v1/packages/acme/x/versions",
        "//elsewhere.example.com/apm/v1/packages/acme/x/versions",
        "https://registry.example.com:8443/apm/v1/packages/acme/x/versions",
        "http://registry.example.com/apm/v1/packages/acme/x/versions",
        "/other/v1/packages/acme/x/versions",
        "/apm/../other/v1/packages/acme/x/versions",
        "/apm/%2e%2e/other/v1/packages/acme/x/versions",
        "https://user:pw@registry.example.com/apm/v1/packages/acme/x/versions",
    ],
)
def test_versions_refuse_redirect_outside_configured_registry(location: str) -> None:
    session = _session(_response(307, location=location))

    with pytest.raises(RegistryError, match="outside the configured registry") as excinfo:
        _client(session).list_versions("acme", "x")

    assert excinfo.value.status == 307
    assert session.request.call_count == 1


def test_redirect_chain_longer_than_three_hops_is_refused() -> None:
    hops = [_response(307, location=f"/apm/v1/hop/{index}") for index in range(4)]
    session = _session(*hops)

    with pytest.raises(RegistryError, match="redirected more than 3 times"):
        _client(session).list_versions("acme", "x")

    assert session.request.call_count == 4


def test_lockfile_replay_follows_same_origin_redirect() -> None:
    url = f"{BASE}/v1/packages/gitlab.com%2Facme/x/versions/1.0.0/download"
    session = _session(
        _response(308, location="/apm/v1/packages/gitlab.com/acme/x/versions/1.0.0/download"),
        _response(200, body=b"archive", ctype="application/gzip"),
    )

    body, ctype = _client(session).fetch_from_url(url)

    assert (body, ctype) == (b"archive", "application/gzip")
    assert [call[2] for call in _calls(session)] == [f"Bearer {TOKEN}"] * 2


def test_lockfile_replay_refuses_cross_origin_redirect() -> None:
    session = _session(_response(302, location="https://cdn.example.net/blob"))

    with pytest.raises(RegistryError, match="outside the configured registry"):
        _client(session).fetch_from_url(f"{BASE}/v1/packages/acme/x/versions/1.0.0/download")

    assert session.request.call_count == 1


def test_publish_307_same_origin_resends_method_and_body() -> None:
    archive = b"PK\x03\x04archive"
    session = _session(
        _response(307, location="/apm/v1/packages/gitlab.com/acme/x/versions/1.0.0"),
        _response(201, body=b'{"package": "acme/x", "version": "1.0.0", "digest": "sha256:1"}'),
    )

    result = _client(session).publish_version("gitlab.com/acme", "x", "1.0.0", archive)

    assert result.digest == "sha256:1"
    second = session.request.call_args_list[1]
    assert second.args == ("PUT",)
    assert second.kwargs["data"] == archive
    assert second.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("status", "location", "message"),
    [
        (307, "https://other.example.net/upload", "outside the configured registry"),
        (308, "/elsewhere/upload", "outside the configured registry"),
        (302, "/apm/v1/packages/acme/x/versions/1.0.0", "only 307 and 308"),
        (303, "/apm/v1/packages/acme/x/versions/1.0.0", "only 307 and 308"),
    ],
)
def test_publish_redirect_outside_bounds_is_not_followed(
    status: int, location: str, message: str
) -> None:
    session = _session(_response(status, location=location))

    with pytest.raises(RegistryError, match=message):
        _client(session).publish_version("acme", "x", "1.0.0", b"PK\x03\x04archive")

    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False
