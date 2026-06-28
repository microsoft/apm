"""Round-2 bounded pool: concurrency cap, drain, isolation, hostile entry.

Deeper than round-1's head-thread cap check: fire a large batch through
the real ``dispatch_http_batch`` path used by ``LifecycleScripts.fire``
and assert (a) concurrency never exceeds ``MAX_HTTP_DISPATCH_THREADS``,
(b) every entry drains, (c) one entry that raises does not abort the
others, and (d) one entry that NEVER responds holds at most one worker
and does not starve the rest of the batch.

Hermetic: ``requests.post`` is patched. Blocking entries are released in
a ``finally`` and threads joined with a timeout so the suite cannot hang.
Host routing uses ``urllib.parse.urlsplit`` to pick behaviour per entry.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from apm_cli.core.script_executors import MAX_HTTP_DISPATCH_THREADS, dispatch_http_batch

from .conftest import make_event, make_http_script


def _resp(code=200):
    r = MagicMock()
    r.status_code = code
    r.ok = True
    return r


def test_large_batch_caps_concurrency_and_fully_drains():
    n = 100
    scripts = [make_http_script(f"https://h{i}.example.com/x") for i in range(n)]

    live = 0
    peak = 0
    delivered = 0
    lock = threading.Lock()

    def _fake_post(url, *a, **k):
        nonlocal live, peak, delivered
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.005)  # widen the concurrency window
        with lock:
            live -= 1
            delivered += 1
        return _resp()

    with patch("requests.post", side_effect=_fake_post):
        workers = dispatch_http_batch(scripts, make_event())
        # The pool must never start more than the cap, even for 100 entries.
        assert len(workers) == min(n, MAX_HTTP_DISPATCH_THREADS)
        for w in workers:
            w.join(timeout=10)

    assert delivered == n, "every entry must drain"
    assert peak <= MAX_HTTP_DISPATCH_THREADS, f"concurrency exceeded cap: {peak}"


def test_one_raising_entry_does_not_abort_the_batch():
    scripts = [make_http_script(f"https://ok{i}.example.com/x") for i in range(10)]
    scripts.insert(5, make_http_script("https://boom.example.com/x"))

    delivered = []
    lock = threading.Lock()

    def _fake_post(url, *a, **k):
        if urlsplit(url).hostname == "boom.example.com":
            raise RuntimeError("hostile endpoint blew up")
        with lock:
            delivered.append(urlsplit(url).hostname)
        return _resp()

    with patch("requests.post", side_effect=_fake_post):
        for w in dispatch_http_batch(scripts, make_event()):
            w.join(timeout=10)

    # All 10 good entries delivered despite the raising one.
    assert len(delivered) == 10
    assert "boom.example.com" not in delivered


def test_one_never_responding_entry_does_not_starve_others():
    """A single hanging entry holds one worker; the rest drain promptly."""
    release = threading.Event()
    started_hang = threading.Event()
    delivered = []
    lock = threading.Lock()

    # 50 good entries + 1 that blocks forever (until released in finally).
    scripts = [make_http_script(f"https://good{i}.example.com/x") for i in range(50)]
    scripts.append(make_http_script("https://hang.example.com/x"))

    def _fake_post(url, *a, **k):
        if urlsplit(url).hostname == "hang.example.com":
            started_hang.set()
            release.wait(timeout=10)
            return _resp()
        with lock:
            delivered.append(urlsplit(url).hostname)
        return _resp()

    try:
        with patch("requests.post", side_effect=_fake_post):
            workers = dispatch_http_batch(scripts, make_event())
            # Wait for the hang to begin, then confirm the other 50 still
            # complete while the hostile entry is still stuck.
            assert started_hang.wait(timeout=5)
            deadline = time.time() + 5
            while time.time() < deadline:
                with lock:
                    if len(delivered) == 50:
                        break
                time.sleep(0.01)
            with lock:
                got = len(delivered)
            assert got == 50, f"hanging entry starved the batch: only {got}/50 drained"
            release.set()
            for w in workers:
                w.join(timeout=10)
    finally:
        release.set()


def test_empty_batch_starts_no_workers():
    with patch("requests.post", side_effect=AssertionError("should not be called")):
        assert dispatch_http_batch([], make_event()) == []
