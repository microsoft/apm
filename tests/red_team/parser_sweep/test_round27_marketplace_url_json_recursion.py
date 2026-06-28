"""Round-27 parser red-team: marketplace url-direct JSON decode fails-not-closed.

TARGET: src/apm_cli/marketplace/client.py :: _fetch_url_direct (line ~350)

Round-25 added the OOM half (a streamed ``_MAX_MARKETPLACE_JSON_BYTES`` byte
cap via ``_read_bounded_response_bytes``) to the marketplace fetchers. But the
``url``-kind direct fetch then decodes the capped body with:

    data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:   # line ~352

That guard does NOT include ``RecursionError``. CPython's ``json.loads`` raises
``RecursionError`` (a ``RuntimeError`` subclass, NOT a ``ValueError``) on a
deeply nested ``[[[[...]]]]`` body -- and a 40 KB nest is FAR under the 10 MiB
byte cap, so the cap never engages. The error escapes ``_fetch_url_direct``,
and its caller ``fetch_marketplace`` (line ~1130) wraps the fetch in
``try: ... except MarketplaceFetchError:`` ONLY, so the ``RecursionError``
escapes that handler too and crashes the command.

UNTRUSTED REACHABILITY (apm install / apm marketplace, default path): a
``source.kind == "url"`` marketplace points ``apm`` at a REMOTE
``marketplace.json`` over HTTPS. The module itself documents this body as
"attacker-influenceable (a compromised or MITM'd marketplace repo)"
(``_read_capped_json`` docstring). A hostile/MITM'd marketplace returns a small
deeply nested body and crashes resolution fail-NOT-closed.

This is the SAME RecursionError-widen class round-21/22/26 closed for the
bundle/plugin/trust-store/deps-registry JSON readers -- the marketplace
``url`` direct decode was missed (round-25 only added its byte cap, not the
decode-guard widen).

SECURE BEHAVIOUR (asserted here, fails pre-fix): a hostile marketplace.json body
must fail CLOSED as ``MarketplaceFetchError``, never escape as a raw
``RecursionError``. A benign body still parses.

FIX NOTE: widen the line ~352 (and the sibling ``_read_capped_json`` line ~282)
guard to ``(json.JSONDecodeError, UnicodeDecodeError, RecursionError)`` -- map a
deep-nest body to ``MarketplaceFetchError`` like the byte-cap overflow already
is.
"""

import json

import pytest

import apm_cli.marketplace.client as mc
from apm_cli.marketplace.errors import MarketplaceFetchError


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


def _deep_json(depth: int) -> bytes:
    return ("[" * depth + "]" * depth).encode("ascii")


def test_url_direct_deep_nest_fails_not_closed(monkeypatch):
    """A deeply nested (sub-cap) marketplace.json must fail CLOSED, not crash."""
    body = _deep_json(20000)  # 40 KB << 10 MiB cap: byte cap never engages
    monkeypatch.setattr(mc, "_http_get", lambda url, **kw: _FakeStreamResponse(body))

    leaked = None
    try:
        mc._fetch_url_direct("https://evil.example/marketplace.json")
    except MarketplaceFetchError:
        pass  # fail-closed: acceptable
    except RecursionError as exc:  # fail-NOT-closed: the bug
        leaked = exc

    assert leaked is None, (
        "_fetch_url_direct leaked an uncaught RecursionError from a deeply "
        "nested marketplace.json body -- its json.loads guard omits "
        "RecursionError, and fetch_marketplace only catches "
        "MarketplaceFetchError, so apm install/marketplace crashes "
        "fail-NOT-closed on a hostile/MITM'd marketplace url source."
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
