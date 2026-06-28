"""Round-9 http regression trap: ValueError resolver fails closed.

r9-http-1 (LOW) -- the round-8 fix broadened ``_ssrf_block_reason``'s
resolver guard to ``except (OSError, UnicodeError)``. But a host carrying a
raw NUL byte makes CPython's ``socket.getaddrinfo`` raise a bare
``ValueError('embedded null byte')`` -- NOT a ``UnicodeError`` -- which
escaped the guard, propagated through ``_prepare_http`` and crashed the
public single-dispatch ``execute_script`` (the batch ``fire()`` worker
swallows it, but the single-dispatch helper does not). It fails CLOSED (no
SSRF, no connect) but violates the documented fail-closed contract. Since
``UnicodeError`` is a subclass of ``ValueError``, the fix narrows to
``except (OSError, ValueError)``, covering both.
"""

from __future__ import annotations

import socket

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry


def test_valueerror_resolver_fails_closed(monkeypatch):
    """A getaddrinfo ValueError must return None, never propagate."""

    def _raise(*_a, **_k):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    assert se._ssrf_block_reason("h\x00ost.evil") is None


def test_unicodeerror_resolver_still_fails_closed(monkeypatch):
    """The round-8 UnicodeError case must remain covered after narrowing."""

    def _raise(*_a, **_k):
        raise UnicodeError("idna")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    assert se._ssrf_block_reason("a" * 64) is None


def test_nul_host_does_not_crash_execute_script(tmp_path, monkeypatch):
    """The public execute_script path must not raise on a NUL-host URL."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    monkeypatch.setenv("APM_E2E_TESTS", "1")

    def _raise(*_a, **_k):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    entry = ScriptEntry(script_type="http", event="post-install", url="https://h\x00ost/")
    event = LifecycleEvent(event="post-install")
    se.execute_script(entry, event)  # must not raise


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254"])
def test_internal_host_still_blocked(host):
    """Narrowing the guard must not weaken the SSRF block for real internals."""
    assert se._ssrf_block_reason(host) is not None
