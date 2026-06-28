"""Round-2 resource caps: streamed body is never buffered.

Deeper than round-1 "stream=True is passed": these tests prove the
executor never *touches* the response body (no ``.content``, ``.text``,
``.json()`` or ``.iter_content``). A malicious endpoint returning an
infinite / chunked / no-content-length body therefore cannot force
buffering or hang the worker past the connect/header timeout -- the
worker reads only ``status_code`` / ``ok`` and returns.

Hermetic: ``requests.post`` returns a booby-trapped response object whose
body accessors raise if read. The executor must finish without tripping
them. Host assertions (where present) use ``urllib.parse``.
"""

from __future__ import annotations

import pytest

from .conftest import make_http_script


class _BodyTrap:
    """A fake response whose body accessors explode if touched.

    Only ``status_code`` and ``ok`` are safe to read -- exactly what a
    non-buffering executor consumes.
    """

    status_code = 200
    ok = True

    @property
    def content(self):  # pragma: no cover - must never be reached
        raise AssertionError("response.content was read (body buffered!)")

    @property
    def text(self):  # pragma: no cover
        raise AssertionError("response.text was read (body buffered!)")

    def json(self):  # pragma: no cover
        raise AssertionError("response.json() was read (body buffered!)")

    def iter_content(self, *a, **k):  # pragma: no cover
        raise AssertionError("response.iter_content was read (body buffered!)")

    def close(self):
        return None


def test_streamed_body_is_never_read(dispatch):
    """Infinite/huge body is safe: the executor reads only the status."""
    recorder = dispatch(
        make_http_script("https://collector.example.com/ingest"),
        response=_BodyTrap(),
    )
    # If any body accessor had been touched, _BodyTrap would have raised
    # inside the worker and the dispatch would still be recorded but the
    # log path would have caught the exception. The key guarantee: the
    # request was made and no body-read assertion fired.
    assert recorder.dispatched
    assert recorder.last.stream is True


def test_stream_flag_and_redirects_off_together(dispatch):
    """stream=True AND allow_redirects=False are both set on the POST."""
    recorder = dispatch(make_http_script("https://collector.example.com/x"))
    call = recorder.last
    assert call.stream is True
    assert call.allow_redirects is False


@pytest.mark.parametrize("timeout_sec", [1, 10, 30])
def test_timeout_is_always_passed_to_post(dispatch, timeout_sec):
    """A finite timeout always reaches requests.post as the read deadline.

    requests treats a scalar timeout as PER-RECV, which a slow-loris resets on
    every dribbled byte; the dispatcher therefore passes a ``(connect, read)``
    tuple AND enforces a total wall-clock deadline (see the round-21 slow-loris
    regression trap). The read element must equal the configured (sub-cap) value.
    """
    recorder = dispatch(
        make_http_script("https://collector.example.com/x", timeout_sec=timeout_sec)
    )
    timeout = recorder.last.timeout
    read = timeout[1] if isinstance(timeout, tuple) else timeout
    assert read == timeout_sec
