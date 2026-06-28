"""Round-26 parser red-team: deps registry JSON reader fail-not-closed.

TARGET: src/apm_cli/deps/registry/client.py

The dedicated package-registry client (used by ``apm install`` when a
project's apm.yml carries a ``registries:`` block, or its lockfile has a
``source: registry`` entry) reads the registry's HTTP body with a bare,
buffered ``response.json()`` in two places:

  * ``_response_json`` (the ``GET /versions`` success path, line ~197)
  * ``_request`` 4xx error-detail decode (line ~182)

Both guard ONLY ``except (ValueError, json.JSONDecodeError)``.

CPython's ``json.loads`` raises ``RecursionError`` -- a subclass of
``RuntimeError``, NOT of ``ValueError`` -- when it decodes a deeply nested
array/object. So a malicious or MITM'd registry that returns a modestly
sized ``[[[[...]]]]`` body makes ``RecursionError`` escape uncaught all the
way up:

    _response_json -> list_versions -> RegistryPackageResolver._registry_call
    (catches only RegistryError) -> resolve -> apm install

This is the SAME class of bug round-21/22 closed for bundle/plugin JSON and
round-25 closed (for the OOM/cap half) on the marketplace API client -- but
the deps registry client was never widened. Secure behaviour: a hostile
registry body must fail CLOSED as ``RegistryError`` (mapped onward to
``RegistryResolutionError``), never crash the interpreter with an uncaught
``RecursionError``.

The same bare ``response.json()`` also lacks the round-25 stream cap, so a
huge body is buffered unbounded into memory -- the no-cap OOM half of the
finding. We prove the deterministic crash half here.
"""

import json
import threading

import pytest
import requests

from apm_cli.deps.registry.client import RegistryClient, RegistryError, _safe_problem_json


def _fake_response(body: bytes, *, status: int = 200, ctype: str = "application/json"):
    r = requests.Response()
    r.status_code = status
    r._content = body
    # Mark the body as already buffered so the client's capped iter_content
    # reader replays r._content via iter_slices instead of touching r.raw
    # (None on a synthetic response).
    r._content_consumed = True
    r.headers["Content-Type"] = ctype
    r.url = "https://registry.example/v1/packages/o/r/versions"
    return r


def _deep_json(depth: int) -> bytes:
    return ("[" * depth + "]" * depth).encode("ascii")


def test_response_json_deep_nest_fails_not_closed():
    """_response_json must map a hostile body to RegistryError, not crash."""
    client = RegistryClient.__new__(RegistryClient)
    resp = _fake_response(_deep_json(20000))

    elapsed = {"t": None}

    def _run():
        import time

        start = time.monotonic()
        try:
            client._response_json(resp, "/versions")
        except RegistryError:
            pass  # fail-closed: acceptable
        except RecursionError:
            # fail-NOT-closed: the bug. Re-raise so the assert below trips.
            elapsed["t"] = time.monotonic() - start
            raise
        finally:
            if elapsed["t"] is None:
                elapsed["t"] = time.monotonic() - start

    # Watchdog: this must not hang either (defense-in-depth for the no-cap path).
    worker_exc = {}

    def _wrapped():
        try:
            _run()
        except BaseException as e:
            worker_exc["e"] = e

    t = threading.Thread(target=_wrapped, daemon=True)
    t.start()
    t.join(10.0)
    assert not t.is_alive(), "registry JSON decode hung > 10s (no stream cap / DoS)"

    # SECURE ASSERTION (fails pre-fix): no RecursionError may escape.
    assert "e" not in worker_exc or not isinstance(worker_exc["e"], RecursionError), (
        "deps RegistryClient._response_json leaked an uncaught RecursionError "
        "from a deeply nested registry JSON body -- apm install crashes "
        "fail-not-closed instead of raising RegistryError"
    )


def test_request_error_body_deep_nest_fails_not_closed():
    """The 4xx error-detail decode path must fail closed, not crash.

    Exercises the real module-level ``_safe_problem_json`` the ``_request`` /
    ``fetch_from_url`` / ``publish_version`` error branches now route through.
    A hostile deeply nested 4xx body must degrade to ``{}`` (RecursionError
    swallowed), never escape and crash ``apm install``.
    """
    resp = _fake_response(_deep_json(20000), status=502)
    leaked = None
    problem = None
    try:
        problem = _safe_problem_json(resp)
    except RecursionError as exc:  # fail-not-closed: the bug
        leaked = exc

    assert leaked is None, (
        "_safe_problem_json leaked an uncaught RecursionError -- a hostile 4xx "
        "registry error body crashes apm install fail-not-closed"
    )
    assert problem == {}


def test_benign_versions_payload_still_parses():
    """False-positive guard: a normal /versions body must still decode fine."""
    client = RegistryClient.__new__(RegistryClient)
    good = json.dumps({"versions": [{"version": "1.2.3"}]}).encode("ascii")
    out = client._response_json(_fake_response(good), "/versions")
    assert out == {"versions": [{"version": "1.2.3"}]}


def test_oversized_declared_length_rejected_without_buffering():
    """The stream cap rejects a body whose declared Content-Length exceeds the
    10 MiB cap, failing closed as RegistryError BEFORE buffering it -- the OOM
    half of the finding (round-25's marketplace cap, now on the deps client)."""
    client = RegistryClient.__new__(RegistryClient)
    resp = _fake_response(b'{"versions": []}')
    resp.headers["Content-Length"] = str(11 * 1024 * 1024)
    with pytest.raises(RegistryError):
        client._response_json(resp, "/versions")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
