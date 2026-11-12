"""Count child-process audit events during one linter run."""

from __future__ import annotations

import sys
import threading

_CHILD_PROCESS_EVENTS = frozenset(
    {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
    }
)
_LOCK = threading.Lock()
_COUNT = 0


def _audit_hook(event: str, _args: tuple[object, ...]) -> None:
    global _COUNT
    if event in _CHILD_PROCESS_EVENTS:
        with _LOCK:
            _COUNT += 1


sys.addaudithook(_audit_hook)


def begin_counting() -> None:
    """Atomically reset the process-wide counter for one linter run."""
    global _COUNT
    with _LOCK:
        _COUNT = 0


def child_process_count() -> int:
    """Return child-process events observed since :func:`begin_counting`."""
    with _LOCK:
        return _COUNT
