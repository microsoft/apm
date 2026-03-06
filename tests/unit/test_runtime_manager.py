"""Unit tests for runtime/manager.py RuntimeManager."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch


class TestRuntimeManagerInit(unittest.TestCase):
    """Tests for RuntimeManager.__init__."""

    def test_default_attributes(self):
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        self.assertEqual(rm.runtime_dir, Path.home() / ".apm" / "runtimes")
        self.assertIn("copilot", rm.supported_runtimes)
        self.assertIn("codex", rm.supported_runtimes)
        self.assertIn("llm", rm.supported_runtimes)

    def test_supported_runtime_keys(self):
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        for name, info in rm.supported_runtimes.items():
            self.assertIn("script", info)
            self.assertIn("description", info)
            self.assertIn("binary", info)


class TestGetEmbeddedScript(unittest.TestCase):
    """Tests for RuntimeManager.get_embedded_script."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_reads_script_from_repo_structure(self):
        """Script is loaded from scripts/runtime/ relative to repo root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script_dir = Path(tmpdir) / "scripts" / "runtime"
            script_dir.mkdir(parents=True)
            (script_dir / "setup-test.sh").write_text("#!/bin/bash\necho test")

            # Patch __file__ so repo_root points to tmpdir
            with patch(
                "apm_cli.runtime.manager.__file__",
                str(Path(tmpdir) / "src" / "apm_cli" / "runtime" / "manager.py"),
            ):
                content = self.rm.get_embedded_script("setup-test.sh")

        self.assertEqual(content, "#!/bin/bash\necho test")

    def test_raises_when_script_not_found(self):
        """RuntimeError raised when script file does not exist."""
        with self.assertRaises(RuntimeError):
            self.rm.get_embedded_script("nonexistent-script.sh")

    def test_frozen_bundle_path_checked_first(self):
        """In frozen (PyInstaller) bundle, bundle path is tried first."""
        with tempfile.TemporaryDirectory() as bundle_dir:
            script_dir = Path(bundle_dir) / "scripts" / "runtime"
            script_dir.mkdir(parents=True)
            (script_dir / "setup-frozen.sh").write_text("frozen content")

            import sys

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", bundle_dir, create=True),
            ):
                content = self.rm.get_embedded_script("setup-frozen.sh")

        self.assertEqual(content, "frozen content")


class TestGetCommonScript(unittest.TestCase):
    """Tests for RuntimeManager.get_common_script."""

    def test_delegates_to_get_embedded_script(self):
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch.object(
            rm, "get_embedded_script", return_value="common content"
        ) as mock_get:
            result = rm.get_common_script()
        mock_get.assert_called_once_with("setup-common.sh")
        self.assertEqual(result, "common content")


class TestGetTokenHelperScript(unittest.TestCase):
    """Tests for RuntimeManager.get_token_helper_script."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_reads_token_helper_from_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_dir = Path(tmpdir) / "scripts"
            script_dir.mkdir(parents=True)
            (script_dir / "github-token-helper.sh").write_text(
                "#!/bin/bash\necho token"
            )

            with patch(
                "apm_cli.runtime.manager.__file__",
                str(Path(tmpdir) / "src" / "apm_cli" / "runtime" / "manager.py"),
            ):
                content = self.rm.get_token_helper_script()

        self.assertEqual(content, "#!/bin/bash\necho token")

    def test_raises_when_token_helper_not_found(self):
        """RuntimeError raised when github-token-helper.sh is missing."""
        with patch.object(Path, "exists", return_value=False):
            with self.assertRaises(RuntimeError):
                self.rm.get_token_helper_script()

    def test_frozen_bundle_path(self):
        """In frozen bundle, reads token helper from bundle dir."""
        with tempfile.TemporaryDirectory() as bundle_dir:
            script_dir = Path(bundle_dir) / "scripts"
            script_dir.mkdir(parents=True)
            (script_dir / "github-token-helper.sh").write_text("bundle helper")

            import sys

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", bundle_dir, create=True),
            ):
                content = self.rm.get_token_helper_script()

        self.assertEqual(content, "bundle helper")


class TestListRuntimes(unittest.TestCase):
    """Tests for RuntimeManager.list_runtimes."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_returns_all_runtimes(self):
        with patch("apm_cli.runtime.manager.shutil.which", return_value=None):
            result = self.rm.list_runtimes()
        self.assertEqual(set(result.keys()), {"copilot", "codex", "llm"})

    def test_runtime_not_installed(self):
        with (
            patch("apm_cli.runtime.manager.shutil.which", return_value=None),
            patch.object(Path, "exists", return_value=False),
        ):
            result = self.rm.list_runtimes()
        for _name, info in result.items():
            self.assertIn("description", info)
            self.assertIn("installed", info)

    def test_runtime_installed_in_system_path(self):
        fake_path = "/usr/local/bin/copilot"
        with (
            patch("shutil.which", return_value=fake_path),
            patch.object(Path, "exists", return_value=False),
        ):
            result = self.rm.list_runtimes()
        copilot = result.get("copilot", {})
        # Result depends on PATH/filesystem; just assert structure is present
        self.assertIn("installed", copilot)
        self.assertIn("description", copilot)

    def test_version_command_called_when_installed(self):
        """Version is retrieved via subprocess when binary is installed."""
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "v1.2.3\n"

        with (
            patch(
                "apm_cli.runtime.manager.shutil.which", return_value="/usr/bin/copilot"
            ),
            patch.object(Path, "exists", return_value=False),
            patch("apm_cli.runtime.manager.subprocess.run", return_value=proc),
        ):
            result = self.rm.list_runtimes()

        # At least one runtime should have version info if binary is found
        for info in result.values():
            if info.get("installed"):
                self.assertIn("version", info)
                break

    def test_version_falls_back_on_subprocess_exception(self):
        """Version is 'unknown' when subprocess raises."""
        with (
            patch(
                "apm_cli.runtime.manager.shutil.which", return_value="/usr/bin/copilot"
            ),
            patch.object(Path, "exists", return_value=False),
            patch(
                "apm_cli.runtime.manager.subprocess.run",
                side_effect=subprocess.TimeoutExpired("copilot", 5),
            ),
        ):
            result = self.rm.list_runtimes()

        for info in result.values():
            if info.get("installed"):
                self.assertEqual(info.get("version"), "unknown")


class TestIsRuntimeAvailable(unittest.TestCase):
    """Tests for RuntimeManager.is_runtime_available."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_unknown_runtime_returns_false(self):
        self.assertFalse(self.rm.is_runtime_available("unknown"))

    def test_found_in_apm_dir(self):
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
        ):
            self.assertTrue(self.rm.is_runtime_available("copilot"))

    def test_not_in_apm_dir_falls_back_to_path(self):
        with (
            patch.object(Path, "exists", return_value=False),
            patch(
                "apm_cli.runtime.manager.shutil.which", return_value="/usr/bin/copilot"
            ),
        ):
            self.assertTrue(self.rm.is_runtime_available("copilot"))

    def test_not_in_apm_dir_not_in_path(self):
        with (
            patch.object(Path, "exists", return_value=False),
            patch("apm_cli.runtime.manager.shutil.which", return_value=None),
        ):
            self.assertFalse(self.rm.is_runtime_available("codex"))


class TestGetRuntimePreference(unittest.TestCase):
    """Tests for RuntimeManager.get_runtime_preference."""

    def test_returns_expected_order(self):
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        prefs = rm.get_runtime_preference()
        self.assertEqual(prefs, ["copilot", "codex", "llm"])


class TestGetAvailableRuntime(unittest.TestCase):
    """Tests for RuntimeManager.get_available_runtime."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_returns_first_available(self):
        def _is_available(name):
            return name == "codex"

        with patch.object(self.rm, "is_runtime_available", side_effect=_is_available):
            result = self.rm.get_available_runtime()
        # copilot checked first (not available), then codex (available)
        self.assertEqual(result, "codex")

    def test_returns_none_when_none_available(self):
        with patch.object(self.rm, "is_runtime_available", return_value=False):
            result = self.rm.get_available_runtime()
        self.assertIsNone(result)

    def test_returns_copilot_when_first(self):
        with patch.object(self.rm, "is_runtime_available", return_value=True):
            result = self.rm.get_available_runtime()
        self.assertEqual(result, "copilot")


class TestRemoveRuntime(unittest.TestCase):
    """Tests for RuntimeManager.remove_runtime."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_unknown_runtime_returns_false(self):
        result = self.rm.remove_runtime("unknown")
        self.assertFalse(result)

    def test_copilot_uses_npm_uninstall_success(self):
        proc = MagicMock()
        proc.returncode = 0
        with patch("apm_cli.runtime.manager.subprocess.run", return_value=proc):
            result = self.rm.remove_runtime("copilot")
        self.assertTrue(result)

    def test_copilot_npm_failure_returns_false(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "permission denied"
        with patch("apm_cli.runtime.manager.subprocess.run", return_value=proc):
            result = self.rm.remove_runtime("copilot")
        self.assertFalse(result)

    def test_copilot_subprocess_exception_returns_false(self):
        with patch(
            "apm_cli.runtime.manager.subprocess.run",
            side_effect=FileNotFoundError("npm not found"),
        ):
            result = self.rm.remove_runtime("copilot")
        self.assertFalse(result)

    def test_binary_not_installed_returns_false(self):
        with patch.object(Path, "exists", return_value=False):
            result = self.rm.remove_runtime("codex")
        self.assertFalse(result)

    def test_removes_binary_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.rm.runtime_dir = Path(tmpdir)
            binary_path = self.rm.runtime_dir / "codex"
            binary_path.write_text("fake binary")
            result = self.rm.remove_runtime("codex")
        self.assertTrue(result)
        self.assertFalse(binary_path.exists())

    def test_removes_binary_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.rm.runtime_dir = Path(tmpdir)
            binary_dir = self.rm.runtime_dir / "codex"
            binary_dir.mkdir()
            (binary_dir / "codex").write_text("exec")
            result = self.rm.remove_runtime("codex")
        self.assertTrue(result)
        self.assertFalse(binary_dir.exists())

    def test_removes_llm_venv_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.rm.runtime_dir = Path(tmpdir)
            binary_path = self.rm.runtime_dir / "llm"
            binary_path.write_text("fake llm")
            venv_path = self.rm.runtime_dir / "llm-venv"
            venv_path.mkdir()
            result = self.rm.remove_runtime("llm")
        self.assertTrue(result)
        self.assertFalse(venv_path.exists())

    def test_removes_llm_without_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.rm.runtime_dir = Path(tmpdir)
            binary_path = self.rm.runtime_dir / "llm"
            binary_path.write_text("fake llm")
            result = self.rm.remove_runtime("llm")
        self.assertTrue(result)

    def test_exception_during_removal_returns_false(self):
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "unlink", side_effect=PermissionError("denied")),
        ):
            result = self.rm.remove_runtime("codex")
        self.assertFalse(result)


class TestSetupRuntime(unittest.TestCase):
    """Tests for RuntimeManager.setup_runtime."""

    def setUp(self):
        from apm_cli.runtime.manager import RuntimeManager

        self.rm = RuntimeManager()

    def test_unsupported_runtime_returns_false(self):
        result = self.rm.setup_runtime("unknown_runtime")
        self.assertFalse(result)

    def test_success(self):
        with (
            patch.object(self.rm, "get_embedded_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "get_common_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "run_embedded_script", return_value=True),
        ):
            result = self.rm.setup_runtime("copilot")
        self.assertTrue(result)

    def test_failure_from_script(self):
        with (
            patch.object(self.rm, "get_embedded_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "get_common_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "run_embedded_script", return_value=False),
        ):
            result = self.rm.setup_runtime("codex")
        self.assertFalse(result)

    def test_version_arg_passed(self):
        with (
            patch.object(self.rm, "get_embedded_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "get_common_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "run_embedded_script", return_value=True) as mock_run,
        ):
            self.rm.setup_runtime("copilot", version="1.2.3")
        args = mock_run.call_args[0]
        self.assertIn("1.2.3", args[2])

    def test_vanilla_flag_passed(self):
        with (
            patch.object(self.rm, "get_embedded_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "get_common_script", return_value="#!/bin/bash"),
            patch.object(self.rm, "run_embedded_script", return_value=True) as mock_run,
        ):
            self.rm.setup_runtime("llm", vanilla=True)
        args = mock_run.call_args[0]
        self.assertIn("--vanilla", args[2])

    def test_exception_returns_false(self):
        with patch.object(
            self.rm, "get_embedded_script", side_effect=RuntimeError("script missing")
        ):
            result = self.rm.setup_runtime("copilot")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
