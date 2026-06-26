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

    def test_watch_apm_dir_and_github_paths(self, tmp_path) -> None:
        """Lines 167-185: multiple watch paths scheduled (APM_DIR + .github dirs)."""
        from apm_cli.commands.compile.watcher import _watch_mode

        # Create various dirs
        (tmp_path / ".apm").mkdir()
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "instructions").mkdir()
        (tmp_path / ".github" / "agents").mkdir()
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
            assert mock_observer.schedule.call_count >= 5
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
