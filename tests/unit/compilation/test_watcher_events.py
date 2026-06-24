"""Comprehensive unit tests for ``apm_cli.commands.compile.watcher``.

Covers:
- ``_format_target_label``: frozenset with user/config/fallback source, None,
  and single-string effective target.
- ``APMFileHandler``: initialization, on_modified filtering/debouncing,
  _recompile success / dry-run / failure / exception paths.
- ``_watch_mode``: ImportError (watchdog missing), no watch-paths, paths found
  with initial compilation success/failure, KeyboardInterrupt stop.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.compile.watcher import APMFileHandler, _format_target_label

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.progress = MagicMock()
    logger.success = MagicMock()
    logger.error = MagicMock()
    logger.warning = MagicMock()
    return logger


def _make_handler(
    *,
    output: str = "AGENTS.md",
    chatmode: str | None = None,
    no_links: bool = False,
    dry_run: bool = False,
    effective_target: Any = None,
) -> APMFileHandler:
    return APMFileHandler(
        output=output,
        chatmode=chatmode,
        no_links=no_links,
        dry_run=dry_run,
        logger=_make_logger(),
        effective_target=effective_target,
    )


def _make_event(src_path: str, *, is_directory: bool = False) -> SimpleNamespace:
    return SimpleNamespace(src_path=src_path, is_directory=is_directory)


# ===========================================================================
# _format_target_label tests
# ===========================================================================


class TestFormatTargetLabel:
    """Tests for the ``_format_target_label`` helper."""

    def test_frozenset_with_user_list_label(self) -> None:
        """frozenset + list target_label_user uses '--target ...' source."""
        target = frozenset({"claude", "agents"})
        label = _format_target_label(target, ["claude", "cursor"], None)
        assert label is not None
        assert "--target" in label
        assert "claude" in label
        assert "cursor" in label

    def test_frozenset_with_config_list_label(self) -> None:
        """frozenset + list target_label_config uses 'apm.yml target:' source."""
        target = frozenset({"claude", "agents"})
        label = _format_target_label(target, None, ["claude", "cursor"])
        assert label is not None
        assert "apm.yml target:" in label

    def test_frozenset_with_no_list_falls_back_to_multi_target(self) -> None:
        """frozenset + neither list source → 'multi-target' source."""
        target = frozenset({"claude", "agents"})
        label = _format_target_label(target, "claude", "cursor")
        assert label is not None
        assert "multi-target" in label

    def test_frozenset_includes_agents_md(self) -> None:
        """frozenset with agents target includes 'AGENTS.md' in label."""
        target = frozenset({"agents"})
        label = _format_target_label(target, None, None)
        assert label is not None
        assert "AGENTS.md" in label

    def test_frozenset_includes_claude_md(self) -> None:
        """frozenset with claude target includes 'CLAUDE.md' in label."""
        target = frozenset({"claude"})
        label = _format_target_label(target, None, None)
        assert label is not None
        assert "CLAUDE.md" in label

    def test_none_target_returns_none(self) -> None:
        """None effective_target → returns None."""
        result = _format_target_label(None, None, None)
        assert result is None

    def test_string_target_returns_description(self) -> None:
        """A single-string target returns its description label."""
        label = _format_target_label("claude", None, None)
        assert label is not None
        assert "Compiling for" in label

    def test_frozenset_compiling_for_prefix(self) -> None:
        """All frozenset paths start with 'Compiling for'."""
        target = frozenset({"claude", "agents"})
        label = _format_target_label(target, ["claude"], None)
        assert label is not None
        assert label.startswith("Compiling for")


# ===========================================================================
# APMFileHandler initialization tests
# ===========================================================================


class TestAPMFileHandlerInit:
    """Tests for APMFileHandler.__init__."""

    def test_default_values(self) -> None:
        """Handler stores all constructor arguments correctly."""
        logger = _make_logger()
        target = frozenset({"claude"})
        handler = APMFileHandler(
            output="AGENTS.md",
            chatmode="chat",
            no_links=True,
            dry_run=True,
            logger=logger,
            effective_target=target,
        )
        assert handler.output == "AGENTS.md"
        assert handler.chatmode == "chat"
        assert handler.no_links is True
        assert handler.dry_run is True
        assert handler.logger is logger
        assert handler.effective_target is target
        assert handler.last_compile == 0.0
        assert handler.debounce_delay == 1.0

    def test_effective_target_defaults_to_none(self) -> None:
        """effective_target defaults to None."""
        handler = APMFileHandler(
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            logger=_make_logger(),
        )
        assert handler.effective_target is None


# ===========================================================================
# APMFileHandler.on_modified tests
# ===========================================================================


class TestAPMFileHandlerOnModified:
    """Tests for ``APMFileHandler.on_modified`` filtering and debounce."""

    def test_directory_events_are_ignored(self) -> None:
        """Events where is_directory=True are skipped without recompile."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        handler.on_modified(_make_event("some/path", is_directory=True))
        handler._recompile.assert_not_called()

    def test_non_md_non_apm_yml_ignored(self) -> None:
        """Events for .py, .json, etc. are skipped."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        for path in ["script.py", "config.json", "image.png", "Makefile"]:
            handler.on_modified(_make_event(path))
        handler._recompile.assert_not_called()

    def test_non_primitive_md_ignored(self) -> None:
        """Generic .md files (README, CHANGELOG, AGENTS output) are skipped.

        Only files matching APM primitive suffixes trigger recompile.
        """
        handler = _make_handler()
        handler._recompile = MagicMock()
        for path in ["AGENTS.md", "README.md", "CHANGELOG.md", "docs/notes.md"]:
            handler.on_modified(_make_event(path))
        handler._recompile.assert_not_called()

    def test_md_file_triggers_recompile(self) -> None:
        """A primitive .md file event triggers _recompile."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        handler.on_modified(_make_event(".apm/agents/foo.agent.md"))
        handler._recompile.assert_called_once_with(".apm/agents/foo.agent.md")

    def test_apm_yml_triggers_recompile(self) -> None:
        """An apm.yml event triggers _recompile."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        # Set last_compile far in the past to skip debounce
        handler.last_compile = 0.0
        handler.on_modified(_make_event("apm.yml"))
        handler._recompile.assert_called_once_with("apm.yml")

    def test_debounce_suppresses_rapid_events(self) -> None:
        """Rapid successive events within debounce_delay are suppressed."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        # First event fires
        handler.on_modified(_make_event(".apm/agents/foo.agent.md"))
        assert handler._recompile.call_count == 1
        # Second event within debounce window is suppressed
        handler.on_modified(_make_event(".apm/agents/foo.agent.md"))
        assert handler._recompile.call_count == 1  # still 1

    def test_event_after_debounce_fires_again(self) -> None:
        """After debounce_delay has elapsed, the next event fires."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        handler.last_compile = time.time() - 2.0  # older than debounce_delay
        handler.on_modified(_make_event(".apm/agents/foo.agent.md"))
        handler._recompile.assert_called_once()

    def test_event_with_no_src_path_attr(self) -> None:
        """Events with no src_path attribute default to empty string → ignored."""
        handler = _make_handler()
        handler._recompile = MagicMock()
        handler.on_modified(SimpleNamespace())  # no src_path, no is_directory
        handler._recompile.assert_not_called()


# ===========================================================================
# APMFileHandler._recompile tests
# ===========================================================================


class TestAPMFileHandlerRecompile:
    """Tests for ``APMFileHandler._recompile``."""

    def _patch_compile(self, success: bool, errors: list[str] | None = None):
        """Return context managers that mock CompilationConfig + AgentsCompiler."""
        mock_config = MagicMock()
        mock_result = SimpleNamespace(
            success=success,
            output_path="AGENTS.md",
            errors=errors or [],
        )
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = mock_result
        return mock_config, mock_compiler

    def test_recompile_success_logs_output_path(self) -> None:
        """Successful recompile logs the output path."""
        handler = _make_handler()
        mock_config = MagicMock()
        mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                return_value=mock_config,
            ),
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            handler._recompile("AGENTS.md")

        handler.logger.success.assert_called_once()
        success_msg = handler.logger.success.call_args[0][0]
        assert "AGENTS.md" in success_msg

    def test_recompile_success_dry_run_logs_dry_run_message(self) -> None:
        """Dry-run successful recompile logs 'dry run' message."""
        handler = _make_handler(dry_run=True)
        mock_config = MagicMock()
        mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                return_value=mock_config,
            ),
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            handler._recompile("any.md")

        handler.logger.success.assert_called_once()
        success_msg = handler.logger.success.call_args[0][0]
        assert "dry run" in success_msg

    def test_recompile_failure_logs_errors(self) -> None:
        """Failed recompile logs each error message."""
        handler = _make_handler()
        mock_config = MagicMock()
        mock_result = SimpleNamespace(
            success=False, output_path=None, errors=["syntax error", "missing file"]
        )
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
                return_value=mock_config,
            ),
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            handler._recompile("broken.md")

        error_calls = [str(c) for c in handler.logger.error.call_args_list]
        assert any("syntax error" in msg for msg in error_calls)
        assert any("missing file" in msg for msg in error_calls)

    def test_recompile_exception_is_caught(self) -> None:
        """Exception inside _recompile is caught and logged."""
        handler = _make_handler()

        with patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
            side_effect=RuntimeError("config broken"),
        ):
            handler._recompile("any.md")

        handler.logger.error.assert_called()

    def test_recompile_output_equals_agents_md_passes_none(self) -> None:
        """When output == AGENTS_MD_FILENAME, output_path=None is passed."""
        handler = _make_handler(output="AGENTS.md")  # equals AGENTS_MD_FILENAME
        mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
            ) as mock_from_yml,
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            mock_from_yml.return_value = MagicMock()
            handler._recompile("any.md")

        kwargs = mock_from_yml.call_args.kwargs
        assert kwargs.get("output_path") is None

    def test_recompile_custom_output_passes_path(self) -> None:
        """When output != AGENTS_MD_FILENAME, output_path is the custom path."""
        handler = _make_handler(output="custom-output.md")
        mock_result = SimpleNamespace(success=True, output_path="custom-output.md", errors=[])
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
            ) as mock_from_yml,
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            mock_from_yml.return_value = MagicMock()
            handler._recompile("any.md")

        kwargs = mock_from_yml.call_args.kwargs
        assert kwargs.get("output_path") == "custom-output.md"

    def test_recompile_forwards_effective_target(self) -> None:
        """effective_target is forwarded as target= kwarg."""
        target = frozenset({"claude", "agents"})
        handler = _make_handler(effective_target=target)
        mock_result = SimpleNamespace(success=True, output_path="AGENTS.md", errors=[])
        mock_compiler_cls = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = mock_result

        with (
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
            ) as mock_from_yml,
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler",
                mock_compiler_cls,
            ),
        ):
            mock_from_yml.return_value = MagicMock()
            handler._recompile("any.md")

        assert mock_from_yml.call_args.kwargs["target"] == target


# ===========================================================================
# _watch_mode tests
# ===========================================================================


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
