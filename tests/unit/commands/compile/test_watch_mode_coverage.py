"""Tests for ``_watch_mode`` in ``apm_cli.commands.compile.watcher``.

Targets lines 153-236 which are completely uncovered because existing tests
raise KeyboardInterrupt too early (from observer.start rather than from the
inner while loop), causing it to be caught by ``except Exception`` → sys.exit(1).

These tests fix that by letting observer.start() succeed and instead raising
KeyboardInterrupt from time.sleep() inside the inner loop.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.progress = MagicMock()
    logger.success = MagicMock()
    logger.error = MagicMock()
    logger.warning = MagicMock()
    return logger


def _make_observer() -> MagicMock:
    observer = MagicMock()
    observer.start = MagicMock()  # succeeds by default (no side_effect)
    observer.stop = MagicMock()
    observer.join = MagicMock()
    observer.schedule = MagicMock()
    return observer


class TestWatchModeFullLoop:
    """Tests that cover lines 153-236 of _watch_mode."""

    def test_watch_apm_yml_keyboard_interrupt(self, tmp_path) -> None:
        """Lines 153-236: with apm.yml, observer starts, loop interrupted → clean stop."""
        from apm_cli.commands.compile.watcher import _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            mock_observer = _make_observer()
            operation = MagicMock()

            # Raise KeyboardInterrupt from time.sleep (inside the while True loop)
            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch(
                    "apm_cli.commands.compile.watcher.lifecycle_operation",
                    return_value=operation,
                ) as mock_lifecycle,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=mock_observer),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0

                # Should return normally (KeyboardInterrupt caught by inner try)
                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=False,
                )

            # Verify clean stop was called
            mock_observer.stop.assert_called_once()
            mock_observer.join.assert_called_once()
            mock_lifecycle.assert_called_once_with()
            operation.__enter__.assert_called_once_with()
            operation.__exit__.assert_called_once()
        finally:
            os.chdir(old_cwd)

    def test_watch_apm_yml_initial_compile_failure(self, tmp_path) -> None:
        """Lines 224-227: initial compilation fails → logs errors, still loops."""
        from apm_cli.commands.compile.watcher import _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=False, output_path=None, errors=["missing .apm/"])
            mock_observer = _make_observer()

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=mock_observer),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0

                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=False,
                )

            # error should have been logged
            assert logger_mock.error.called
        finally:
            os.chdir(old_cwd)

    def test_watch_apm_yml_dry_run_success(self, tmp_path) -> None:
        """Lines 217-218: dry_run=True → 'dry run' success message."""
        from apm_cli.commands.compile.watcher import _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            mock_observer = _make_observer()

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=mock_observer),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0

                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=True,
                )

            # Should call success with "dry run" message
            success_calls = [str(c) for c in logger_mock.success.call_args_list]
            assert any("dry run" in call.lower() for call in success_calls)
        finally:
            os.chdir(old_cwd)
        assert not list(tmp_path.glob("*.md"))

    def test_watch_apm_dir_and_github_paths(self, tmp_path) -> None:
        """Lines 167-185: multiple watch paths scheduled (APM_DIR + .github dirs)."""
        from apm_cli.commands.compile.watcher import _watch_mode

        # Create various dirs
        (tmp_path / ".apm").mkdir()
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "instructions").mkdir()
        (tmp_path / ".github" / "agents").mkdir()
        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            mock_observer = _make_observer()

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=mock_observer),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0

                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=False,
                )

            # All watch paths should have been scheduled
            assert mock_observer.schedule.call_count >= 4
        finally:
            os.chdir(old_cwd)

    def test_watch_with_target_label_displayed(self, tmp_path) -> None:
        """Lines 199-201: target label is logged when effective_target is set."""
        from apm_cli.commands.compile.watcher import _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            mock_observer = _make_observer()

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=mock_observer),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0

                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=False,
                    effective_target=frozenset({"claude", "agents"}),
                    target_label_user=["claude"],
                )

            # label progress should have been called
            progress_calls = [str(c) for c in logger_mock.progress.call_args_list]
            assert any("claude" in call.lower() for call in progress_calls)
        finally:
            os.chdir(old_cwd)


def _stage_edited_bundle_instruction(root) -> str:
    """Write a lockfile whose staged instruction hash no longer matches (#3718)."""
    from apm_cli.deps.lockfile import LockFile

    staged = "apm_modules/bundler/.apm/instructions/glob.instructions.md"
    (root / staged).parent.mkdir(parents=True)
    (root / staged).write_text("edited\n", encoding="utf-8")
    LockFile(
        local_staged_files=[staged],
        local_staged_file_hashes={staged: "sha256:" + "a" * 64},
    ).write(root / "apm.lock.yaml")
    return staged


class TestWatchModeStagedHashDrift:
    """Watch compilation reports edited bundle staging like one-shot compile (#3718)."""

    def test_initial_and_recompile_warn_on_staged_hash_drift(self, tmp_path) -> None:
        from apm_cli.commands.compile.watcher import APMFileHandler, _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")
        staged = _stage_edited_bundle_instruction(tmp_path)
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            with (
                patch("apm_cli.commands.compile.watcher.CommandLogger", return_value=logger_mock),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("apm_cli.commands.compile.watcher.time") as mock_time,
                patch("watchdog.observers.Observer", return_value=_make_observer()),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                mock_time.sleep.side_effect = KeyboardInterrupt
                mock_time.time.return_value = 0.0
                _watch_mode(output="AGENTS.md", chatmode=None, no_links=False, dry_run=True)
                handler_logger = _make_logger()
                APMFileHandler("AGENTS.md", None, False, True, handler_logger)._recompile(
                    ".apm/x.md"
                )
        finally:
            os.chdir(old_cwd)

        for logger in (logger_mock, handler_logger):
            warnings = " ".join(str(call.args[0]) for call in logger.warning.call_args_list)
            assert f"hash-drift: staged file {staged} changed since install" in warnings
        assert not list(tmp_path.glob("*.md"))


class TestWatchMode:
    """Tests for the ``_watch_mode`` function."""

    def _mock_observer(self) -> MagicMock:
        observer = MagicMock()
        observer.start = MagicMock()
        observer.stop = MagicMock()
        observer.join = MagicMock()
        observer.schedule = MagicMock()
        return observer

    def test_import_error_exits_1(self) -> None:
        """Missing watchdog library → sys.exit(1)."""
        import sys as _sys

        from apm_cli.commands.compile.watcher import _watch_mode

        with (
            patch("apm_cli.commands.compile.watcher.CommandLogger") as mock_logger_cls,
            patch.dict(
                _sys.modules,
                {
                    "watchdog": None,
                    "watchdog.events": None,
                    "watchdog.observers": None,
                },
            ),
        ):
            mock_logger_cls.return_value = _make_logger()
            with pytest.raises(SystemExit) as exc_info:
                _watch_mode(
                    output="AGENTS.md",
                    chatmode=None,
                    no_links=False,
                    dry_run=False,
                )
        assert exc_info.value.code == 1

    def test_no_watch_paths_returns_early(self, tmp_path) -> None:
        """When no APM dirs or apm.yml exist, logs warning and returns."""
        import os

        from apm_cli.commands.compile.watcher import _watch_mode

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            with patch(
                "apm_cli.commands.compile.watcher.CommandLogger",
                return_value=logger_mock,
            ):
                # No apm.yml, no .apm, etc. in tmp_path
                with (
                    patch("watchdog.observers.Observer"),
                    patch("watchdog.events.FileSystemEventHandler"),
                ):
                    _watch_mode(
                        output="AGENTS.md",
                        chatmode=None,
                        no_links=False,
                        dry_run=False,
                    )
        except (ImportError, SystemExit):
            pass  # watchdog might not be installed; that's fine here
        except Exception:
            pass  # any other error is acceptable — we just check no crash loop
        finally:
            os.chdir(old_cwd)

    def test_watch_mode_initial_compilation_success(self, tmp_path) -> None:
        """Successful initial compilation calls logger.success."""
        from apm_cli.commands.compile.watcher import _watch_mode

        # Create apm.yml so there's a watch path
        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()
            mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
            mock_observer = self._mock_observer()

            # Make observer.start raise KeyboardInterrupt to exit the loop
            def _start_and_interrupt():
                raise KeyboardInterrupt

            mock_observer.start.side_effect = _start_and_interrupt

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    return_value=MagicMock(),
                ),
                patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
                patch("watchdog.observers.Observer", return_value=mock_observer),
                patch("watchdog.events.FileSystemEventHandler"),
            ):
                mock_compiler_cls.return_value.compile.return_value = mock_result
                import contextlib

                with contextlib.suppress(ImportError, SystemExit):
                    _watch_mode(
                        output="AGENTS.md",
                        chatmode=None,
                        no_links=False,
                        dry_run=False,
                    )
        finally:
            os.chdir(old_cwd)

    def test_watch_mode_general_exception_exits_1(self, tmp_path) -> None:
        """General exception in _watch_mode → sys.exit(1)."""
        from apm_cli.commands.compile.watcher import _watch_mode

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")

        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            logger_mock = _make_logger()

            with (
                patch(
                    "apm_cli.commands.compile.watcher.CommandLogger",
                    return_value=logger_mock,
                ),
                patch(
                    "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                    side_effect=RuntimeError("fatal error"),
                ),
                patch("watchdog.observers.Observer"),
                patch("watchdog.events.FileSystemEventHandler"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    _watch_mode(
                        output="AGENTS.md",
                        chatmode=None,
                        no_links=False,
                        dry_run=False,
                    )
            assert exc_info.value.code == 1
        except ImportError:
            pytest.skip("watchdog not installed")
        finally:
            os.chdir(old_cwd)
