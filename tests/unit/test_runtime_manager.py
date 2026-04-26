"""Unit tests for RuntimeManager and runtime CLI commands."""

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.runtime import runtime as runtime_group
from apm_cli.runtime.manager import RuntimeManager

# ---------------------------------------------------------------------------
# RuntimeManager unit tests
# ---------------------------------------------------------------------------


class TestRuntimeManagerInit:
    def test_init_sets_runtime_dir(self):
        manager = RuntimeManager()
        assert manager.runtime_dir == Path.home() / ".apm" / "runtimes"

    def test_init_supported_runtimes_keys(self):
        manager = RuntimeManager()
        assert set(manager.supported_runtimes.keys()) == {"copilot", "codex", "llm", "gemini"}

    def test_init_script_extension_unix(self):
        with patch("apm_cli.runtime.manager.sys") as mock_sys:
            mock_sys.platform = "linux"
            manager = RuntimeManager()
        for info in manager.supported_runtimes.values():
            assert info["script"].endswith(".sh")

    def test_init_script_extension_windows(self):
        with patch("apm_cli.runtime.manager.sys") as mock_sys:
            mock_sys.platform = "win32"
            manager = RuntimeManager()
        for info in manager.supported_runtimes.values():
            assert info["script"].endswith(".ps1")


class TestRuntimeManagerGetRuntimePreference:
    def test_returns_expected_order(self):
        manager = RuntimeManager()
        pref = manager.get_runtime_preference()
        assert pref == ["copilot", "codex", "gemini", "llm"]


class TestRuntimeManagerIsRuntimeAvailable:
    def test_unknown_runtime_returns_false(self):
        manager = RuntimeManager()
        assert manager.is_runtime_available("unknown") is False

    def test_binary_in_apm_dir_returns_true(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "copilot"
        binary.write_text("fake")
        assert manager.is_runtime_available("copilot") is True

    def test_binary_dir_in_apm_dir_not_file_returns_false(self, tmp_path):
        """A directory named after the binary is not a valid binary."""
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary_dir = tmp_path / "copilot"
        binary_dir.mkdir()
        # is_file() returns False for directories
        with patch("apm_cli.runtime.manager.shutil.which", return_value=None):
            assert manager.is_runtime_available("copilot") is False

    def test_binary_in_system_path_returns_true(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path  # nothing here
        with patch(
            "apm_cli.runtime.manager.shutil.which", return_value="/usr/bin/copilot"
        ):
            assert manager.is_runtime_available("copilot") is True

    def test_binary_not_found_returns_false(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        with patch("apm_cli.runtime.manager.shutil.which", return_value=None):
            assert manager.is_runtime_available("copilot") is False


class TestRuntimeManagerGetAvailableRuntime:
    def test_returns_first_available(self):
        manager = RuntimeManager()
        with patch.object(
            manager, "is_runtime_available", side_effect=lambda r: r == "codex"
        ):
            assert manager.get_available_runtime() == "codex"

    def test_returns_none_when_nothing_available(self):
        manager = RuntimeManager()
        with patch.object(manager, "is_runtime_available", return_value=False):
            assert manager.get_available_runtime() is None

    def test_copilot_has_highest_priority(self):
        manager = RuntimeManager()
        with patch.object(manager, "is_runtime_available", return_value=True):
            assert manager.get_available_runtime() == "copilot"


class TestRuntimeManagerListRuntimes:
    def test_all_not_installed_when_nothing_found(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        with patch("apm_cli.runtime.manager.shutil.which", return_value=None):
            result = manager.list_runtimes()
        assert set(result.keys()) == {"copilot", "codex", "llm", "gemini"}
        for name, info in result.items():
            assert info["installed"] is False
            assert info["path"] is None

    def test_runtime_found_in_apm_dir(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "codex"
        binary.write_text("fake")
        with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
            proc = MagicMock()
            proc.returncode = 1
            mock_run.return_value = proc
            result = manager.list_runtimes()
        assert result["codex"]["installed"] is True
        assert result["codex"]["path"] == str(binary)

    def test_runtime_found_in_system_path(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        with patch("apm_cli.runtime.manager.shutil.which", return_value="/usr/bin/llm"):
            with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
                proc = MagicMock()
                proc.returncode = 1
                mock_run.return_value = proc
                result = manager.list_runtimes()
        assert result["llm"]["installed"] is True
        assert result["llm"]["path"] == "/usr/bin/llm"

    def test_version_detected_when_available(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "llm"
        binary.write_text("fake")
        with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "1.2.3\n"
            mock_run.return_value = proc
            result = manager.list_runtimes()
        assert result["llm"]["version"] == "1.2.3"

    def test_version_set_to_unknown_on_exception(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "llm"
        binary.write_text("fake")
        with patch(
            "apm_cli.runtime.manager.subprocess.run", side_effect=Exception("timeout")
        ):
            result = manager.list_runtimes()
        assert result["llm"]["version"] == "unknown"


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
        with patch.object(
            manager, "get_embedded_script", side_effect=RuntimeError("oops")
        ):
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


class TestRuntimeManagerRemoveRuntime:
    def test_unknown_runtime_returns_false(self):
        manager = RuntimeManager()
        assert manager.remove_runtime("unknown") is False

    def test_copilot_npm_success(self):
        manager = RuntimeManager()
        with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
            proc = MagicMock()
            proc.returncode = 0
            mock_run.return_value = proc
            assert manager.remove_runtime("copilot") is True
        mock_run.assert_called_once_with(
            ["npm", "uninstall", "-g", "@github/copilot"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_copilot_npm_failure(self):
        manager = RuntimeManager()
        with patch("apm_cli.runtime.manager.subprocess.run") as mock_run:
            proc = MagicMock()
            proc.returncode = 1
            proc.stderr = "error"
            mock_run.return_value = proc
            assert manager.remove_runtime("copilot") is False

    def test_copilot_npm_exception(self):
        manager = RuntimeManager()
        with patch(
            "apm_cli.runtime.manager.subprocess.run",
            side_effect=Exception("npm not found"),
        ):
            assert manager.remove_runtime("copilot") is False

    def test_binary_not_installed_returns_false(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        assert manager.remove_runtime("codex") is False

    def test_remove_binary_file_success(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "codex"
        binary.write_text("fake")
        assert manager.remove_runtime("codex") is True
        assert not binary.exists()

    def test_remove_binary_dir_success(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary_dir = tmp_path / "codex"
        binary_dir.mkdir()
        assert manager.remove_runtime("codex") is True
        assert not binary_dir.exists()

    def test_remove_llm_also_removes_venv(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "llm"
        binary.write_text("fake")
        venv = tmp_path / "llm-venv"
        venv.mkdir()
        assert manager.remove_runtime("llm") is True
        assert not binary.exists()
        assert not venv.exists()

    def test_remove_llm_no_venv_still_succeeds(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "llm"
        binary.write_text("fake")
        assert manager.remove_runtime("llm") is True

    def test_remove_exception_returns_false(self, tmp_path):
        manager = RuntimeManager()
        manager.runtime_dir = tmp_path
        binary = tmp_path / "codex"
        binary.write_text("fake")
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            assert manager.remove_runtime("codex") is False


class TestRuntimeManagerGetEmbeddedScript:
    def test_dev_script_found(self, tmp_path):
        """Script loading works when repo script exists on disk."""
        manager = RuntimeManager()
        # Script search walks up from __file__ 4 levels then into scripts/runtime/
        # Create a fake script where the code looks for it
        current_file = Path(__file__)
        # We just check that when a script is found it returns its content
        with patch("apm_cli.runtime.manager.Path") as MockPath:
            fake_script = MagicMock()
            fake_script.exists.return_value = True
            fake_script.read_text.return_value = "#!/bin/bash\necho hello"
            # Set up the path chain
            instance = MagicMock()
            instance.__truediv__ = MagicMock(return_value=fake_script)
            MockPath.return_value = instance
            MockPath.side_effect = None
            # Re-create to avoid issues with __init__
        # Simpler: patch the actual script path resolution
        manager2 = RuntimeManager()
        real_script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "runtime"
            / "setup-copilot.sh"
        )
        if real_script_path.exists():
            content = manager2.get_embedded_script("setup-copilot.sh")
            assert len(content) > 0
        else:
            with pytest.raises((FileNotFoundError, RuntimeError)):
                manager2.get_embedded_script("nonexistent-script.sh")

    def test_script_not_found_raises(self):
        manager = RuntimeManager()
        with patch("apm_cli.runtime.manager.getattr", return_value=False):
            pass
        with pytest.raises(RuntimeError, match="Could not load setup script"):
            manager.get_embedded_script("definitely-does-not-exist.sh")


# ---------------------------------------------------------------------------
# commands/runtime.py CLI integration tests
# ---------------------------------------------------------------------------


RUNTIME_MGR_PATH = "apm_cli.runtime.manager.RuntimeManager"


class TestRuntimeSetupCommand:
    def test_setup_success(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.setup_runtime.return_value = True
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["setup", "copilot"])
        assert result.exit_code == 0
        mock_mgr.setup_runtime.assert_called_once_with("copilot", None, False)

    def test_setup_failure_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.setup_runtime.return_value = False
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["setup", "codex"])
        assert result.exit_code == 1

    def test_setup_exception_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH, side_effect=RuntimeError("fail")):
            result = runner.invoke(runtime_group, ["setup", "llm"])
        assert result.exit_code == 1

    def test_setup_with_version_flag(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.setup_runtime.return_value = True
            MockMgr.return_value = mock_mgr
            result = runner.invoke(
                runtime_group, ["setup", "copilot", "--version", "2.0"]
            )
        mock_mgr.setup_runtime.assert_called_once_with("copilot", "2.0", False)

    def test_setup_with_vanilla_flag(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.setup_runtime.return_value = True
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["setup", "copilot", "--vanilla"])
        mock_mgr.setup_runtime.assert_called_once_with("copilot", None, True)

    def test_setup_invalid_runtime_name(self):
        runner = CliRunner()
        result = runner.invoke(runtime_group, ["setup", "invalid-runtime"])
        assert result.exit_code != 0


class TestRuntimeListCommand:
    def _mock_runtimes(self):
        return {
            "copilot": {
                "description": "GitHub Copilot CLI",
                "installed": True,
                "path": "/usr/bin/copilot",
                "version": "1.0",
            },
            "codex": {
                "description": "OpenAI Codex CLI",
                "installed": False,
                "path": None,
            },
            "llm": {
                "description": "LLM library",
                "installed": True,
                "path": "/usr/bin/llm",
            },
        }

    def test_list_exits_0(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.list_runtimes.return_value = self._mock_runtimes()
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["list"])
        assert result.exit_code == 0

    def test_list_calls_list_runtimes(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.list_runtimes.return_value = {}
            MockMgr.return_value = mock_mgr
            runner.invoke(runtime_group, ["list"])
        mock_mgr.list_runtimes.assert_called_once()

    def test_list_exception_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH, side_effect=RuntimeError("db error")):
            result = runner.invoke(runtime_group, ["list"])
        assert result.exit_code == 1

    def test_list_fallback_output_contains_runtimes(self):
        """Fallback (non-Rich) path shows runtime names."""
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.list_runtimes.return_value = self._mock_runtimes()
            MockMgr.return_value = mock_mgr
            with (
                patch("apm_cli.commands.runtime._get_console", return_value=None),
                patch("rich.table.Table", side_effect=ImportError),
            ):
                result = runner.invoke(runtime_group, ["list"])
        assert result.exit_code == 0
        assert "copilot" in result.output


class TestRuntimeRemoveCommand:
    def test_remove_success(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.remove_runtime.return_value = True
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["remove", "--yes", "codex"])
        assert result.exit_code == 0
        mock_mgr.remove_runtime.assert_called_once_with("codex")

    def test_remove_failure_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.remove_runtime.return_value = False
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["remove", "--yes", "llm"])
        assert result.exit_code == 1

    def test_remove_exception_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH, side_effect=RuntimeError("fail")):
            result = runner.invoke(runtime_group, ["remove", "--yes", "copilot"])
        assert result.exit_code == 1

    def test_remove_invalid_runtime_name(self):
        runner = CliRunner()
        result = runner.invoke(runtime_group, ["remove", "--yes", "bad-runtime"])
        assert result.exit_code != 0


class TestRuntimeStatusCommand:
    def test_status_with_available_runtime(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.get_available_runtime.return_value = "copilot"
            mock_mgr.get_runtime_preference.return_value = ["copilot", "codex", "llm"]
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["status"])
        assert result.exit_code == 0

    def test_status_no_runtime_available(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH) as MockMgr:
            mock_mgr = MagicMock()
            mock_mgr.get_available_runtime.return_value = None
            mock_mgr.get_runtime_preference.return_value = ["copilot", "codex", "llm"]
            MockMgr.return_value = mock_mgr
            result = runner.invoke(runtime_group, ["status"])
        assert result.exit_code == 0

    def test_status_exception_exits_1(self):
        runner = CliRunner()
        with patch(RUNTIME_MGR_PATH, side_effect=RuntimeError("fail")):
            result = runner.invoke(runtime_group, ["status"])
        assert result.exit_code == 1
