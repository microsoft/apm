"""Round-8 http regression trap: un-IDNA-encodable host fail-closed.

r8-http-1 (MED) -- ``_ssrf_block_reason`` resolved DNS names with
``socket.getaddrinfo(host, None)`` under ``except OSError``. But a host
that cannot be IDNA-encoded (an empty label like ``0..0.1``, a label over
63 octets, or a surrogate code point) makes getaddrinfo raise
``UnicodeError`` -- a ``ValueError`` subclass, NOT an ``OSError`` -- which
escaped the guard, propagated through ``_prepare_http`` and crashed the
public single-dispatch ``execute_script`` API (the production batch path
swallowed it, silently dropping the entry). This violates the
``_safe_urlparse`` fail-closed contract. The fix broadens the resolver
guard to ``except (OSError, UnicodeError): return None`` -- an
un-encodable host is unreachable, so allowing it is SSRF-safe and the
request layer simply fails to connect.
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry

_UNENCODABLE_HOSTS = [
    "0..0.1",
    "a" * 64,
    "x" * 300,
    "host\udc80",
]


@pytest.mark.parametrize("host", _UNENCODABLE_HOSTS)
def test_ssrf_reason_unencodable_host_fails_closed(host):
    """An un-IDNA-encodable host must return None, never raise."""
    assert se._ssrf_block_reason(host) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://0..0.1/",
        "https://" + "a" * 64 + "/",
        "https://x.:/path",
    ],
)
def test_execute_script_does_not_crash_on_unencodable_host(url, tmp_path, monkeypatch):
    """The public execute_script path must not raise on a hostile URL."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    entry = ScriptEntry(script_type="http", event="post-install", url=url)
    event = LifecycleEvent(event="post-install")
    # Must complete without propagating an exception to the caller.
    se.execute_script(entry, event)


def test_internal_host_still_blocked():
    """The broadened guard must not weaken the SSRF block for real internals."""
    assert se._ssrf_block_reason("127.0.0.1") is not None
    assert se._ssrf_block_reason("169.254.169.254") is not None
