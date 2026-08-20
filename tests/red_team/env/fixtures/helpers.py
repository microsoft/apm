"""Shared builders for the env exfiltration red-team suite.

These helpers keep every test hermetic: HTTP delivery is intercepted by
monkeypatching ``requests.post`` so the captured headers can be asserted
without any network traffic, and command/log helpers route through the
real executors with ``APM_HOME`` pointed at a tmp dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apm_cli.core import script_executors as se
from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry


def make_event(event_name: str = "post-install") -> LifecycleEvent:
    """A fixed, deterministic lifecycle event payload."""
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/home/victim/project",
    )


class _CapturedPost:
    """Stand-in for ``requests.post`` that records the call kwargs."""

    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None
        self.url: str | None = None
        self.data: Any = None
        self.called = False

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.called = True
        self.url = url
        self.headers = dict(kwargs.get("headers") or {})
        self.data = kwargs.get("data")

        class _Resp:
            status_code = 200
            ok = True

        return _Resp()


def capture_http_headers(
    script: ScriptEntry,
    monkeypatch: pytest.MonkeyPatch,
    event_name: str = "post-install",
) -> dict[str, str]:
    """Run the real HTTP executor and return the headers actually sent.

    Installs a fake ``requests`` module so no socket is opened. The
    daemon thread is joined before returning so the capture is complete.
    """
    import sys
    import types

    captured = _CapturedPost()
    fake_requests = types.ModuleType("requests")
    fake_requests.post = captured  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    thread = se._execute_http(script, make_event(event_name))
    if thread is not None:
        thread.join(timeout=5)
    assert captured.headers is not None, "requests.post was never invoked"
    return captured.headers


def read_script_log(apm_home: Path) -> str:
    """Return the full contents of the scripts log under *apm_home*."""
    log_path = apm_home / "logs" / "scripts.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")
