"""Unit tests for apm_cli.utils.console."""

import importlib
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestStatusSymbols:
    """Tests for STATUS_SYMBOLS dictionary."""

    def test_required_keys_present(self):
        from apm_cli.utils.console import STATUS_SYMBOLS

        required = [
            "success",
            "sparkles",
            "running",
            "gear",
            "info",
            "warning",
            "error",
            "check",
            "cross",
            "list",
            "preview",
            "robot",
            "metrics",
            "default",
        ]
        for key in required:
            assert key in STATUS_SYMBOLS, f"Missing key: {key}"

    def test_all_values_are_strings(self):
        from apm_cli.utils.console import STATUS_SYMBOLS

        for k, v in STATUS_SYMBOLS.items():
            assert isinstance(v, str), f"Symbol for '{k}' is not a string"


class TestGetConsole:
    """Tests for _get_console()."""

    def setup_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def teardown_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def test_returns_console_when_rich_available(self):
        from apm_cli.utils.console import _get_console

        result = _get_console()
        # Rich is installed in test environment, so we expect a Console object.
        assert result is not None

    def test_returns_none_when_console_raises(self):
        from apm_cli.utils.console import _get_console

        with patch("apm_cli.utils.console.Console", side_effect=Exception("fail")):
            result = _get_console()
        assert result is None

    def test_returns_none_when_rich_unavailable(self):
        from apm_cli.utils.console import _get_console

        with patch("apm_cli.utils.console.RICH_AVAILABLE", False):
            result = _get_console()
        assert result is None


class TestRichEcho:
    """Tests for _rich_echo()."""

    def test_basic_message_via_rich(self, capsys):
        from apm_cli.utils.console import _rich_echo

        # Rich is available; no exception expected.
        _rich_echo("hello world")
        # Just ensure no crash; Rich writes to its own stream.

    def test_style_param_used_as_color(self, capsys):
        """style= parameter is a backward-compat alias for color=."""
        from apm_cli.utils.console import _rich_echo

        mock_console = MagicMock()
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_echo("msg", style="cyan")
        mock_console.print.assert_called_once()
        _, kwargs = mock_console.print.call_args
        assert kwargs.get("style") == "cyan"

    def test_symbol_prepended(self, capsys):
        from apm_cli.utils.console import STATUS_SYMBOLS, _rich_echo

        mock_console = MagicMock()
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_echo("test msg", symbol="info")
        args, _ = mock_console.print.call_args
        assert STATUS_SYMBOLS["info"] in args[0]
        assert "test msg" in args[0]

    def test_unknown_symbol_ignored(self):
        from apm_cli.utils.console import _rich_echo

        mock_console = MagicMock()
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_echo("msg", symbol="nonexistent_symbol_xyz")
        args, _ = mock_console.print.call_args
        assert args[0] == "msg"

    def test_bold_flag(self):
        from apm_cli.utils.console import _rich_echo

        mock_console = MagicMock()
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_echo("msg", color="green", bold=True)
        _, kwargs = mock_console.print.call_args
        assert kwargs.get("style") == "bold green"

    def test_colorama_fallback_when_no_console(self, capsys):
        """Falls back to colorama when console is None."""
        from apm_cli.utils.console import _rich_echo

        mock_fore = MagicMock()
        mock_fore.RED = "\033[31m"
        mock_fore.GREEN = "\033[32m"
        mock_fore.YELLOW = "\033[33m"
        mock_fore.BLUE = "\033[34m"
        mock_fore.CYAN = "\033[36m"
        mock_fore.WHITE = "\033[37m"
        mock_fore.MAGENTA = "\033[35m"
        mock_style = MagicMock()
        mock_style.BRIGHT = "\033[1m"
        mock_style.RESET_ALL = "\033[0m"

        with (
            patch("apm_cli.utils.console._get_console", return_value=None),
            patch("apm_cli.utils.console.COLORAMA_AVAILABLE", True),
            patch("apm_cli.utils.console.Fore", mock_fore),
            patch("apm_cli.utils.console.Style", mock_style),
        ):
            _rich_echo("colorama msg", color="red")

        captured = capsys.readouterr()
        assert "colorama msg" in captured.out

    def test_colorama_fallback_bold(self, capsys):
        from apm_cli.utils.console import _rich_echo

        mock_fore = MagicMock()
        mock_fore.GREEN = "\033[32m"
        mock_fore.WHITE = "\033[37m"
        mock_style = MagicMock()
        mock_style.BRIGHT = "\033[1m"
        mock_style.RESET_ALL = "\033[0m"

        with (
            patch("apm_cli.utils.console._get_console", return_value=None),
            patch("apm_cli.utils.console.COLORAMA_AVAILABLE", True),
            patch("apm_cli.utils.console.Fore", mock_fore),
            patch("apm_cli.utils.console.Style", mock_style),
        ):
            _rich_echo("bold msg", color="green", bold=True)

        captured = capsys.readouterr()
        assert "bold msg" in captured.out

    def test_colorama_fallback_unknown_color(self, capsys):
        from apm_cli.utils.console import _rich_echo

        mock_fore = MagicMock()
        mock_fore.WHITE = "\033[37m"
        mock_style = MagicMock()
        mock_style.BRIGHT = "\033[1m"
        mock_style.RESET_ALL = "\033[0m"

        with (
            patch("apm_cli.utils.console._get_console", return_value=None),
            patch("apm_cli.utils.console.COLORAMA_AVAILABLE", True),
            patch("apm_cli.utils.console.Fore", mock_fore),
            patch("apm_cli.utils.console.Style", mock_style),
        ):
            _rich_echo("msg", color="unknown_color_xyz")

        captured = capsys.readouterr()
        assert "msg" in captured.out

    def test_plain_fallback_when_no_rich_and_no_colorama(self, capsys):
        from apm_cli.utils.console import _rich_echo

        with (
            patch("apm_cli.utils.console._get_console", return_value=None),
            patch("apm_cli.utils.console.COLORAMA_AVAILABLE", False),
        ):
            _rich_echo("plain msg")

        captured = capsys.readouterr()
        assert "plain msg" in captured.out

    def test_console_print_exception_falls_through_to_colorama(self, capsys):
        from apm_cli.utils.console import _rich_echo

        mock_console = MagicMock()
        mock_console.print.side_effect = Exception("render error")

        mock_fore = MagicMock()
        mock_fore.BLUE = "\033[34m"
        mock_fore.WHITE = "\033[37m"
        mock_style = MagicMock()
        mock_style.BRIGHT = "\033[1m"
        mock_style.RESET_ALL = "\033[0m"

        with (
            patch("apm_cli.utils.console._get_console", return_value=mock_console),
            patch("apm_cli.utils.console.COLORAMA_AVAILABLE", True),
            patch("apm_cli.utils.console.Fore", mock_fore),
            patch("apm_cli.utils.console.Style", mock_style),
        ):
            _rich_echo("fallback msg", color="blue")

        captured = capsys.readouterr()
        assert "fallback msg" in captured.out


class TestRichConvenienceFunctions:
    """Tests for _rich_success/error/warning/info wrappers."""

    @pytest.mark.parametrize(
        "fn_name,expected_color",
        [
            ("_rich_success", "green"),
            ("_rich_error", "red"),
            ("_rich_warning", "yellow"),
            ("_rich_info", "blue"),
        ],
    )
    def test_delegates_to_rich_echo(self, fn_name, expected_color):
        import apm_cli.utils.console as console_mod

        fn = getattr(console_mod, fn_name)
        with patch.object(console_mod, "_rich_echo") as mock_echo:
            fn("test message", symbol="info")
        mock_echo.assert_called_once()
        _, kwargs = mock_echo.call_args
        assert kwargs.get("color") == expected_color
        assert kwargs.get("symbol") == "info"

    def test_rich_success_is_bold(self):
        import apm_cli.utils.console as console_mod

        with patch.object(console_mod, "_rich_echo") as mock_echo:
            console_mod._rich_success("done")
        _, kwargs = mock_echo.call_args
        assert kwargs.get("bold") is True


class TestRichPanel:
    """Tests for _rich_panel()."""

    def test_rich_path(self):
        from apm_cli.utils.console import _rich_panel

        mock_console = MagicMock()
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_panel("content", title="Title", style="cyan")
        mock_console.print.assert_called_once()

    def test_fallback_with_title(self, capsys):
        from apm_cli.utils.console import _rich_panel

        with patch("apm_cli.utils.console._get_console", return_value=None):
            _rich_panel("some content", title="My Title")

        captured = capsys.readouterr()
        assert "My Title" in captured.out
        assert "some content" in captured.out

    def test_fallback_without_title(self, capsys):
        from apm_cli.utils.console import _rich_panel

        with patch("apm_cli.utils.console._get_console", return_value=None):
            _rich_panel("content only")

        captured = capsys.readouterr()
        assert "content only" in captured.out

    def test_panel_render_exception_falls_to_fallback(self, capsys):
        from apm_cli.utils.console import _rich_panel

        mock_console = MagicMock()
        mock_console.print.side_effect = Exception("panel crash")
        with patch("apm_cli.utils.console._get_console", return_value=mock_console):
            _rich_panel("panel content", title="Oops")

        captured = capsys.readouterr()
        assert "panel content" in captured.out


class TestCreateFilesTable:
    """Tests for _create_files_table()."""

    def test_returns_none_when_rich_unavailable(self):
        from apm_cli.utils.console import _create_files_table

        with patch("apm_cli.utils.console.RICH_AVAILABLE", False):
            result = _create_files_table([{"name": "f.md", "description": "A file"}])
        assert result is None

    def test_dict_items(self):
        from apm_cli.utils.console import _create_files_table

        result = _create_files_table(
            [{"name": "readme.md", "description": "Main readme"}],
            title="Test Files",
        )
        # Rich is available; should return a Table object (non-None)
        assert result is not None

    def test_list_tuple_items(self):
        from apm_cli.utils.console import _create_files_table

        result = _create_files_table([["script.py", "A script"], ("config.yaml", "Config")])
        assert result is not None

    def test_plain_string_items(self):
        from apm_cli.utils.console import _create_files_table

        result = _create_files_table(["just_a_filename.txt"])
        assert result is not None

    def test_returns_none_on_exception(self):
        from apm_cli.utils.console import _create_files_table

        with patch("apm_cli.utils.console.Table", side_effect=Exception("boom")):
            result = _create_files_table([{"name": "x", "description": "y"}])
        assert result is None

    def test_empty_list(self):
        from apm_cli.utils.console import _create_files_table

        result = _create_files_table([])
        assert result is not None  # Returns empty Table


class TestShowDownloadSpinner:
    """Tests for show_download_spinner context manager."""

    def test_rich_path_yields_status(self):
        from apm_cli.utils.console import show_download_spinner

        mock_status = MagicMock()
        mock_console = MagicMock()
        mock_console.status.return_value.__enter__ = MagicMock(return_value=mock_status)
        mock_console.status.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("apm_cli.utils.console._get_console", return_value=mock_console),
            patch("apm_cli.utils.console.RICH_AVAILABLE", True),
        ):
            with show_download_spinner("owner/repo") as s:
                assert s == mock_status

    def test_no_rich_fallback_yields_none(self, capsys):
        from apm_cli.utils.console import show_download_spinner

        with patch("apm_cli.utils.console._get_console", return_value=None):
            with show_download_spinner("owner/repo") as s:
                assert s is None

        captured = capsys.readouterr()
        assert "owner/repo" in captured.out

    def test_rich_exception_fallback_yields_none(self, capsys):
        from apm_cli.utils.console import show_download_spinner

        mock_console = MagicMock()
        mock_console.status.side_effect = Exception("spinner error")

        with (
            patch("apm_cli.utils.console._get_console", return_value=mock_console),
            patch("apm_cli.utils.console.RICH_AVAILABLE", True),
        ):
            with show_download_spinner("owner/repo") as s:
                assert s is None

        captured = capsys.readouterr()
        assert "owner/repo" in captured.out


# ---------------------------------------------------------------------------
# ImportError fallback branches (lines 18-23 and 31-34) reached by
# re-importing the module with the target library blocked in sys.modules.
# ---------------------------------------------------------------------------


def _reimport_console_without(blocked_modules: list[str]):
    """Re-import apm_cli.utils.console with ``blocked_modules`` blocked.

    Returns the freshly imported module, then restores everything in
    sys.modules so other tests are unaffected.
    """
    # Save and remove existing entries we will touch.
    saved: dict[str, object] = {}
    for key in list(sys.modules.keys()):
        if key == "apm_cli.utils.console" or any(
            key == m or key.startswith(m + ".") for m in blocked_modules
        ):
            saved[key] = sys.modules.pop(key)

    # Block the target imports by storing None (Python import machinery
    # raises ImportError when sys.modules[name] is None).
    for mod_name in blocked_modules:
        sys.modules[mod_name] = None  # type: ignore[assignment]

    try:
        import apm_cli.utils.console as fresh

        importlib.reload(fresh)  # ensure module-level code re-runs
        return fresh
    finally:
        # Remove freshly loaded/reloaded entries.
        for key in list(sys.modules.keys()):
            if key == "apm_cli.utils.console" or any(
                key == m or key.startswith(m + ".") for m in blocked_modules
            ):
                del sys.modules[key]
        # Restore originals.
        sys.modules.update(saved)


class TestImportFallbacks:
    """Cover the except-ImportError branches at module level (lines 18-23, 31-34)."""

    def test_rich_unavailable_sets_constants(self):
        """Lines 18-23: RICH_AVAILABLE=False and sentinel Any assignments."""
        fresh = _reimport_console_without(["rich"])
        assert fresh.RICH_AVAILABLE is False
        assert fresh.Console is Any
        assert fresh.Panel is Any
        assert fresh.Table is Any
        assert fresh.rich_print is None

    def test_colorama_unavailable_sets_constants(self):
        """Lines 31-34: COLORAMA_AVAILABLE=False and Fore/Style set to None."""
        fresh = _reimport_console_without(["colorama"])
        assert fresh.COLORAMA_AVAILABLE is False
        assert fresh.Fore is None
        assert fresh.Style is None


class TestGetConsoleDoubleCheckLock:
    """Line 80: inner guard of the double-checked locking pattern."""

    def setup_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def teardown_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def test_inner_guard_returns_pre_set_instance(self):
        """Hit line 80 by setting _console_instance inside the lock's __enter__.

        The outer None-check sees None, we enter the lock, the mock __enter__
        sets _console_instance to a sentinel, then the inner guard returns it
        immediately without constructing a new Console.
        """
        import apm_cli.utils.console as mod

        sentinel = MagicMock(name="pre_set_console")
        real_lock = mod._console_lock

        class _FakeLock:
            def __enter__(self_inner):
                # Simulate another thread having set _console_instance.
                mod._console_instance = sentinel
                real_lock.acquire()
                return self_inner

            def __exit__(self_inner, *args: object) -> None:
                real_lock.release()

        with patch.object(mod, "_console_lock", _FakeLock()):
            result = mod._get_console()

        assert result is sentinel


class TestSetConsoleStderr:
    """Tests for set_console_stderr (also resets singleton)."""

    def setup_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def teardown_method(self):
        from apm_cli.utils.console import _reset_console

        _reset_console()

    def test_set_stderr_true_clears_singleton(self):
        import apm_cli.utils.console as mod

        # Force singleton creation.
        mod._get_console()
        assert mod._console_instance is not None

        mod.set_console_stderr(True)
        assert mod._console_instance is None
        assert mod._console_stderr is True

    def test_set_stderr_false_restores_default(self):
        import apm_cli.utils.console as mod

        mod.set_console_stderr(True)
        mod.set_console_stderr(False)
        assert mod._console_stderr is False
        assert mod._console_instance is None
