"""Shared fixtures for the HTTP executor red-team suite.

All tests here are HERMETIC: ``requests.post`` is always patched so no
real network traffic ever leaves the process. The executor imports
``requests`` lazily inside its worker thread (``import requests`` then
``requests.post(...)``), so patching the module attribute
``requests.post`` intercepts every dispatch regardless of thread timing.

URL assertions throughout this suite parse the captured URL with
``urllib.parse`` and compare on parsed components -- never substring --
per the repo test-convention rule (CodeQL
``py/incomplete-url-substring-sanitization``).
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

    @property
    def headers(self) -> dict[str, str]:
        return self.kwargs.get("headers") or {}

    @property
    def data(self) -> Any:
        return self.kwargs.get("data")


@dataclass
class Recorder:
    """Records ``requests.post`` calls made by the executor thread."""

    calls: list[PostCall] = field(default_factory=list)

    @property
    def dispatched(self) -> bool:
        return bool(self.calls)

    @property
    def last(self) -> PostCall:
        assert self.calls, "expected a requests.post call but none was recorded"
        return self.calls[-1]


def _fake_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    return resp


@pytest.fixture
def dispatch():
    """Return a helper that runs ``_execute_http`` hermetically.

    The helper patches ``requests.post``, invokes the executor, joins the
    daemon thread it returns (so any dispatch is fully recorded before the
    assertion runs), and returns a :class:`Recorder`. If the executor
    refuses the URL up-front (no thread started), ``recorder.dispatched``
    is ``False``.
    """
    from apm_cli.core import script_executors

    def _run(
        script: ScriptEntry,
        event: LifecycleEvent | None = None,
        *,
        logger: Any = None,
        verbose: bool = False,
        status_code: int = 200,
    ) -> Recorder:
        recorder = Recorder()

        def _fake_post(url: str, *args: Any, **kwargs: Any) -> MagicMock:
            recorder.calls.append(PostCall(url=url, args=args, kwargs=kwargs))
            return _fake_response(status_code)

        with (
            patch("requests.post", side_effect=_fake_post),
            patch.object(script_executors, "_get_guarded_session", return_value=None),
            patch.object(script_executors, "_get_capturing_session", return_value=None),
        ):
            thread = script_executors._execute_http(
                script,
                event if event is not None else make_event(),
                logger=logger,
                verbose=verbose,
            )
            if isinstance(thread, threading.Thread):
                thread.join(timeout=5)
        return recorder

    return _run


@pytest.fixture
def blocking_post():
    """Patch ``requests.post`` with a worker that blocks until released.

    Used by thread-exhaustion tests so concurrently started dispatch
    threads remain observably alive while their count is measured. Always
    release + join in a ``finally`` to avoid leaking daemon threads.
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        release = threading.Event()
        started = threading.Semaphore(0)
        live = {"count": 0}
        lock = threading.Lock()

        def _fake_post(url: str, *args: Any, **kwargs: Any) -> MagicMock:
            with lock:
                live["count"] += 1
            started.release()
            release.wait(timeout=10)
            return _fake_response(200)

        from apm_cli.core import script_executors

        with (
            patch("requests.post", side_effect=_fake_post),
            patch.object(script_executors, "_get_guarded_session", return_value=None),
            patch.object(script_executors, "_get_capturing_session", return_value=None),
        ):
            try:
                yield release, started, live, lock
            finally:
                release.set()

    return _ctx
