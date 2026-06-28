"""Round-29 parser red-team: oversized-int ValueError escapes the
marketplace ``url``-kind JSON decoder.

TARGET: src/apm_cli/marketplace/client.py
  - ``_fetch_url_direct`` (json.loads guard, ~line 352)
  - reached end-to-end by ``fetch_marketplace`` (``except MarketplaceFetchError`` only)

NOVELTY vs ALREADY-FIXED:
  Round-27 (test_round27_marketplace_url_json_recursion.py) widened the
  ``_fetch_url_direct`` json.loads guard to
  ``(json.JSONDecodeError, UnicodeDecodeError, RecursionError)`` so a
  deeply-nested body fails closed. That widen STILL omits a bare
  ``ValueError``. CPython's ``json.loads`` raises a *plain* ``ValueError``
  (NOT a ``json.JSONDecodeError``) when an integer literal exceeds
  ``sys.int_max_str_digits`` (default 4300):

      >>> json.loads('{"x": ' + '9'*5000 + '}')
      ValueError: Exceeds the limit (4300 digits) for integer string conversion

  That ValueError is well under the 10 MiB streaming byte cap (5 KB of
  digits), so the cap never engages, and it is NOT in the except tuple, so
  it escapes ``_fetch_url_direct``. ``fetch_marketplace`` wraps the fetch in
  ``try: ... except MarketplaceFetchError:`` ONLY -- a bare ValueError sails
  straight through to the CLI as an uncaught crash.

REACHABILITY (default-on, untrusted):
  A registered ``source.kind == "url"`` marketplace (``path == ""``, an
  https ``marketplace.json`` URL) points apm at a REMOTE, attacker-influenceable
  document (a compromised or MITM'd marketplace host -- the threat model the
  ``_read_capped_json`` docstring itself acknowledges). ``apm marketplace``
  / ``apm install`` of a marketplace plugin drives ``fetch_marketplace`` on
  that source on a default path. A single oversized integer in the body
  crashes apm with an uncaught ``ValueError``.

SECURE PROPERTY (fail-closed): the hostile body must surface as the typed,
  caught ``MarketplaceFetchError`` (every marketplace caller already treats it
  as fail-closed), never a bare ``ValueError``.

These probes go RED on HEAD 427ed91de (bare ValueError leaks) and GREEN once
the except tuple is widened to include ``ValueError`` (e.g.
``(json.JSONDecodeError, ValueError, UnicodeDecodeError, RecursionError)`` --
JSONDecodeError is a ValueError subclass so adding ValueError subsumes it).
A benign body must still parse.
"""

import json
import os

import pytest

import apm_cli.marketplace.client as mc
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource


class _FakeStreamResponse:
    """Minimal streamed HTTPS response stand-in for ``_http_get``."""

    def __init__(self, body: bytes, *, url: str = "https://evil.example/marketplace.json"):
        self._body = body
        self.url = url
        self.status_code = 200
        self.headers: dict[str, str] = {}  # no Content-Length -> incremental stream path

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self) -> None:
        return None


def _oversized_int_json(digits: int = 5000) -> bytes:
    # Structurally valid object whose single value is an integer literal far
    # past sys.int_max_str_digits (default 4300). ~5 KB << 10 MiB byte cap.
    return b'{"x": ' + (b"9" * digits) + b"}"


def test_url_direct_oversized_int_fails_closed(monkeypatch):
    """``_fetch_url_direct`` must NOT leak a bare ValueError on an oversized int."""
    body = _oversized_int_json()
    monkeypatch.setattr(mc, "_http_get", lambda url, **kw: _FakeStreamResponse(body))

    leaked = None
    try:
        mc._fetch_url_direct("https://evil.example/marketplace.json")
    except MarketplaceFetchError:
        pass  # fail-closed: acceptable
    except ValueError as exc:  # fail-NOT-closed: the bug (JSONDecodeError excluded below)
        # json.JSONDecodeError IS a ValueError subclass and IS in the guard;
        # only a *bare* ValueError (oversized int) reaches here.
        if isinstance(exc, json.JSONDecodeError):
            raise
        leaked = exc

    assert leaked is None, (
        "_fetch_url_direct leaked an uncaught bare ValueError from an "
        "oversized-int marketplace.json body -- its json.loads guard "
        "(JSONDecodeError, UnicodeDecodeError, RecursionError) omits the "
        "plain ValueError that json.loads raises past int_max_str_digits. "
        "fetch_marketplace catches only MarketplaceFetchError, so a hostile "
        "url-kind marketplace crashes apm fail-NOT-closed."
    )


def test_fetch_marketplace_oversized_int_reaches_cli(monkeypatch, tmp_path):
    """End-to-end: the bare ValueError escapes ``fetch_marketplace``'s
    ``except MarketplaceFetchError`` and would reach the CLI uncaught.

    Proves untrusted-reachability of the uncaught crash, not just the sink.
    """
    # Isolate the sidecar cache inside the worktree so no real cache is read
    # or written (and so a fresh url key always misses -> network path runs).
    cache_dir = os.path.join(str(tmp_path), "rt29_mkt_cache")
    os.makedirs(cache_dir, exist_ok=True)
    monkeypatch.setattr(mc, "_cache_dir", lambda: cache_dir)

    body = _oversized_int_json()
    monkeypatch.setattr(mc, "_http_get", lambda url, **kw: _FakeStreamResponse(body))

    source = MarketplaceSource(
        name="evil-url-mkt",
        url="https://evil.example/marketplace.json",
        path="",  # path == "" + https json URL -> kind == "url"
    )
    assert source.kind == "url", "test setup: source must classify as url kind"

    leaked = None
    try:
        mc.fetch_marketplace(source)
    except MarketplaceFetchError:
        pass  # fail-closed: acceptable
    except ValueError as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise
        leaked = exc

    assert leaked is None, (
        "fetch_marketplace propagated an uncaught bare ValueError to its "
        "caller: a url-kind marketplace whose body carries an oversized int "
        "crashes apm install/marketplace instead of failing closed as "
        "MarketplaceFetchError."
    )


def test_benign_marketplace_json_still_parses(monkeypatch):
    """False-positive guard: a normal marketplace.json must still parse."""
    good = json.dumps({"apmVersion": "1.0", "servers": []}).encode("ascii")
    monkeypatch.setattr(mc, "_http_get", lambda url, **kw: _FakeStreamResponse(good))
    result = mc._fetch_url_direct("https://registry.example/marketplace.json")
    assert result is not None
    assert result.data == {"apmVersion": "1.0", "servers": []}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
