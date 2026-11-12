"""Public Codex native-client boundaries; no plugin code or remote service runs."""

from __future__ import annotations

import io
import json
import queue
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.codex_plugins.client import CodexNativeClient, CodexNativeError, CodexRpc
from apm_cli.core.scope import InstallScope

pytestmark = pytest.mark.component
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in executable")


_ADMITTED = ["0.163.0", "0.164.0", "0.170.2", "1.0.0", "0.164.0-alpha.11.1", "0.164.0-0.rc-1"]
_REFUSED = [
    "0.162.1",
    "0.163.0-alpha.1",
    "0.163",
    "v0.164.0",
    "0.164.0 extra",
    "unknown",
    "",
    "0.164.0-",
    "0.164.0-.",
    "0.164.0-alpha.",
    "0.164.0-alpha..1",
    "0.164.0-01",
    "0.164.0-alpha.01",
    "00.164.0",
    "0.0164.0",
]


def _stand_in_codex(directory: Path, version_line: str, app_server: str = "") -> Path:
    """Write a real executable that answers --version and optionally one app-server line."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "requests.log"
    script = directory / "codex"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then printf \'%s\\n\' {shlex.quote(version_line)}; exit 0; fi\n'
        f"IFS= read -r line; printf '%s\\n' \"$line\" >> {shlex.quote(str(log))}\n"
        f"printf '%s\\n' {shlex.quote(app_server)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("version", _ADMITTED)
def test_native_client_admits_minimum_and_later_versions(monkeypatch, tmp_path, version):
    """Self-updating Codex installs stay admitted at or above the qualified floor."""
    monkeypatch.setattr("shutil.which", lambda name: "/owned/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, f"codex-cli {version}\n", ""))
    monkeypatch.setattr(subprocess, "run", run)
    client = CodexNativeClient.for_scope(InstallScope.USER, tmp_path)
    assert client.executable == Path("/owned/codex")


@pytest.mark.parametrize("version", _REFUSED)
def test_native_client_refuses_older_or_unparseable_versions(monkeypatch, tmp_path, version):
    """Older or unrecognized clients are refused with the minimum named."""
    monkeypatch.setattr("shutil.which", lambda name: "/owned/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, f"codex-cli {version}\n", ""))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(CodexNativeError, match=r"0\.163\.0 or later"):
        CodexNativeClient.for_scope(InstallScope.USER, tmp_path)


def test_native_client_refuses_failed_version_probe(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: "/owned/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "codex-cli 0.164.0\n", ""))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(CodexNativeError, match=r"0\.163\.0 or later"):
        CodexNativeClient.for_scope(InstallScope.USER, tmp_path)


def test_missing_native_client_names_minimum(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(CodexNativeError, match=r"0\.163\.0 or later on PATH"):
        CodexNativeClient.for_scope(InstallScope.USER, tmp_path)


@_POSIX_ONLY
@pytest.mark.parametrize(
    "version_line, admitted",
    [
        ("codex-cli 0.163.0", True),
        ("codex-cli 0.164.0", True),
        ("codex-cli 0.162.1", False),
        ("codex-cli 0.163.0-alpha.1", False),
        ("not a version", False),
    ],
)
def test_stand_in_binary_version_gate(monkeypatch, tmp_path, version_line, admitted):
    """A real executable on PATH is gated by its own --version output."""
    script = _stand_in_codex(tmp_path / "bin", version_line)
    monkeypatch.setenv("PATH", str(script.parent))
    if admitted:
        assert CodexNativeClient.for_scope(InstallScope.USER, tmp_path).executable == script
    else:
        with pytest.raises(CodexNativeError, match=r"0\.163\.0 or later"):
            CodexNativeClient.for_scope(InstallScope.USER, tmp_path)


@_POSIX_ONLY
@pytest.mark.parametrize(
    "payload", ["not-json", "[1]", '{"id": 99, "result": {}}', '{"id": 1, "result": []}']
)
def test_admitted_client_fails_closed_on_malformed_app_server(monkeypatch, tmp_path, payload):
    """An admitted newer client with protocol drift is refused at initialize."""
    script = _stand_in_codex(tmp_path / "bin", "codex-cli 0.164.0", payload)
    monkeypatch.setenv("PATH", str(script.parent))
    client = CodexNativeClient.for_scope(InstallScope.USER, tmp_path)
    with pytest.raises(CodexNativeError, match="initialize"):
        client.connect().__enter__()
    requests = (script.parent / "requests.log").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["method"] for line in requests] == ["initialize"]


def test_project_scope_refusal_does_not_probe_or_write(monkeypatch, tmp_path):
    """Unsupported project intent must not leak into the user's native configuration."""
    lookup = Mock(side_effect=AssertionError("native binary must not be probed"))
    monkeypatch.setattr("shutil.which", lookup)
    with pytest.raises(CodexNativeError, match="user scope"):
        CodexNativeClient.for_scope(InstallScope.PROJECT, tmp_path)
    assert not list(tmp_path.iterdir())
    lookup.assert_not_called()


def test_qualified_client_uses_resolved_executable(monkeypatch, tmp_path):
    """The qualified binary is resolved once and used without a shell."""
    binary = tmp_path / "native client"
    monkeypatch.setattr("shutil.which", lambda name: str(binary))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.163.0\n", ""))
    monkeypatch.setattr(subprocess, "run", run)
    client = CodexNativeClient.for_scope(InstallScope.USER, tmp_path)
    assert client.executable == binary
    assert run.call_args.args[0] == [str(binary), "--version"]
    assert run.call_args.kwargs["timeout"] > 0
    assert run.call_args.kwargs.get("shell", False) is False


def test_public_config_update_requires_version_and_checks_override(tmp_path):
    """Never treat a masked or concurrent configuration edit as native registration."""
    client = CodexNativeClient(Path("/owned/codex"), tmp_path)
    rpc = Mock()
    rpc.call.return_value = {"status": "okOverridden", "version": "next"}
    with pytest.raises(CodexNativeError, match="overridden"):
        client.write_config(rpc, [{"keyPath": "plugins.owned.enabled", "value": False}], "old")
    method, params = rpc.call.call_args.args
    assert method == "config/batchWrite"
    assert params["expectedVersion"] == "old"
    assert params["edits"] == [
        {"keyPath": "plugins.owned.enabled", "value": False, "mergeStrategy": "replace"}
    ]


def test_selected_marketplace_upgrade_never_requests_global_refresh(tmp_path):
    """The native update operation names exactly one owned marketplace."""
    client = CodexNativeClient(Path("/owned/codex"), tmp_path)
    rpc = Mock()
    rpc.call.return_value = {
        "selectedMarketplaces": ["apm-owned"],
        "upgradedRoots": [str(tmp_path / "native-root")],
        "errors": [],
    }
    result = client.upgrade(rpc, "apm-owned")
    assert result == [tmp_path / "native-root"]
    rpc.call.assert_called_once_with("marketplace/upgrade", {"marketplaceName": "apm-owned"})


def test_native_errors_do_not_echo_arbitrary_payloads(tmp_path):
    """Native failures identify the operation without leaking configuration contents."""
    client = CodexNativeClient(Path("/owned/codex"), tmp_path)
    rpc = Mock()
    rpc.call.return_value = {
        "selectedMarketplaces": ["apm-owned"],
        "upgradedRoots": [],
        "errors": [{"message": "SENSITIVE_NATIVE_VALUE"}],
    }
    with pytest.raises(CodexNativeError) as error:
        client.upgrade(rpc, "apm-owned")
    assert "SENSITIVE_NATIVE_VALUE" not in str(error.value)


def test_read_config_selects_user_layer_not_effective_merged_values(tmp_path):
    """Ownership is checked against the writable user layer, never another scope."""
    client = CodexNativeClient(Path("/owned/codex"), tmp_path)
    rpc = Mock()
    rpc.call.return_value = json.loads(
        json.dumps(
            {
                "config": {"plugins": {"foreign": {"enabled": True}}},
                "layers": [
                    {
                        "name": {"type": "user", "file": str(tmp_path / "config.toml")},
                        "version": "user-version",
                        "config": {"plugins": {"owned": {"enabled": False}}},
                    },
                    {
                        "name": {"type": "project", "dotCodexFolder": "/foreign/.codex"},
                        "version": "project-version",
                        "config": {"plugins": {"foreign": {"enabled": True}}},
                    },
                ],
            }
        )
    )
    state = client.read_config(rpc)
    assert state.version == "user-version"
    assert state.values == {"plugins": {"owned": {"enabled": False}}}
    assert state.path == tmp_path / "config.toml"


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


@pytest.mark.parametrize(
    "actual, expected",
    [
        ("0.163.0", True),
        ("0.164.0", True),
        ("1.0.0", True),
        ("0.162.1", False),
        ("0.164.0-alpha.1", False),
        ("unknown", False),
    ],
)
def test_native_version_prerequisite_is_minimum(monkeypatch, actual, expected):
    """The declarative gate admits stable releases at or above the minimum only."""
    from tests.integration import conftest

    monkeypatch.setattr(conftest.shutil, "which", lambda name: "/owned/codex")
    monkeypatch.setattr(
        conftest.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, f"codex-cli {actual}\n", "")),
    )
    conftest._has_runtime_version.cache_clear()
    try:
        assert conftest._has_runtime_version("codex", "0.163.0") is expected
    finally:
        conftest._has_runtime_version.cache_clear()
