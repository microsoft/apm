"""Resource exhaustion: unbounded response body + unbounded dispatch threads.

Two genuine breaks on head:

* ``d3-resp-buffer`` (severity: medium). ``requests.post`` is called
  WITHOUT ``stream=True``. ``requests`` eagerly downloads the full
  response body into memory before returning, yet the executor only reads
  ``resp.status_code``. A malicious endpoint that answers a webhook with a
  multi-gigabyte body (within the 10s timeout) inflicts memory pressure on
  the host for zero benefit. Secure expectation: pass ``stream=True`` (and
  never read ``.content``/``.text``) so the body is not buffered. The
  assertion FAILS on head.

* ``d3-thread-exhaust`` (severity: medium). ``LifecycleScriptRunner.fire``
  starts one daemon thread per http entry with NO concurrency cap. A
  policy/user script file with N http entries for a single event spawns N
  simultaneous threads + sockets. Secure expectation: a sane cap (bounded
  pool / semaphore). The assertion FAILS on head.

Both tests are hermetic: ``requests.post`` is patched. The thread test
uses a blocking post so the started threads are observably concurrent,
then releases and joins them.
"""

from __future__ import annotations

import threading

from apm_cli.core.lifecycle_scripts import LifecycleScriptRunner

from .conftest import make_event, make_http_script

# Secure upper bound on simultaneous dispatch threads from one event.
# Head applies no cap; with N entries it starts N threads.
MAX_CONCURRENT_DISPATCH = 32


class TestResponseBodyNotBuffered:
    def test_stream_true_is_passed(self, dispatch) -> None:
        recorder = dispatch(make_http_script("https://hooks.example.com/p"))
        assert recorder.dispatched
        # SECURE expectation: body must not be eagerly buffered.
        # HEAD: stream kwarg absent -> requests buffers the whole body.
        assert recorder.last.stream is True, (
            "executor calls requests.post without stream=True; a malicious "
            "endpoint can force the whole response body into memory"
        )


class TestDispatchThreadCap:
    def test_fire_caps_concurrent_threads(self, blocking_post) -> None:
        n_entries = 500
        scripts = [
            make_http_script(f"https://endpoint-{i}.example.com/hook") for i in range(n_entries)
        ]
        runner = LifecycleScriptRunner(scripts=scripts)
        event = make_event()

        with blocking_post() as (release, _started, _live, _lock):
            threads = runner.fire("post-install", event)
            try:
                # SECURE expectation: fire() must not start an unbounded
                # number of dispatch threads. HEAD starts one per entry.
                assert len(threads) <= MAX_CONCURRENT_DISPATCH, (
                    f"fire() started {len(threads)} concurrent dispatch threads "
                    f"for {n_entries} http entries with no cap (expected "
                    f"<= {MAX_CONCURRENT_DISPATCH})"
                )
            finally:
                release.set()
                for t in threads:
                    if isinstance(t, threading.Thread):
                        t.join(timeout=5)

    def test_small_batch_all_delivered(self, dispatch) -> None:
        """Regression trap: a modest batch must still all be delivered.

        Exercised one-at-a-time through the same dispatch helper to confirm
        the executor itself delivers each configured entry.
        """
        scripts = [make_http_script(f"https://e{i}.example.com/h") for i in range(5)]
        delivered = 0
        for s in scripts:
            if dispatch(s).dispatched:
                delivered += 1
        assert delivered == 5

    def test_fire_returns_thread_per_http_entry_on_head(self, blocking_post) -> None:
        """Documents the current (vulnerable) 1:1 entry->thread fan-out.

        Not an assertion of secure behaviour -- it pins the observed head
        behaviour so the break in test_fire_caps_concurrent_threads is
        unambiguous: every entry yields its own live thread.
        """
        n_entries = 20
        scripts = [make_http_script(f"https://e{i}.example.com/h") for i in range(n_entries)]
        runner = LifecycleScriptRunner(scripts=scripts)

        with blocking_post() as (release, started, live, lock):
            threads = runner.fire("post-install", make_event())
            try:
                # Wait until every worker has entered the blocking post.
                for _ in range(n_entries):
                    assert started.acquire(timeout=5)
                with lock:
                    assert live["count"] == n_entries
                assert len(threads) == n_entries
            finally:
                release.set()
                for t in threads:
                    if isinstance(t, threading.Thread):
                        t.join(timeout=5)
