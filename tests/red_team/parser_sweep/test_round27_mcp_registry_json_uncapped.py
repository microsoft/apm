"""Round-27 parser red-team: MCP SimpleRegistryClient JSON reader uncapped.

TARGET: src/apm_cli/registry/client.py  ::  SimpleRegistryClient._cached_get_json

This is the DIFFERENT registry module from the ``deps`` ``RegistryClient`` that
round-26 capped (``deps/registry/client.py``). The MCP discovery client reads
the registry's HTTP body with a bare, buffered ``response.json()`` (and a bare
``response.content`` on the cache-store path) in THREE places:

  * line ~212  -- the cache-disabled / auth-bypass fast path
  * line ~251  -- ``body = response.content`` (buffers the whole body to store)
  * line ~260  -- the post-store success return ``response.json()``

None of them caps the body size, and none widens the decode guard. Two genuine
breaks follow on a DEFAULT, untrusted-reachable path:

UNTRUSTED REACHABILITY (apm install / uninstall):
    a cloned repo's apm.yml MCP dependency carries a ``registry:`` URL
    (``MCPDependency.registry``). On install that URL flows untouched:

        APMPackage.from_apm_yml(apm.yml)            # untrusted clone
          -> MCPDependency.from_dict(dep).registry  # attacker-chosen host
          -> MCPServerOperations(registry_url=...)   # mcp_integrator_install.py:759
          -> SimpleRegistryClient(registry_url)
          -> _cached_get_json(...)  ->  response.json()   # UNBOUNDED

    So an attacker who publishes a package pointing at ``registry:
    https://evil.example/`` makes the victim's ``apm install`` fetch and
    buffer/parse an attacker-sized, attacker-shaped body.

BREAK 1 (OOM, no possible guard): ``response.content`` / ``response.json()``
    buffer the FULL body into memory before any ``except`` can run. A
    multi-GB body exhausts memory regardless of downstream ``except
    Exception`` guards. The round-26 deps client rejects this cheaply off the
    declared ``Content-Length`` (10 MiB cap) BEFORE buffering; the MCP client
    has no such cap.

BREAK 2 (deep-nest crash): ``response.json()`` -> ``json.loads`` raises
    ``RecursionError`` on a modestly sized ``[[[[...]]]]`` body. The MCP client
    does not map it to a controlled registry/transport error, so it escapes
    ``_cached_get_json`` raw. (Several install call sites swallow it with a
    broad ``except Exception`` -- but the method's own contract, like its
    round-26 sibling, must fail CLOSED as a bounded error.)

SECURE BEHAVIOUR (asserted here, fails pre-fix): a hostile registry body must
fail closed -- a bounded error off an over-cap declared length WITHOUT
buffering, and no raw ``RecursionError`` / ``MemoryError`` escaping -- while a
benign servers payload still parses.

FIX NOTE: route ``_cached_get_json`` through a capped incremental reader (mirror
``deps/registry/client.py`` ``_read_capped_body`` / ``_decode_capped_json``):
use ``self.session.get(..., stream=True)``, reject on declared/actual bytes >
cap, and decode with a guard widened to ``(ValueError, json.JSONDecodeError,
RecursionError, UnicodeDecodeError)``.
"""

import json
import threading
import time

import pytest
import requests

from apm_cli.registry.client import SimpleRegistryClient


def _bare_client() -> SimpleRegistryClient:
    """A SimpleRegistryClient with __init__ bypassed (no network, no cache).

    The cache-disabled branch (``self._http_cache is None``) takes the
    simplest path: ``session.get`` -> ``raise_for_status`` -> ``response.json()``.
    """
    client = SimpleRegistryClient.__new__(SimpleRegistryClient)
    client.registry_url = "https://registry.example"
    client._http_cache = None
    client._timeout = (5, 5)
    return client


class _FakeSession:
    """Minimal requests.Session stand-in returning a pre-baked response."""

    def __init__(self, response: requests.Response):
        self._response = response
        self.headers: dict[str, str] = {}

    def get(self, url, **kwargs):
        return self._response


def _fake_response(body: bytes, *, status: int = 200, content_length: str | None = None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r._content_consumed = True
    r.headers["Content-Type"] = "application/json"
    if content_length is not None:
        r.headers["Content-Length"] = content_length
    r.url = "https://registry.example/v0.1/servers"
    return r


def _deep_json(depth: int) -> bytes:
    return ("[" * depth + "]" * depth).encode("ascii")


def test_cached_get_json_deep_nest_fails_not_closed():
    """A deeply nested registry body must fail CLOSED, not crash the parser.

    Pre-fix: ``response.json()`` -> ``RecursionError`` escapes
    ``_cached_get_json`` uncaught.
    """
    client = _bare_client()
    client.session = _FakeSession(_fake_response(_deep_json(20000)))

    worker_exc: dict[str, BaseException] = {}

    def _run():
        try:
            client._cached_get_json("https://registry.example/v0.1/servers")
        except (requests.RequestException, ValueError):
            pass  # fail-closed: a controlled, catchable error is acceptable
        except BaseException as exc:
            worker_exc["e"] = exc

    t = threading.Thread(target=_run, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(10.0)
    assert not t.is_alive(), "MCP registry JSON decode hung > 10s (no cap / DoS)"
    _ = time.monotonic() - start

    leaked = worker_exc.get("e")
    assert not isinstance(leaked, RecursionError), (
        "SimpleRegistryClient._cached_get_json leaked an uncaught RecursionError "
        "from a deeply nested MCP registry body -- the discovery path fails "
        "NOT-closed instead of raising a bounded registry/transport error "
        "(round-26 capped the sibling deps client; this MCP client was missed)."
    )


def test_cached_get_json_oversized_declared_length_rejected_without_buffering():
    """An over-cap declared Content-Length must be rejected WITHOUT buffering.

    The OOM half: a malicious registry advertising a multi-GB body must fail
    closed off the declared length before ``response.content`` /
    ``response.json()`` buffers it. ``.json()`` here raises ``MemoryError`` to
    stand in for the real OOM a 5 GiB ``response.content`` read would cause, so
    any client that touches the body instead of honouring a byte cap trips it.
    """

    class _OOMResponse(requests.Response):
        @property
        def content(self):  # type: ignore[override]
            raise MemoryError("simulated OOM: buffered 5 GiB registry body")

        def json(self, **kwargs):
            raise MemoryError("simulated OOM: parsed 5 GiB registry body")

    resp = _OOMResponse()
    resp.status_code = 200
    resp._content_consumed = True
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Length"] = str(5 * 1024 * 1024 * 1024)  # 5 GiB > any sane cap
    resp.url = "https://registry.example/v0.1/servers"

    client = _bare_client()
    client.session = _FakeSession(resp)

    leaked: dict[str, BaseException] = {}
    try:
        client._cached_get_json("https://registry.example/v0.1/servers")
    except (requests.RequestException, ValueError):
        pass  # fail-closed off a byte cap: acceptable
    except BaseException as exc:
        leaked["e"] = exc

    assert not isinstance(leaked.get("e"), MemoryError), (
        "SimpleRegistryClient._cached_get_json buffered/parsed a body whose "
        "declared Content-Length (5 GiB) far exceeds any safe cap -- it must "
        "reject off the declared length BEFORE touching the body (mirror the "
        "round-26 deps _read_capped_body cap). Unbounded read = memory-exhaustion "
        "DoS reachable from an untrusted apm.yml MCP registry URL on apm install."
    )


def test_benign_servers_payload_still_parses():
    """False-positive guard: a normal /servers body must still decode fine."""
    client = _bare_client()
    good = json.dumps({"servers": [{"name": "io.example/srv", "id": "abc"}]}).encode("ascii")
    client.session = _FakeSession(_fake_response(good, content_length=str(len(good))))
    data, _hdrs = client._cached_get_json("https://registry.example/v0.1/servers")
    assert data == {"servers": [{"name": "io.example/srv", "id": "abc"}]}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
