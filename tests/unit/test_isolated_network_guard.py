"""Cross-platform capability contracts for the generated subprocess guard."""

from __future__ import annotations

import _socket
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import _NETWORK_GUARD, IsolatedApmEnvironment

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


def _run_guarded_child(
    tmp_path: Path, script: str, *, prelude: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run a fresh interpreter with the real generated sitecustomize guard."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    if prelude:
        (isolated.root / "network_guard" / "sitecustomize.py").write_text(
            prelude + _NETWORK_GUARD, encoding="utf-8"
        )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_guard_preserves_native_sendmsg_capability(tmp_path: Path) -> None:
    """Both public constructors retain their native platform capabilities."""
    result = _run_guarded_child(
        tmp_path,
        """
import _socket, json, sitecustomize, socket
capabilities = {}
for module, native in (
    (socket, sitecustomize._REAL_SOCKET),
    (_socket, sitecustomize._REAL_RAW_SOCKET),
):
    expected = hasattr(native, "sendmsg")
    for name in ("socket", "SocketType"):
        constructor = getattr(module, name)
        instance = constructor()
        try:
            assert hasattr(constructor, "sendmsg") == expected
            assert hasattr(instance, "sendmsg") == expected
            capabilities[module.__name__ + "." + name] = expected
        finally:
            instance.close()
print(json.dumps(capabilities))
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        f"{module}.{constructor}": hasattr(_socket.socket, "sendmsg")
        for module in ("socket", "_socket")
        for constructor in ("socket", "SocketType")
    }


@pytest.mark.parametrize("module", ["asyncio", "unittest.mock"])
def test_guard_allows_real_async_imports(tmp_path: Path, module: str) -> None:
    """Fresh imports must not take a nonexistent Windows os.sysconf branch."""
    result = _run_guarded_child(
        tmp_path,
        f"import importlib; print(importlib.import_module({module!r}).__name__)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.strip() == module


def test_guard_without_native_sendmsg_or_sysconf(tmp_path: Path) -> None:
    """Exercise Windows' absent capabilities deterministically on every OS."""
    result = _run_guarded_child(
        tmp_path,
        """
import asyncio, unittest.mock
import _socket, socket
for module in (socket, _socket):
    for name in ("socket", "SocketType"):
        constructor = getattr(module, name)
        assert not hasattr(constructor, "sendmsg")
        assert not hasattr(constructor(), "sendmsg")
print("imports and absent capabilities preserved")
""",
        prelude="""
import _socket, os, socket

class _SocketWithoutSendmsg:
    pass

class _HighLevelSocketWithoutSendmsg(_SocketWithoutSendmsg):
    pass

_socket.socket = _SocketWithoutSendmsg
socket.socket = _HighLevelSocketWithoutSendmsg
if hasattr(os, "sysconf"):
    del os.sysconf
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.strip() == "imports and absent capabilities preserved"


@pytest.mark.parametrize("module", ["socket", "_socket"])
@pytest.mark.parametrize("constructor", ["socket", "SocketType"])
@pytest.mark.parametrize("family", ["AF_INET", "AF_INET6"])
@pytest.mark.parametrize("operation", ["connect", "sendmsg"])
def test_guard_denies_network_with_native_capabilities(
    tmp_path: Path, module: str, constructor: str, family: str, operation: str
) -> None:
    """Deny IP traffic without manufacturing unsupported socket operations."""
    result = _run_guarded_child(
        tmp_path,
        f"""
import {module} as module
constructor = module.{constructor}
sock = constructor(module.{family}, module.SOCK_DGRAM)
try:
    operation = {operation!r}
    supported = {operation != "sendmsg" or hasattr(_socket.socket, "sendmsg")!r}
    assert hasattr(constructor, operation) == supported
    address = ("203.0.113.1", 9) if {family!r} == "AF_INET" else ("2001:db8::1", 9)
    try:
        if operation == "sendmsg":
            sock.sendmsg([b"x"], [], 0, address)
        else:
            sock.connect(address)
    except OSError as error:
        assert supported
        assert str(error) == "IP network disabled by test environment"
        print("denied")
    except AttributeError:
        assert not supported
        print("unsupported")
    else:
        raise AssertionError("Network operation escaped guard")
finally:
    sock.close()
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    expected = (
        "unsupported"
        if operation == "sendmsg" and not hasattr(_socket.socket, "sendmsg")
        else "denied"
    )
    assert result.stdout.strip() == expected
