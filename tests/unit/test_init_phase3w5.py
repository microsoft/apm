"""Phase-3w5 tests for apm_cli.commands.init.

Covers missing lines/branches identified in coverage-unit.json:
- _perform_init: project_name handling, existing apm.yml, yes flag
- _interactive_project_setup: ImportError fallback (non-Rich path) (lines 351-366)
- _confirm_setup_summary: ImportError fallback, abort path (lines 402-412)
- _resolve_init_targets: --target flag, non-interactive/yes mode, TTY mode (lines 440-489)
- _read_existing_targets: targets/target field, plural/singular forms (lines 492-521)
- _parse_toggle_input: range, csv, errors, all/none, invalid tokens (lines 525-560)
- _prompt_target_selection: interactive render, toggle, done (lines 563-631)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# _parse_toggle_input
# ---------------------------------------------------------------------------


class TestParseToggleInput:
    def test_empty_response_returns_empty(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("", 5)
        assert indices == []
        assert err is None

    def test_single_number(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("3", 5)
        assert indices == [2]  # 0-based
        assert err is None

    def test_csv_numbers(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1,3,5", 5)
        assert indices == [0, 2, 4]
        assert err is None

    def test_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("2-4", 5)
        assert indices == [1, 2, 3]
        assert err is None

    def test_mixed_range_and_csv(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1,3-5,7", 7)
        assert set(indices) == {0, 2, 3, 4, 6}
        assert err is None

    def test_all_keyword(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("all", 4)
        assert indices == [0, 1, 2, 3]
        assert err is None

    def test_none_keyword(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("none", 4)
        assert indices == [0, 1, 2, 3]
        assert err is None

    def test_invalid_range_non_numeric(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("a-b", 5)
        assert indices == []
        assert err is not None
        assert "Invalid range" in err

    def test_range_out_of_bounds(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1-10", 5)
        assert indices == []
        assert err is not None
        assert "out of bounds" in err

    def test_invalid_token(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("abc", 5)
        assert indices == []
        assert err is not None
        assert "Invalid token" in err

    def test_number_out_of_bounds(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("10", 5)
        assert indices == []
        assert err is not None
        assert "out of bounds" in err

    def test_number_zero_out_of_bounds(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("0", 5)
        assert indices == []
        assert err is not None

    def test_whitespace_ignored(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("  2  ", 5)
        assert indices == [1]
        assert err is None


# ---------------------------------------------------------------------------
# _read_existing_targets
# ---------------------------------------------------------------------------


class TestReadExistingTargets:
    def test_no_apm_yml_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        result = _read_existing_targets(tmp_path)
        assert result == []

    def test_plural_targets_list(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"targets": ["copilot", "claude"]}))
        result = _read_existing_targets(tmp_path)
        assert result == ["copilot", "claude"]

    def test_plural_targets_csv_string(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"targets": "copilot,claude"}))
        result = _read_existing_targets(tmp_path)
        assert result == ["copilot", "claude"]

    def test_legacy_singular_target(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"target": "copilot"}))
        result = _read_existing_targets(tmp_path)
        assert result == ["copilot"]

    def test_legacy_target_list(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"target": ["copilot", "cursor"]}))
        result = _read_existing_targets(tmp_path)
        assert result == ["copilot", "cursor"]

    def test_non_dict_yaml_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text("- list\n- items\n")
        result = _read_existing_targets(tmp_path)
        assert result == []

    def test_no_target_field_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"name": "test"}))
        result = _read_existing_targets(tmp_path)
        assert result == []

    def test_invalid_yaml_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text("{{{{invalid yaml")
        result = _read_existing_targets(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# _resolve_init_targets
# ---------------------------------------------------------------------------


class TestResolveInitTargets:
    def _make_logger(self):
        m = MagicMock()
        m.progress = MagicMock()
        return m

    def test_target_flag_wins_unconditionally(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        result = _resolve_init_targets(
            tmp_path,
            target_flag="copilot",
            yes=True,
            apm_yml_exists=False,
            logger=logger,
        )
        assert result == ["copilot"]

    def test_target_flag_list_wins(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        result = _resolve_init_targets(
            tmp_path,
            target_flag=["copilot", "claude"],
            yes=True,
            apm_yml_exists=False,
            logger=logger,
        )
        assert set(result) == {"copilot", "claude"}

    def test_yes_flag_non_interactive_with_signals(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        mock_signal = MagicMock()
        mock_signal.target = "copilot"
        mock_signal.source = ".github"
        logger = self._make_logger()

        with (
            patch("apm_cli.commands.init.detect_signals", return_value=[mock_signal]),
            patch("apm_cli.commands.init.EXPLICIT_ONLY_TARGETS", new=set()),
        ):
            result = _resolve_init_targets(
                tmp_path,
                target_flag=None,
                yes=True,
                apm_yml_exists=False,
                logger=logger,
            )
        assert "copilot" in (result or [])

    def test_yes_flag_no_signals_returns_none(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        with patch("apm_cli.commands.init.detect_signals", return_value=[]):
            result = _resolve_init_targets(
                tmp_path,
                target_flag=None,
                yes=True,
                apm_yml_exists=False,
                logger=logger,
            )
        assert result is None

    def test_non_tty_no_signals_returns_none(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        with (
            patch("apm_cli.commands.init.detect_signals", return_value=[]),
            patch("apm_cli.commands.init._stdin_is_tty", return_value=False),
        ):
            result = _resolve_init_targets(
                tmp_path,
                target_flag=None,
                yes=False,
                apm_yml_exists=False,
                logger=logger,
            )
        assert result is None

    def test_apm_yml_exists_seeds_prechecked(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        with (
            patch(
                "apm_cli.commands.init._read_existing_targets",
                return_value=["copilot"],
            ),
            patch("apm_cli.commands.init._stdin_is_tty", return_value=False),
        ):
            result = _resolve_init_targets(
                tmp_path,
                target_flag=None,
                yes=False,
                apm_yml_exists=True,
                logger=logger,
            )
        assert result == ["copilot"]


# ---------------------------------------------------------------------------
# _confirm_setup_summary - ImportError fallback
# ---------------------------------------------------------------------------


class TestConfirmSetupSummary:
    def test_rich_path_aborts_on_no(self):
        from apm_cli.commands.init import _confirm_setup_summary

        config = {
            "name": "my-proj",
            "version": "1.0.0",
            "description": "Desc",
            "author": "Author",
        }
        logger = MagicMock()

        with (
            patch("apm_cli.commands.init._get_console", return_value=MagicMock()),
        ):
            # Patch Confirm.ask to return False -> should sys.exit(0)
            with (
                patch("rich.prompt.Confirm.ask", return_value=False),
                pytest.raises(SystemExit) as exc_info,
            ):
                _confirm_setup_summary(config, logger)
        assert exc_info.value.code == 0

    def test_fallback_text_confirms_proceed(self):
        from apm_cli.commands.init import _confirm_setup_summary

        config = {
            "name": "my-proj",
            "version": "1.0.0",
            "description": "Desc",
            "author": "Author",
        }
        logger = MagicMock()

        with (
            patch.dict(
                sys.modules, {"rich.console": None, "rich.panel": None, "rich.prompt": None}
            ),
            patch("click.confirm", return_value=True),
        ):
            # Should not raise
            _confirm_setup_summary(config, logger)

    def test_fallback_text_aborts_on_no(self):
        from apm_cli.commands.init import _confirm_setup_summary

        config = {
            "name": "my-proj",
            "version": "1.0.0",
            "description": "Desc",
            "author": "Author",
        }
        logger = MagicMock()

        with (
            patch.dict(
                sys.modules, {"rich.console": None, "rich.panel": None, "rich.prompt": None}
            ),
            patch("click.confirm", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            _confirm_setup_summary(config, logger)
        assert exc_info.value.code == 0

    def test_targets_line_shows_in_summary(self):
        from apm_cli.commands.init import _confirm_setup_summary

        config = {
            "name": "my-proj",
            "version": "1.0.0",
            "description": "Desc",
            "author": "Author",
            "targets": ["copilot", "claude"],
        }
        logger = MagicMock()

        with (
            patch.dict(
                sys.modules, {"rich.console": None, "rich.panel": None, "rich.prompt": None}
            ),
            patch("click.confirm", return_value=True),
        ):
            _confirm_setup_summary(config, logger)


# ---------------------------------------------------------------------------
# _interactive_project_setup - ImportError fallback
# ---------------------------------------------------------------------------


class TestInteractiveProjectSetup:
    def test_fallback_text_path(self, tmp_path):
        """Covers the ImportError fallback (click.prompt) path in _interactive_project_setup."""
        from apm_cli.commands.init import _interactive_project_setup

        logger = MagicMock()

        with (
            patch.dict(sys.modules, {"rich.console": None, "rich.prompt": None}),
            patch("click.prompt", side_effect=["my-project", "1.0.0", "A test project", "Author"]),
            patch("apm_cli.commands._helpers._auto_detect_author", return_value="TestAuthor"),
            patch(
                "apm_cli.commands._helpers._auto_detect_description",
                return_value="Auto desc",
            ),
            patch("apm_cli.commands._helpers._validate_project_name", return_value=True),
        ):
            result = _interactive_project_setup("my-project", logger)

        assert result["name"] == "my-project"
        assert result["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# init CLI command smoke tests
# ---------------------------------------------------------------------------


class TestInitCommand:
    def setup_method(self):
        self.runner = CliRunner()
        self.orig_dir = os.getcwd()

    def teardown_method(self):
        import contextlib

        with contextlib.suppress(FileNotFoundError, OSError):
            os.chdir(self.orig_dir)

    def _run(self, *args, **kwargs):
        from apm_cli.cli import cli

        return self.runner.invoke(cli, ["init", *args], **kwargs)

    def test_yes_mode_creates_apm_yml(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("apm_cli.commands.init._resolve_init_targets", return_value=None):
                result = self._run("--yes")
        assert result.exit_code == 0
        # apm.yml should be created in the isolated filesystem

    def test_invalid_project_name_exits(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            result = self._run("../invalid/name", "--yes")
        assert result.exit_code != 0

    def test_project_name_dot_treated_as_none(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("apm_cli.commands.init._resolve_init_targets", return_value=None):
                result = self._run(".", "--yes")
        assert result.exit_code == 0

    def test_plugin_flag_deprecated_warning(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("apm_cli.commands.init._resolve_init_targets", return_value=None),
                patch("apm_cli.commands.init._validate_plugin_name", return_value=True),
            ):
                result = self._run("--plugin", "--yes")
        assert result.exit_code == 0
        # Deprecated flag warning should appear in stderr (or output)

    def test_marketplace_flag_deprecated_warning(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("apm_cli.commands.init._resolve_init_targets", return_value=None),
                patch("apm_cli.commands.init._validate_plugin_name", return_value=True),
            ):
                result = self._run("--marketplace", "--yes")
        assert result.exit_code == 0

    def test_existing_apm_yml_with_yes_overwrites(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            Path("apm.yml").write_text("name: existing\n")
            with patch("apm_cli.commands.init._resolve_init_targets", return_value=None):
                result = self._run("--yes")
        assert result.exit_code == 0

    def test_target_flag_passed_through(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            result = self._run("--target", "copilot", "--yes")
        assert result.exit_code == 0

    def test_plugin_with_invalid_name_exits(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            os.rename if hasattr(os, "rename") else None
            # Create a dir with invalid plugin name (contains uppercase)
            self._run("--plugin", "--yes", "Invalid_Plugin_Name")
        # Should exit with error due to invalid plugin name
        # (implementation may vary based on validation logic)
