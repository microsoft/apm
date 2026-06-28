"""Shared hermetic fixtures for the round-2 HTTP executor sweep.

The executor imports ``requests`` lazily inside the dispatch worker
(``import requests`` then ``requests.post(...)``), so patching the module
attribute ``requests.post`` intercepts every dispatch regardless of which
thread runs it. The SSRF guard resolves names through
``socket.getaddrinfo`` *inside* ``apm_cli.core.script_executors``; tests
patch that symbol to keep DNS hermetic and to model rebinding.

Every URL/host assertion parses the captured URL with ``urllib.parse``
(``urlsplit``) and compares parsed components -- never substring -- per
the repo test-convention rule.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry


def make_event(event_name: str = "post-install") -> LifecycleEvent:
    """Build a deterministic lifecycle event payload."""
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/home/victim/project",
    )


@pytest.fixture(autouse=True)
def _neutralize_guarded_session():
    """Force the resolve-and-pin guarded session off for hermetic dispatch.

    Production wraps ``requests.post`` in a DNS-pinned ``requests.Session``
    (closing the rebinding TOCTOU); that bypasses module-level
    ``patch("requests.post", ...)``. Returning ``None`` from
    ``_get_guarded_session`` routes every dispatch back through the mocked
    ``requests.post`` so these tests stay hermetic. The pin itself is
    covered directly in ``test_dns_rebinding_pinned.py``.
    """
    from apm_cli.core import script_executors

    with patch.object(script_executors, "_get_guarded_session", return_value=None):
        yield


def make_http_script(url: str, **kwargs: Any) -> ScriptEntry:
    """Build an http ScriptEntry pointed at *url*."""
    return ScriptEntry(script_type="http", event="post-install", url=url, **kwargs)


@dataclass
class PostCall:
    """One captured ``requests.post`` invocation."""

    url: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]

    @property
    def timeout(self) -> Any:
        return self.kwargs.get("timeout")

    @property
    def allow_redirects(self) -> Any:
        return self.kwargs.get("allow_redirects")

    @property
    def stream(self) -> Any:
        return self.kwargs.get("stream")


@dataclass
class Recorder:
    """Records ``requests.post`` calls made by executor threads."""

    calls: list[PostCall] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, url: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        with self._lock:
            self.calls.append(PostCall(url=url, args=args, kwargs=kwargs))

    @property
    def dispatched(self) -> bool:
        return bool(self.calls)

    @property
    def last(self) -> PostCall:
        assert self.calls, "expected a requests.post call but none was recorded"
        return self.calls[-1]


@pytest.fixture(autouse=True)
def hermetic_dns():
    """Default the guard's resolver to a benign PUBLIC IP.

    Keeps the suite fully offline: ``_ssrf_block_reason`` calls
    ``socket.getaddrinfo`` for any non-literal host, which would otherwise
    hit real DNS. Tests modelling rebinding patch the same symbol again
    inside their own ``with`` block (nested patch wins, then restores).
    """
    import socket

    def _public(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    with patch("apm_cli.core.script_executors.socket.getaddrinfo", side_effect=_public):
        yield


def _fake_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    return resp


@pytest.fixture
def dispatch():
    """Run ``_execute_http`` hermetically and return a Recorder.

    Patches ``requests.post``, invokes the executor, joins the daemon
    thread (so dispatch is fully recorded before assertions run). If the
    guard refuses up-front, ``recorder.dispatched`` is False and no socket
    was ever opened.
    """
    from apm_cli.core import script_executors

    def _run(
        script: ScriptEntry,
        event: LifecycleEvent | None = None,
        *,
        verbose: bool = False,
        status_code: int = 200,
        response: Any = None,
    ) -> Recorder:
        recorder = Recorder()

        def _fake_post(url: str, *args: Any, **kwargs: Any) -> Any:
            recorder.record(url, args, kwargs)
            return response if response is not None else _fake_response(status_code)

        with (
            patch("requests.post", side_effect=_fake_post),
            patch.object(script_executors, "_get_guarded_session", return_value=None),
        ):
            thread = script_executors._execute_http(
                script,
                event if event is not None else make_event(),
                verbose=verbose,
            )
            if isinstance(thread, threading.Thread):
                thread.join(timeout=5)
        return recorder

    return _run
