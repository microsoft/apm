"""Unit tests for RuntimeManager and runtime CLI commands (pure logic cases)."""

import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys
from unittest.mock import ANY, MagicMock, Mock, call, patch  # noqa: F401

import pytest

from apm_cli.runtime.manager import RuntimeManager

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# RuntimeManager unit tests
# ---------------------------------------------------------------------------


class TestFrozenLoaderEnvIsolation:
    """A frozen binary must not leak its bundled loader paths to setup children."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Bash setup scripts only")
    def test_setup_script_restores_original_loader_paths(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/_internal")
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
        monkeypatch.setenv("DYLD_LIBRARY_PATH", "/bundle/_internal")
        monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)
        manager = RuntimeManager()
        with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            assert manager.run_embedded_script("echo setup", "", [])
        env = mock_run.call_args.kwargs["env"]
        assert env["LD_LIBRARY_PATH"] == "/usr/local/lib"
        assert "LD_LIBRARY_PATH_ORIG" not in env
        assert "DYLD_LIBRARY_PATH" not in env


class TestRuntimeManagerGetRuntimePreference:
    def test_returns_expected_order(self):
        manager = RuntimeManager()
        pref = manager.get_runtime_preference()
        assert pref == ["copilot", "codex", "gemini", "llm"]


class TestRuntimeManagerGetAvailableRuntime:
    def test_returns_first_available(self):
        manager = RuntimeManager()
        with patch.object(manager, "is_runtime_available", side_effect=lambda r: r == "codex"):
            assert manager.get_available_runtime() == "codex"

    def test_returns_none_when_nothing_available(self):
        manager = RuntimeManager()
        with patch.object(manager, "is_runtime_available", return_value=False):
            assert manager.get_available_runtime() is None

    def test_copilot_has_highest_priority(self):
        manager = RuntimeManager()
        with patch.object(manager, "is_runtime_available", return_value=True):
            assert manager.get_available_runtime() == "copilot"


class TestRuntimeManagerSetupRuntime:
    def test_unsupported_runtime_returns_false(self):
        manager = RuntimeManager()
        assert manager.setup_runtime("unknown") is False

    def test_success_path(self):
        manager = RuntimeManager()
        with (
            patch.object(manager, "get_embedded_script", return_value="script"),
            patch.object(manager, "get_common_script", return_value="common"),
            patch.object(manager, "run_embedded_script", return_value=True),
        ):
            assert manager.setup_runtime("copilot") is True

    def test_failure_path(self):
        manager = RuntimeManager()
        with (
            patch.object(manager, "get_embedded_script", return_value="script"),
            patch.object(manager, "get_common_script", return_value="common"),
            patch.object(manager, "run_embedded_script", return_value=False),
        ):
            assert manager.setup_runtime("copilot") is False

    def test_exception_returns_false(self):
        manager = RuntimeManager()
        with patch.object(manager, "get_embedded_script", side_effect=RuntimeError("oops")):
            assert manager.setup_runtime("copilot") is False

    def test_version_arg_unix(self):
        manager = RuntimeManager()
        with (
            patch.object(manager, "get_embedded_script", return_value="s"),
            patch.object(manager, "get_common_script", return_value="c"),
            patch.object(manager, "run_embedded_script", return_value=True) as mock_run,
        ):
            with patch("apm_cli.runtime.manager.sys") as mock_sys:
                mock_sys.platform = "linux"
                manager.setup_runtime("copilot", version="1.0")
        mock_run.assert_called_once_with("s", "c", ["1.0"])

    def test_vanilla_flag_unix(self):
        manager = RuntimeManager()
        with (
            patch.object(manager, "get_embedded_script", return_value="s"),
            patch.object(manager, "get_common_script", return_value="c"),
            patch.object(manager, "run_embedded_script", return_value=True) as mock_run,
        ):
            with patch("apm_cli.runtime.manager.sys") as mock_sys:
                mock_sys.platform = "linux"
                manager.setup_runtime("codex", vanilla=True)
        mock_run.assert_called_once_with("s", "c", ["--vanilla"])


# ---------------------------------------------------------------------------
# commands/runtime.py CLI integration tests
# ---------------------------------------------------------------------------
