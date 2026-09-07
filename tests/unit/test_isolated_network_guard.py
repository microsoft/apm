"""Cross-platform socket capability contracts for the hermetic test harness."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import (
    _NETWORK_GUARD,
    IsolatedApmEnvironment,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


@pytest.mark.parametrize("supports_sendmsg", [False, True])
def test_network_guard_preserves_optional_socket_methods(supports_sendmsg: bool) -> None:
    """Exercise both platform APIs without requiring a Windows test host."""
    script = """
import _socket
import socket
import sys

class PlatformSocket:
    family = socket.AF_INET

if sys.argv[1] == "True":
    def sendmsg(self, *args, **kwargs):
        raise AssertionError("unguarded sendmsg reached")
    PlatformSocket.sendmsg = sendmsg

socket.socket = _socket.socket = PlatformSocket
expected = hasattr(PlatformSocket, "sendmsg")
exec(sys.stdin.read(), {"__name__": "sitecustomize"})

for socket_type in (socket.socket, socket.SocketType, _socket.socket, _socket.SocketType):
    assert hasattr(socket_type, "sendmsg") == expected
    if expected:
        try:
            socket_type().sendmsg([b"x"], [], 0, ("203.0.113.1", 9))
        except OSError as error:
            assert str(error) == "IP network disabled by test environment"
        else:
            raise AssertionError("sendmsg was not blocked")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(supports_sendmsg)],
        input=_NETWORK_GUARD,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_isolated_python_can_import_asyncio_and_mock(tmp_path: Path) -> None:
    """Native feature detection must work after sitecustomize installs the guard."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio; import unittest.mock"],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
