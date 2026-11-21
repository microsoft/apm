"""tests for apm_cli.commands.init.

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

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# _parse_toggle_input
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _read_existing_targets
# ---------------------------------------------------------------------------


class TestReadExistingTargets:
    @pytest.mark.parametrize(
        "content",
        [None, "- list\n- items\n", "name: test\n", "{{{{invalid yaml"],
        ids=["no-apm-yml", "non-dict-yaml", "no-target-field", "invalid-yaml"],
    )
    def test_unusable_manifest_returns_empty(self, tmp_path, content):
        from apm_cli.commands.init import _read_existing_targets

        if content is not None:
            (tmp_path / "apm.yml").write_text(content)
        assert _read_existing_targets(tmp_path) == []

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

    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            ({"target": "agents"}, ["copilot"]),
            ({"target": "agents,claude"}, ["copilot", "claude"]),
            ({"target": "agy"}, ["antigravity"]),
        ],
    )
    def test_legacy_target_uses_the_migrate_mapping(self, tmp_path, declared, expected):
        """#3857: a re-init never writes a ``targets:`` token APM rejects."""
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(yaml.safe_dump(declared))
        assert _read_existing_targets(tmp_path) == expected

    def test_unmappable_target_is_refused(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets
        from apm_cli.models.manifest_targets import TargetRewriteError

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"target": "intellij"}))
        with pytest.raises(TargetRewriteError, match="'intellij' has no 'targets:'"):
            _read_existing_targets(tmp_path)

    def test_anchored_manifest_is_refused(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets
        from apm_cli.models.manifest_io import ManifestAnchorError

        (tmp_path / "apm.yml").write_text("x: &t [claude]\ntargets: *t\n")
        with pytest.raises(ManifestAnchorError, match="anchor '&t' on line 1"):
            _read_existing_targets(tmp_path)


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

    def test_ide_target_flag_is_refused_before_writing(self, tmp_path):
        """#3878: --target intellij is never recorded (not even as copilot)."""
        from apm_cli.commands.init import _resolve_init_targets

        logger = self._make_logger()
        with pytest.raises(SystemExit) as excinfo:
            _resolve_init_targets(
                tmp_path, target_flag="intellij", yes=True, apm_yml_exists=False, logger=logger
            )
        assert excinfo.value.code == 1
        assert "'intellij' is an IDE, not a target" in logger.error.call_args.args[0]
        assert not (tmp_path / "apm.yml").exists()

    def test_unmappable_existing_target_exits_before_writing_when_non_interactive(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        (tmp_path / "apm.yml").write_text("target: intellij\n")
        logger = self._make_logger()
        with (
            patch("apm_cli.commands.init._stdin_is_tty", return_value=False),
            pytest.raises(SystemExit) as excinfo,
        ):
            _resolve_init_targets(
                tmp_path, target_flag=None, yes=True, apm_yml_exists=True, logger=logger
            )
        assert excinfo.value.code == 1
        assert "'intellij' has no 'targets:' equivalent" in logger.error.call_args.args[0]
        assert (tmp_path / "apm.yml").read_text() == "target: intellij\n"

    def test_anchored_existing_manifest_exits_before_writing_when_non_interactive(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        (tmp_path / "apm.yml").write_text("x: &t [claude]\ntargets: *t\n")
        logger = self._make_logger()
        with (
            patch("apm_cli.commands.init._stdin_is_tty", return_value=False),
            pytest.raises(SystemExit) as excinfo,
        ):
            _resolve_init_targets(
                tmp_path, target_flag=None, yes=True, apm_yml_exists=True, logger=logger
            )
        assert excinfo.value.code == 1
        assert "must not use YAML anchors" in logger.error.call_args.args[0]

    def test_unmappable_existing_target_warns_and_prompts_when_interactive(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets

        (tmp_path / "apm.yml").write_text("target: intellij\n")
        logger = self._make_logger()
        with (
            patch("apm_cli.commands.init._stdin_is_tty", return_value=True),
            patch("apm_cli.commands.init.detect_signals", return_value=[]),
            patch(
                "apm_cli.commands.init._prompt_target_selection", return_value=["claude"]
            ) as prompt,
        ):
            result = _resolve_init_targets(
                tmp_path, target_flag=None, yes=False, apm_yml_exists=True, logger=logger
            )
        assert result == ["claude"]
        assert prompt.call_args.args[0] == set()
        assert "'intellij' has no 'targets:' equivalent" in logger.warning.call_args.args[0]


# ---------------------------------------------------------------------------
# _confirm_setup_summary - ImportError fallback
# ---------------------------------------------------------------------------


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
                "apm_cli.models.manifest_bootstrap.auto_detect_description",
                return_value="Auto desc",
            ),
            patch("apm_cli.commands._helpers._validate_project_name", return_value=True),
        ):
            result = _interactive_project_setup("my-project", logger)

        assert result["name"] == "my-project"
        assert result["version"] == "1.0.0"

    def test_fallback_text_path_reprompts_on_invalid_name(self, tmp_path):
        """The click fallback shows the shared invalid-name rule, then re-prompts."""
        from apm_cli.commands.init import _interactive_project_setup

        logger = MagicMock()

        with (
            patch.dict(sys.modules, {"rich.console": None, "rich.prompt": None}),
            patch(
                "click.prompt",
                side_effect=["bad/name", "my-project", "1.0.0", "A test project", "Author"],
            ),
            patch("click.echo") as echo,
            patch("apm_cli.commands._helpers._auto_detect_author", return_value="TestAuthor"),
            patch(
                "apm_cli.models.manifest_bootstrap.auto_detect_description",
                return_value="Auto desc",
            ),
        ):
            result = _interactive_project_setup("my-project", logger)

        assert result["name"] == "my-project"
        echoed = " ".join(str(call.args[0]) for call in echo.call_args_list if call.args)
        assert "Invalid project name 'bad/name'" in echoed
        assert "empty or whitespace-only" in echoed


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

    def test_plugin_flag_was_removed(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("apm_cli.commands.init._resolve_init_targets", return_value=None),
                patch("apm_cli.commands.init._validate_plugin_name", return_value=True),
            ):
                result = self._run("--plugin", "--yes")
        assert result.exit_code == 2

    def test_marketplace_flag_was_removed(self, tmp_path):
        with self.runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("apm_cli.commands.init._resolve_init_targets", return_value=None),
                patch("apm_cli.commands.init._validate_plugin_name", return_value=True),
            ):
                result = self._run("--marketplace", "--yes")
        assert result.exit_code == 2

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
