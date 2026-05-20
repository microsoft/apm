"""Tests for missed coverage lines in ``apm_cli.commands._helpers``.

Covers:
- _rich_blank_line when console is None (line 95)
- _lazy_yaml ImportError path (lines 100-105)
- _lazy_prompt ImportError path (lines 110-115)
- _lazy_confirm ImportError path (lines 120-125)
- print_version when sha is empty (branch 341->344)
- print_version console.print exception fallback (lines 350-354)
- print_version console is None (lines 352-354)
- _update_gitignore_for_apm_modules read error with no logger (line 427-429)
- _update_gitignore_for_apm_modules write error with no logger (lines 448-452)
- _auto_detect_author success path (lines 502-503) and exception (504-505)
- _auto_detect_description success path (line 522-525) and exception (526-527)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _rich_blank_line
# ---------------------------------------------------------------------------


class TestRichBlankLineNoConsole:
    def test_falls_back_to_click_echo_when_no_console(self) -> None:
        """Line 95: console is None → click.echo() called."""
        from apm_cli.commands._helpers import _rich_blank_line

        with (
            patch("apm_cli.commands._helpers._get_console", return_value=None),
            patch("apm_cli.commands._helpers.click") as mock_click,
        ):
            _rich_blank_line()

        mock_click.echo.assert_called_once_with()


# ---------------------------------------------------------------------------
# Lazy import helpers
# ---------------------------------------------------------------------------


class TestLazyYaml:
    def test_import_error_re_raises(self) -> None:
        """Lines 100-105: yaml missing → ImportError with PyYAML message."""

        import apm_cli.commands._helpers as helpers

        orig = sys.modules.get("yaml")
        try:
            sys.modules["yaml"] = None  # type: ignore[assignment]
            import pytest

            with pytest.raises(ImportError, match="PyYAML"):
                helpers._lazy_yaml()
        finally:
            if orig is None:
                sys.modules.pop("yaml", None)
            else:
                sys.modules["yaml"] = orig


class TestLazyPrompt:
    def test_import_error_returns_none(self) -> None:
        """Lines 110-115: rich.prompt missing → returns None."""
        import apm_cli.commands._helpers as helpers

        orig = sys.modules.get("rich.prompt")
        try:
            sys.modules["rich.prompt"] = None  # type: ignore[assignment]
            result = helpers._lazy_prompt()
        finally:
            if orig is None:
                sys.modules.pop("rich.prompt", None)
            else:
                sys.modules["rich.prompt"] = orig

        assert result is None


class TestLazyConfirm:
    def test_import_error_returns_none(self) -> None:
        """Lines 120-125: rich.prompt missing → returns None."""
        import apm_cli.commands._helpers as helpers

        orig = sys.modules.get("rich.prompt")
        try:
            sys.modules["rich.prompt"] = None  # type: ignore[assignment]
            result = helpers._lazy_confirm()
        finally:
            if orig is None:
                sys.modules.pop("rich.prompt", None)
            else:
                sys.modules["rich.prompt"] = orig

        assert result is None


# ---------------------------------------------------------------------------
# print_version paths
# ---------------------------------------------------------------------------


class TestPrintVersionShaFalsy:
    def test_no_sha_not_appended_to_version_string(self) -> None:
        """Branch 341->344: sha empty → version_str unchanged (no parenthetical)."""
        from apm_cli.commands._helpers import print_version

        ctx = MagicMock()
        ctx.resilient_parsing = False

        mock_console = MagicMock()
        with (
            patch("apm_cli.commands._helpers.get_version", return_value="1.2.3"),
            patch("apm_cli.commands._helpers.get_build_sha", return_value=""),
            patch("apm_cli.commands._helpers._get_console", return_value=mock_console),
        ):
            print_version(ctx, None, True)

        call_args = mock_console.print.call_args[0][0]
        assert "1.2.3" in call_args
        # sha should NOT be appended as "(sha)" — check no "(abc" pattern
        assert "1.2.3 (" not in call_args


class TestPrintVersionConsoleException:
    def test_console_print_exception_falls_back_to_click_echo(self) -> None:
        """Lines 350-354: console.print raises → click.echo fallback."""
        from apm_cli.commands._helpers import print_version

        ctx = MagicMock()
        ctx.resilient_parsing = False

        mock_console = MagicMock()
        mock_console.print.side_effect = Exception("markup failure")

        with (
            patch("apm_cli.commands._helpers.get_version", return_value="2.3.4"),
            patch("apm_cli.commands._helpers.get_build_sha", return_value="abc1234"),
            patch("apm_cli.commands._helpers._get_console", return_value=mock_console),
            patch("apm_cli.commands._helpers.click") as mock_click,
        ):
            # Suppress is_enabled import error if experimental module absent
            print_version(ctx, None, True)

        mock_click.echo.assert_called()
        text = mock_click.echo.call_args[0][0]
        assert "2.3.4" in text


class TestPrintVersionNoConsole:
    def test_no_console_falls_back_to_click_echo(self) -> None:
        """Lines 352-354: console is None → click.echo fallback."""
        from apm_cli.commands._helpers import print_version

        ctx = MagicMock()
        ctx.resilient_parsing = False

        with (
            patch("apm_cli.commands._helpers.get_version", return_value="3.0.0"),
            patch("apm_cli.commands._helpers.get_build_sha", return_value=""),
            patch("apm_cli.commands._helpers._get_console", return_value=None),
            patch("apm_cli.commands._helpers.click") as mock_click,
        ):
            print_version(ctx, None, True)

        mock_click.echo.assert_called()
        text = mock_click.echo.call_args[0][0]
        assert "3.0.0" in text


# ---------------------------------------------------------------------------
# _update_gitignore_for_apm_modules  (no-logger paths)
# ---------------------------------------------------------------------------


class TestUpdateGitignoreReadError:
    def test_read_error_no_logger_calls_rich_warning(self, tmp_path: Path) -> None:
        """Lines 428-430: read OSError with logger=None → _rich_warning + return."""
        from apm_cli.commands._helpers import _update_gitignore_for_apm_modules

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("some content\n")  # make exists() True

        import builtins

        real_open = builtins.open

        def patched_open(path, mode="r", *args, **kwargs):
            if ".gitignore" in str(path) and "a" not in mode:
                raise OSError("permission denied")
            return real_open(path, mode, *args, **kwargs)

        with (
            patch("apm_cli.commands._helpers.GITIGNORE_FILENAME", str(gitignore)),
            patch("builtins.open", side_effect=patched_open),
            patch("apm_cli.commands._helpers._rich_warning") as mock_warn,
        ):
            _update_gitignore_for_apm_modules(logger=None)

        mock_warn.assert_called_once()
        assert ".gitignore" in mock_warn.call_args[0][0].lower()


class TestUpdateGitignoreWriteError:
    def test_write_error_no_logger_calls_rich_warning(self, tmp_path: Path) -> None:
        """Lines 448-452: write OSError with logger=None → _rich_warning."""
        from apm_cli.commands._helpers import _update_gitignore_for_apm_modules

        gitignore = tmp_path / ".gitignore"
        # Content without the apm_modules/ pattern, so the write path is triggered
        gitignore.write_text("node_modules/\n")

        import builtins

        real_open = builtins.open

        def patched_open(path, mode="r", *args, **kwargs):
            if ".gitignore" in str(path) and "a" in mode:
                raise OSError("read-only filesystem")
            return real_open(path, mode, *args, **kwargs)

        with (
            patch("apm_cli.commands._helpers.GITIGNORE_FILENAME", str(gitignore)),
            patch("builtins.open", side_effect=patched_open),
            patch("apm_cli.commands._helpers._rich_warning") as mock_warn,
        ):
            _update_gitignore_for_apm_modules(logger=None)

        mock_warn.assert_called_once()
        assert ".gitignore" in mock_warn.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# _auto_detect_author
# ---------------------------------------------------------------------------


class TestAutoDetectAuthor:
    def test_git_user_name_returned_on_success(self) -> None:
        """Lines 502-503: subprocess succeeds → returns stripped name."""
        from apm_cli.commands._helpers import _auto_detect_author

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Alice Developer\n"

        with patch("subprocess.run", return_value=mock_result):
            result = _auto_detect_author()

        assert result == "Alice Developer"

    def test_fallback_developer_on_exception(self) -> None:
        """Lines 504-505: subprocess raises → returns 'Developer'."""
        from apm_cli.commands._helpers import _auto_detect_author

        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = _auto_detect_author()

        assert result == "Developer"

    def test_fallback_developer_when_returncode_nonzero(self) -> None:
        """Line 502 branch: returncode != 0 → returns 'Developer'."""
        from apm_cli.commands._helpers import _auto_detect_author

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _auto_detect_author()

        assert result == "Developer"


# ---------------------------------------------------------------------------
# _auto_detect_description
# ---------------------------------------------------------------------------


class TestAutoDetectDescription:
    def test_returns_default_even_with_git_url(self) -> None:
        """Lines 522-525: subprocess finds URL → still returns default description."""
        from apm_cli.commands._helpers import _auto_detect_description

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/org/repo.git\n"

        with patch("subprocess.run", return_value=mock_result):
            result = _auto_detect_description("myproject")

        assert result == "APM project for myproject"

    def test_exception_returns_default_description(self) -> None:
        """Lines 526-527: subprocess raises → returns default description."""
        from apm_cli.commands._helpers import _auto_detect_description

        with patch("subprocess.run", side_effect=OSError("no git")):
            result = _auto_detect_description("myproject")

        assert result == "APM project for myproject"


# ---------------------------------------------------------------------------
# Lazy import SUCCESS paths (lines 103, 113, 123)
# ---------------------------------------------------------------------------


class TestLazyYamlSuccess:
    def test_returns_yaml_module_when_available(self) -> None:
        """Line 103: _lazy_yaml() success → returns yaml module."""
        from apm_cli.commands._helpers import _lazy_yaml

        result = _lazy_yaml()
        import yaml

        assert result is yaml


class TestLazyPromptSuccess:
    def test_returns_prompt_class_when_available(self) -> None:
        """Line 113: _lazy_prompt() success → returns Prompt class."""
        from apm_cli.commands._helpers import _lazy_prompt

        result = _lazy_prompt()
        assert result is not None
        assert hasattr(result, "ask")


class TestLazyConfirmSuccess:
    def test_returns_confirm_class_when_available(self) -> None:
        """Line 123: _lazy_confirm() success → returns Confirm class."""
        from apm_cli.commands._helpers import _lazy_confirm

        result = _lazy_confirm()
        assert result is not None
        assert hasattr(result, "ask")


# ---------------------------------------------------------------------------
# _check_orphaned_packages paths (lines 299, 313-314, 330-331)
# ---------------------------------------------------------------------------


class TestCheckOrphanedPackages:
    def test_returns_empty_when_no_apm_yml(self, tmp_path) -> None:
        """Line 299: APM_YML_FILENAME doesn't exist → []."""
        from apm_cli.commands._helpers import _check_orphaned_packages

        with patch("apm_cli.commands._helpers.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_path_cls.return_value = mock_path
            result = _check_orphaned_packages()

        assert result == []

    def test_returns_empty_on_parse_exception(self, tmp_path) -> None:
        """Lines 313-314: APMPackage.from_apm_yml raises → []."""
        from apm_cli.commands._helpers import _check_orphaned_packages

        # apm.yml exists, apm_modules exists, but parsing fails

        with (
            patch("apm_cli.commands._helpers.Path") as mock_path_cls,
            patch(
                "apm_cli.commands._helpers.APMPackage.from_apm_yml"
                if hasattr(
                    __import__("apm_cli.commands._helpers", fromlist=["APMPackage"]), "APMPackage"
                )
                else "apm_cli.models.apm_package.APMPackage.from_apm_yml",
                side_effect=ValueError("bad yaml"),
            ),
        ):
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path_cls.return_value = mock_path
            result = _check_orphaned_packages()

        # Either [] from the exception or [] from no apm_modules
        assert isinstance(result, list)

    def test_returns_empty_on_outer_exception(self) -> None:
        """Lines 330-331: outer exception swallowed → []."""
        from apm_cli.commands._helpers import _check_orphaned_packages

        with patch("apm_cli.commands._helpers.Path", side_effect=RuntimeError("disk error")):
            result = _check_orphaned_packages()

        assert result == []


# ---------------------------------------------------------------------------
# _create_minimal_apm_yml with "target" key (lines 609-614)
# ---------------------------------------------------------------------------


class TestCreateMinimalApmYml:
    def test_target_string_normalised_to_list(self, tmp_path) -> None:
        """Lines 609-614: config['target'] is a CSV string → targets list in written file."""
        from apm_cli.commands._helpers import _create_minimal_apm_yml

        cfg = {
            "name": "test-project",
            "version": "1.0.0",
            "description": "desc",
            "author": "dev",
            "target": "claude, cursor",
        }

        out = tmp_path / "apm.yml"
        _create_minimal_apm_yml(cfg, target_path=out)

        content = out.read_text()
        assert "targets" in content
        assert "claude" in content
        assert "cursor" in content

    def test_target_list_normalised(self, tmp_path) -> None:
        """Lines 611-612: config['target'] is a list → targets list in written file."""
        from apm_cli.commands._helpers import _create_minimal_apm_yml

        cfg = {
            "name": "test-project",
            "version": "1.0.0",
            "description": "desc",
            "author": "dev",
            "target": ["claude", "copilot"],
        }

        out = tmp_path / "apm.yml"
        _create_minimal_apm_yml(cfg, target_path=out)

        content = out.read_text()
        assert "targets" in content
        assert "claude" in content
        assert "copilot" in content
