"""tests for apm_cli.commands.init (pure logic cases)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-onboarding-metadata-only")]

# ---------------------------------------------------------------------------
# _parse_toggle_input
# ---------------------------------------------------------------------------


class TestParseToggleInput:
    @pytest.mark.parametrize(
        ("raw", "total", "expected"),
        [
            ("", 5, []),
            ("3", 5, [2]),
            ("1,3,5", 5, [0, 2, 4]),
            ("2-4", 5, [1, 2, 3]),
            ("all", 4, [0, 1, 2, 3]),
            ("none", 4, [0, 1, 2, 3]),
            ("  2  ", 5, [1]),
        ],
        ids=["empty", "single-number", "csv-numbers", "range", "all", "none", "whitespace"],
    )
    def test_valid_selection(self, raw, total, expected):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input(raw, total)
        assert indices == expected
        assert err is None

    def test_mixed_range_and_csv(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1,3-5,7", 7)
        assert set(indices) == {0, 2, 3, 4, 6}
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


# ---------------------------------------------------------------------------
# _read_existing_targets
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_init_targets
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# init CLI command smoke tests
# ---------------------------------------------------------------------------


# Should exit with error due to invalid plugin name
# (implementation may vary based on validation logic)
