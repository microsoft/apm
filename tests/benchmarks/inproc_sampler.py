"""RESEARCH: in-process stack sampler (py-spy's raw format) for hosts where py-spy cannot attach.

Started from the ``-c`` entry of a profiled run: a daemon thread snapshots every other
thread's stack at *rate* Hz and, at exit, writes ``frame;frame;... count`` lines
(root first), the same format ``spy_phases`` reads. Stacks whose leaf is a blocking
stdlib wait are skipped, approximating py-spy's ``--idle`` off.
"""

from __future__ import annotations

import atexit
import sys
import threading
import time
from collections import Counter

_IDLE_LEAVES = frozenset(
    {
        "wait",
        "_wait_for_tstate_lock",
        "select",
        "poll",
        "acquire",
        "sleep",
        "_communicate",
        "communicate",
        "recv",
        "recv_into",
        "readinto",
        "_worker",
        "get",
    }
)


def _render(frame) -> tuple[str, str] | None:
    parts = []
    leaf = frame.f_code
    while frame is not None:
        code = frame.f_code
        parts.append(f"{code.co_name} ({code.co_filename}:{frame.f_lineno})")
        frame = frame.f_back
    if leaf.co_name in _IDLE_LEAVES and "apm_cli" not in leaf.co_filename:
        return None
    return leaf.co_name, ";".join(reversed(parts))


def start(out: str, rate: int = 250) -> None:
    stacks: Counter = Counter()
    me = threading.get_ident()
    period = 1.0 / rate

    def loop() -> None:
        while True:
            time.sleep(period)
            for ident, frame in sys._current_frames().items():
                if ident == me:
                    continue
                rendered = _render(frame)
                if rendered is not None:
                    stacks[rendered[1]] += 1

    thread = threading.Thread(target=loop, name="apm-inproc-sampler", daemon=True)
    thread.start()
    me = thread.ident

    def dump() -> None:
        with open(out, "w", encoding="utf-8") as handle:
            for stack, count in list(stacks.items()):
                handle.write(f"{stack} {count}\n")

    atexit.register(dump)
