"""Vector 6 -- HTTP thread join semantics in fire().

fire() returns the daemon threads HTTP scripts spawn. We assert:
- HTTP scripts run on a daemon thread (a hung POST can never block
  interpreter exit).
- command scripts run synchronously (no thread returned; their side
  effects are visible the instant fire() returns).
"""

from __future__ import annotations

import threading
from pathlib import Path

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
    ScriptEntry,
)

from .conftest import make_command_entry, touch_cmd


def _event(wd: str) -> LifecycleEvent:
    return LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=wd,
    )


def test_http_script_runs_on_daemon_thread(apm_home: Path, tmp_path: Path, monkeypatch) -> None:
    """A hung HTTP POST stays on a daemon thread and cannot block exit."""
    import requests

    release = threading.Event()
    entered = threading.Event()

    def fake_post(*_a, **_k):
        entered.set()
        # Simulate a slow/hung endpoint, but stay bounded for the test.
        release.wait(timeout=5)

        class _Resp:
            status_code = 200
            ok = True

        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)

    http_entry = ScriptEntry(
        script_type="http",
        event="post-install",
        url="https://endpoint.invalid/hook",
        source="user",
    )
    runner = LifecycleScriptRunner(scripts=[http_entry])

    threads = runner.fire("post-install", _event(str(tmp_path)))
    try:
        assert len(threads) == 1, "HTTP script should return exactly one thread"
        t = threads[0]
        assert t.daemon is True, "HTTP thread MUST be daemon to not block exit"
        assert entered.wait(timeout=5), "HTTP worker never started"
        assert t.is_alive(), "HTTP POST should still be in-flight (async)"
    finally:
        release.set()
        for t in threads:
            t.join(timeout=5)


def test_command_script_runs_synchronously(apm_home: Path, tmp_path: Path) -> None:
    """Command scripts return no thread and complete before fire() returns."""
    sentinel = tmp_path / "S"
    runner = LifecycleScriptRunner(scripts=[make_command_entry(touch_cmd(sentinel))])

    threads = runner.fire("post-install", _event(str(tmp_path)))

    assert threads == [], "command scripts must not return a thread"
    assert sentinel.exists(), "command script must finish before fire() returns"


def test_command_then_http_ordering(apm_home: Path, tmp_path: Path, monkeypatch) -> None:
    """Command runs synchronously first; only the HTTP thread is returned."""
    import requests

    order: list[str] = []
    sentinel = tmp_path / "S"

    def fake_post(*_a, **_k):
        order.append("http")

        class _Resp:
            status_code = 200
            ok = True

        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)

    cmd = make_command_entry(touch_cmd(sentinel))
    http_entry = ScriptEntry(
        script_type="http",
        event="post-install",
        url="https://endpoint.invalid/hook",
        source="user",
    )
    runner = LifecycleScriptRunner(scripts=[cmd, http_entry])

    threads = runner.fire("post-install", _event(str(tmp_path)))
    # Command side effect is already visible (synchronous).
    assert sentinel.exists()
    for t in threads:
        t.join(timeout=5)
    assert len(threads) == 1
    assert order == ["http"]
