"""Round-25 red-team: the marketplace remote-JSON readers are an ASYMMETRIC
byte-cap surface -- the GitHub / GitLab / Azure-DevOps API fetch paths buffer
and ``json.loads`` an attacker-controlled ``marketplace.json`` with NO size
ceiling, while the direct ``url`` path enforces ``_MAX_MARKETPLACE_JSON_BYTES``
(10 MiB).

This is the SAME class of gap the round-24 builder fold closed (one reader
capped at 64 KiB, its sibling reader left uncapped), one layer up in the
client fetch dispatch:

    _fetch_url_direct  -> _read_bounded_response_bytes(resp, url, 10 MiB)   CAPPED
    _fetch_github      -> parse_response = lambda r: r.json()              UNCAPPED
    _fetch_gitlab/ado  -> _parse_json_text(resp) = json.loads(resp.text)  UNCAPPED

Reachability (untrusted REMOTE input, default command surface):
    apm marketplace add / update / sync / audit / validate
      -> commands/marketplace/*.py: fetch_marketplace(source, force_refresh=True)
      -> marketplace/client.py: _fetch_file(source, path)
      -> _fetch_github / _fetch_gitlab / _fetch_ado   (source.kind in {github,gitlab,ado})
      -> requests resp.json() / json.loads(resp.text)   # unbounded buffer + parse

A malicious (or compromised / MITM'd) marketplace repo serving a multi-GB
``marketplace.json`` over the GitHub Contents API is buffered in full by
``requests`` and materialised by ``json.loads`` -> unbounded memory -> OOM
DoS. The 10 MiB ceiling the maintainer applied to the direct path is the
clear intended contract; the API paths bypass it.

SECURE outcome (post-fix): every remote marketplace.json reader -- API paths
included -- enforces the same ``_MAX_MARKETPLACE_JSON_BYTES`` ceiling and
fails closed with ``MarketplaceFetchError`` on an over-cap body. These probes
assert that secure contract, so they FAIL on the vulnerable head and PASS
once the API readers are bounded.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from .conftest import run_guarded

pytestmark = pytest.mark.e2e


def _oversize_json_body() -> str:
    """A syntactically valid marketplace.json a few KiB OVER the 10 MiB cap."""
    from apm_cli.marketplace import client

    pad = "A" * (client._MAX_MARKETPLACE_JSON_BYTES + 4096)
    return json.dumps({"version": 1, "plugins": [], "_pad": pad})


class _FakeApiResp:
    """Minimal stand-in for a streamed ``requests.Response`` (API path).

    The capped API readers issue the request with ``stream=True`` and pull the
    body through ``iter_content`` so an oversized marketplace.json is rejected
    incrementally (never buffered whole). This fake mirrors that streamed
    contract: ``iter_content`` yields the body in chunks; ``.text``/``.json()``
    remain for any buffered-path assertion.
    """

    status_code = 200
    headers: ClassVar[dict[str, str]] = {}
    url = "https://api.example.com/marketplace.json"

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.text)

    def iter_content(self, chunk_size: int = 65536):
        data = self.text.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def close(self) -> None:
        return None


class _PassthroughAuth:
    """``AuthResolver`` stub: invoke the fetch closure unauthenticated."""

    def try_with_fallback(self, host, fn, **kwargs):
        return fn(None, None)


def _drive_api_fetch(monkeypatch, fetcher_name: str, host: str, url: str):
    """Drive a single API fetcher with an over-cap response and return
    ``(finished, result, exception)`` from the guarded call."""
    from apm_cli.core.auth import AuthResolver
    from apm_cli.marketplace import client
    from apm_cli.marketplace.models import MarketplaceSource

    body = _oversize_json_body()
    monkeypatch.setattr(client, "_http_get", lambda _url, **_kw: _FakeApiResp(body))

    source = MarketplaceSource(name="evil", url=url, ref="main", path="marketplace.json")
    host_info = AuthResolver.classify_host(host)
    fetcher = getattr(client, fetcher_name)

    return run_guarded(
        lambda: fetcher(
            source,
            "marketplace.json",
            host_info=host_info,
            auth_resolver=_PassthroughAuth(),
        ),
        timeout=8.0,
    )


def test_github_api_path_enforces_size_cap(monkeypatch):
    """``_fetch_github`` must reject a >10 MiB marketplace.json body.

    On the vulnerable head it returns the fully-parsed dict (no cap), so this
    assertion fails -- demonstrating the unbounded remote-JSON sink.
    """
    from apm_cli.marketplace.client import _MAX_MARKETPLACE_JSON_BYTES, MarketplaceFetchError

    finished, result, exc = _drive_api_fetch(
        monkeypatch, "_fetch_github", "github.com", "https://github.com/evil/repo"
    )

    assert finished, "github API fetch hung on an oversized marketplace.json"
    assert isinstance(exc, MarketplaceFetchError), (
        "github API path buffered and parsed a marketplace.json larger than "
        f"{_MAX_MARKETPLACE_JSON_BYTES} bytes WITHOUT a size cap "
        f"(returned {type(result).__name__}); the direct url path caps this "
        "but the API path does not -- unbounded remote-JSON OOM sink."
    )


def test_gitlab_api_path_enforces_size_cap(monkeypatch):
    """``_fetch_gitlab`` must reject a >10 MiB marketplace.json body."""
    from apm_cli.marketplace.client import _MAX_MARKETPLACE_JSON_BYTES, MarketplaceFetchError

    finished, result, exc = _drive_api_fetch(
        monkeypatch, "_fetch_gitlab", "gitlab.com", "https://gitlab.com/evil/repo"
    )

    assert finished, "gitlab API fetch hung on an oversized marketplace.json"
    assert isinstance(exc, MarketplaceFetchError), (
        "gitlab API path (json.loads(resp.text)) parsed a marketplace.json "
        f"larger than {_MAX_MARKETPLACE_JSON_BYTES} bytes WITHOUT a size cap "
        f"(returned {type(result).__name__}) -- unbounded remote-JSON OOM sink."
    )


def test_url_direct_path_caps_control(monkeypatch):
    """Control: the direct ``url`` fetch DOES enforce the 10 MiB ceiling.

    This proves the cap is an intended contract that the API paths bypass --
    so it passes on the vulnerable head and stays green after the fix.
    """
    from apm_cli.marketplace import client
    from apm_cli.marketplace.client import MarketplaceFetchError

    body = b"A" * (client._MAX_MARKETPLACE_JSON_BYTES + 4096)

    class _FakeStreamResp:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        url = "https://example.com/marketplace.json"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 65536):
            for i in range(0, len(body), chunk_size):
                yield body[i : i + chunk_size]

        def close(self) -> None:
            return None

    monkeypatch.setattr(client, "_http_get", lambda _url, **_kw: _FakeStreamResp())

    finished, _result, exc = run_guarded(
        lambda: client._fetch_url_direct("https://example.com/marketplace.json"),
        timeout=8.0,
    )
    assert finished, "direct url fetch hung on an oversized marketplace.json"
    assert isinstance(exc, MarketplaceFetchError), (
        "the direct url path is expected to cap the body at "
        f"{client._MAX_MARKETPLACE_JSON_BYTES} bytes"
    )
