"""Public Codex native-client boundaries; no plugin code or remote service runs (pure logic cases)."""

from __future__ import annotations

import io
import queue
from unittest.mock import Mock

import pytest

from apm_cli.codex_plugins.client import CodexRpc

pytestmark = pytest.mark.unit


def test_rpc_reader_failure_stays_bounded_when_response_queue_is_full():
    """A saturated response stream must not raise a second unhandled queue.Full."""
    rpc = CodexRpc.__new__(CodexRpc)
    rpc._process = Mock(stdout=io.BytesIO(b"invalid-json\n"))
    rpc._responses = queue.Queue(maxsize=1)
    rpc._responses.put({"id": 1})
    rpc._reader_error = None
    rpc._read()
    assert isinstance(rpc._reader_error, ValueError)
    assert rpc._responses.qsize() == 1


def test_rpc_close_reaps_helper_after_broken_pipe():
    rpc = CodexRpc.__new__(CodexRpc)
    rpc._process = Mock()
    rpc._process.stdin.close.side_effect = BrokenPipeError("closed native stream")
    rpc._reader = Mock()
    with pytest.raises(BrokenPipeError):
        rpc.close()
    rpc._process.wait.assert_called_once_with(timeout=5)
    rpc._reader.join.assert_called_once_with(timeout=1)
    rpc._process.stdout.close.assert_called_once()
