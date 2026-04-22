"""Unit tests for the `apm experimental` CLI command group.

Uses click.testing.CliRunner exclusively; no real ~/.apm/ writes occur.

Coverage:
  - `apm experimental` (no subcommand) invokes list and shows the table.
  - list / list --enabled / list --disabled filtering.
  - enable: success message, hint line, underscore input normalisation.
  - enable with typo: exit 1, suggestion, recovery hint.
  - enable with completely unknown flag: exit 1, no "Did you mean" line.
  - disable: success message.
  - reset <name>: single-flag reset confirmation.
  - reset (no args): nothing-to-reset path, decline confirmation, --yes path.
  - -v / --verbose: config file path appears in output.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """CliRunner -- stderr is merged into stdout by default in Click 8."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_config_cache() -> None:
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch) -> None:
    """Redirect all config reads/writes to a throw-away temp directory.

    ``ensure_config_exists()`` will create the directory and file on first
    access -- no pre-creation required.
    """
    import apm_cli.config as _conf

    config_dir = tmp_path / ".apm"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)


# ---------------------------------------------------------------------------
# list (default subcommand)
# ---------------------------------------------------------------------------


class TestListCommand:
    """Tests for `apm experimental list` and the default-to-list behaviour."""

    def test_no_subcommand_invokes_list_and_shows_table_header(
        self, runner: CliRunner
    ) -> None:
        """Invoking the group with no subcommand defaults to `list`."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, [])
        assert result.exit_code == 0
        # The Rich table title or flag name must be present.
        assert (
            "Experimental Features" in result.output
            or "verbose-version" in result.output
        )

    def test_list_shows_verbose_version_disabled_by_default(
        self, runner: CliRunner
    ) -> None:
        """verbose-version appears with 'disabled' status when no override is set."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list"])
        assert result.exit_code == 0
        assert "verbose-version" in result.output
        assert "disabled" in result.output

    def test_list_enabled_filter_prints_no_flags_message_when_none_enabled(
        self, runner: CliRunner
    ) -> None:
        """--enabled with nothing enabled prints the 'no flags enabled' message."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--enabled"])
        assert result.exit_code == 0
        assert "No experimental flags are enabled." in result.output

    def test_list_disabled_filter_shows_flag_at_default(
        self, runner: CliRunner
    ) -> None:
        """--disabled shows verbose-version when it is at its default (disabled)."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--disabled"])
        assert result.exit_code == 0
        assert "verbose-version" in result.output

    def test_list_after_enable_appears_in_enabled_not_in_disabled(
        self, runner: CliRunner
    ) -> None:
        """After enabling, --enabled shows the flag and --disabled does not."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])

        # --enabled must show the flag
        result_en = runner.invoke(experimental, ["list", "--enabled"])
        assert result_en.exit_code == 0
        assert "verbose-version" in result_en.output

        # --disabled must NOT show the flag (all flags enabled message)
        result_dis = runner.invoke(experimental, ["list", "--disabled"])
        assert result_dis.exit_code == 0
        assert "verbose-version" not in result_dis.output
    def test_list_enabled_and_disabled_are_mutually_exclusive(
        self, runner: CliRunner
    ) -> None:
        """Passing both --enabled and --disabled produces a UsageError."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--enabled", "--disabled"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# enable subcommand
# ---------------------------------------------------------------------------


class TestEnableCommand:
    """Tests for `apm experimental enable <name>`."""

    def test_enable_exits_0_emits_success_and_hint(
        self, runner: CliRunner
    ) -> None:
        """Successful enable exits 0, emits [+] success line and hint."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["enable", "verbose-version"])
        assert result.exit_code == 0
        assert "[+] Enabled experimental feature: verbose-version" in result.output
        # hint line must follow success
        assert "apm --version" in result.output

    def test_enable_already_enabled_emits_warning_not_success(
        self, runner: CliRunner
    ) -> None:
        """Enabling an already-enabled flag emits warning [!], not a false success."""
        from apm_cli.commands.experimental import experimental

        # First enable succeeds
        runner.invoke(experimental, ["enable", "verbose-version"])
        # Second enable should be idempotent warning
        result = runner.invoke(experimental, ["enable", "verbose-version"])
        assert result.exit_code == 0
        assert "[!]" in result.output
        assert "already enabled" in result.output
        assert "[+] Enabled" not in result.output

    def test_enable_accepts_underscore_input(self, runner: CliRunner) -> None:
        """verbose_version (underscore) is normalised and accepted."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["enable", "verbose_version"])
        assert result.exit_code == 0
        assert "Enabled experimental feature: verbose-version" in result.output

    def test_enable_typo_exits_1_with_suggestion_and_recovery_hint(
        self, runner: CliRunner
    ) -> None:
        """One-character typo produces exit 1, error message, suggestion, recovery hint."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["enable", "verbse-version"])
        assert result.exit_code == 1
        assert "Unknown experimental feature: verbse-version" in result.output
        assert "Did you mean: verbose-version?" in result.output
        assert "apm experimental list" in result.output

    def test_enable_bogus_flag_exits_1_without_suggestion(
        self, runner: CliRunner
    ) -> None:
        """A flag name with no similarity produces no 'Did you mean' line."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["enable", "zzzz-totally-unrelated-qwerty"])
        assert result.exit_code == 1
        assert "Unknown experimental feature" in result.output
        assert "Did you mean" not in result.output


# ---------------------------------------------------------------------------
# disable subcommand
# ---------------------------------------------------------------------------


class TestDisableCommand:
    """Tests for `apm experimental disable <name>`."""

    def test_disable_after_enable_exits_0_emits_success(
        self, runner: CliRunner
    ) -> None:
        """disable exits 0 and emits the [+] disabled confirmation."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])
        result = runner.invoke(experimental, ["disable", "verbose-version"])
        assert result.exit_code == 0
        assert "[+] Disabled experimental feature: verbose-version" in result.output

    def test_disable_already_disabled_emits_warning_not_success(
        self, runner: CliRunner
    ) -> None:
        """Disabling an already-disabled flag emits warning [!], not a false success."""
        from apm_cli.commands.experimental import experimental

        # Flag is disabled by default -- second disable should be idempotent warning
        result = runner.invoke(experimental, ["disable", "verbose-version"])
        assert result.exit_code == 0
        assert "[!]" in result.output
        assert "already disabled" in result.output
        assert "[+] Disabled" not in result.output


# ---------------------------------------------------------------------------
# reset subcommand
# ---------------------------------------------------------------------------


class TestResetCommand:
    """Tests for `apm experimental reset [name] [--yes]`."""

    def test_reset_single_flag_exits_0_emits_confirmation(
        self, runner: CliRunner
    ) -> None:
        """reset <name> exits 0 and emits the per-flag reset confirmation."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])
        result = runner.invoke(experimental, ["reset", "verbose-version"])
        assert result.exit_code == 0
        assert "[+] Reset verbose-version to default" in result.output

    def test_reset_single_flag_already_at_default_prints_noop(
        self, runner: CliRunner
    ) -> None:
        """reset <name> on a pristine config prints nothing-to-do, not success."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["reset", "verbose-version"])
        assert result.exit_code == 0
        assert "already at its default" in result.output
        assert "Nothing to do" in result.output
        # Must NOT falsely claim a reset occurred
        assert "Reset verbose-version to default" not in result.output

    def test_reset_no_overrides_prints_nothing_to_reset(
        self, runner: CliRunner
    ) -> None:
        """reset with no overrides active emits the 'nothing to reset' message."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["reset"])
        assert result.exit_code == 0
        assert (
            "All features already at default settings. Nothing to reset."
            in result.output
        )

    def test_reset_with_overrides_declining_confirmation_does_not_reset(
        self, runner: CliRunner
    ) -> None:
        """Declining the confirmation prompt does not call _reset_flags(None)."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])

        with patch(
            "apm_cli.commands.experimental._reset_flags"
        ) as mock_reset, patch("rich.prompt.Confirm.ask", return_value=False):
            result = runner.invoke(experimental, ["reset"])

        assert result.exit_code == 0
        assert "Operation cancelled" in result.output
        # Verify the bulk-reset call never happened.
        bulk_calls = [c for c in mock_reset.call_args_list if c.args == (None,)]
        assert len(bulk_calls) == 0

    def test_reset_yes_flag_skips_prompt_and_resets(
        self, runner: CliRunner
    ) -> None:
        """--yes bypasses the confirmation prompt and resets all overrides."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])
        result = runner.invoke(experimental, ["reset", "--yes"])
        assert result.exit_code == 0
        assert "[+] Reset all experimental features to defaults" in result.output

    def test_reset_redundant_override_shows_removing_wording(
        self, runner: CliRunner
    ) -> None:
        """When override equals default, confirmation uses 'redundant override - removing'."""
        from apm_cli.commands.experimental import experimental

        # disable sets the flag to False, which matches default=False -> redundant
        runner.invoke(experimental, ["enable", "verbose-version"])
        runner.invoke(experimental, ["disable", "verbose-version"])

        with patch("rich.prompt.Confirm.ask", return_value=False):
            result = runner.invoke(experimental, ["reset"])

        assert result.exit_code == 0
        assert "redundant override - removing" in result.output
        # Must NOT contain the old arrow format for redundant overrides
        assert "currently disabled -> disabled" not in result.output

    def test_reset_singular_uses_its_default(
        self, runner: CliRunner
    ) -> None:
        """When resetting exactly 1 flag, summary says 'its default' not 'their defaults'."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])

        with patch("rich.prompt.Confirm.ask", return_value=False):
            result = runner.invoke(experimental, ["reset"])

        assert result.exit_code == 0
        assert "its default" in result.output
        assert "their defaults" not in result.output


# ---------------------------------------------------------------------------
# --verbose flag
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    """Tests for the -v / --verbose output path."""

    def test_verbose_list_shows_config_file_path(self, runner: CliRunner) -> None:
        """With -v, verbose_detail emits 'Config file: <path>' before the table."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["-v", "list"])
        assert result.exit_code == 0
        assert "Config file:" in result.output

    def test_verbose_after_subcommand_succeeds(self, runner: CliRunner) -> None:
        """apm experimental list -v must not raise 'Error: No such option: -v'."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "-v"])
        assert result.exit_code == 0
        assert "Config file:" in result.output
        assert "No such option" not in result.output


# ---------------------------------------------------------------------------
# Intro line (SF-2)
# ---------------------------------------------------------------------------


class TestIntroLine:
    """Tests for the intro-line displayed by `list`."""

    def test_list_does_not_emit_intro_at_normal_verbosity(self, runner: CliRunner) -> None:
        """Normal `list` output does NOT contain the intro preamble."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list"])
        assert result.exit_code == 0
        assert (
            "Experimental features let you try new behaviour"
            not in result.output
        )

    def test_list_verbose_emits_intro_line(self, runner: CliRunner) -> None:
        """With --verbose, `list` prints the intro description."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--verbose"])
        assert result.exit_code == 0
        assert (
            "Experimental features let you try new behaviour before it becomes default."
            in result.output
        )


# ---------------------------------------------------------------------------
# --verbose after subcommand (P-UX-1)
# ---------------------------------------------------------------------------


class TestVerboseAfterSubcommand:
    """Tests that --verbose/-v works when placed AFTER the subcommand name."""

    def test_enable_verbose_after_subcommand(self, runner: CliRunner) -> None:
        """apm experimental enable <name> --verbose shows Config file."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["enable", "verbose-version", "--verbose"])
        assert result.exit_code == 0
        assert "Config file:" in result.output

    def test_disable_verbose_after_subcommand(self, runner: CliRunner) -> None:
        """apm experimental disable <name> --verbose shows Config file."""
        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])
        result = runner.invoke(experimental, ["disable", "verbose-version", "-v"])
        assert result.exit_code == 0
        assert "Config file:" in result.output

    def test_reset_verbose_after_subcommand(self, runner: CliRunner) -> None:
        """apm experimental reset --verbose shows Config file."""
        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["reset", "--verbose"])
        assert result.exit_code == 0
        assert "Config file:" in result.output


# ---------------------------------------------------------------------------
# --json output (P-UX-2)
# ---------------------------------------------------------------------------


class TestJsonOutput:
    """Tests for `apm experimental list --json`."""

    def test_json_output_parses_and_has_correct_schema(self, runner: CliRunner) -> None:
        """--json outputs valid JSON with the expected keys for each flag."""
        import json

        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--json"])
        assert result.exit_code == 0

        rows = json.loads(result.output)
        assert isinstance(rows, list)
        assert len(rows) >= 1

        for row in rows:
            assert set(row.keys()) == {"name", "enabled", "default", "description", "source"}
            assert isinstance(row["name"], str)
            assert isinstance(row["enabled"], bool)
            assert isinstance(row["default"], bool)
            assert isinstance(row["description"], str)
            assert row["source"] in ("default", "config")

    def test_json_output_shows_default_source_when_no_override(self, runner: CliRunner) -> None:
        """Without overrides, source is 'default'."""
        import json

        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--json"])
        rows = json.loads(result.output)

        vv = [r for r in rows if r["name"] == "verbose_version"][0]
        assert vv["enabled"] is False
        assert vv["source"] == "default"

    def test_json_output_shows_config_source_after_enable(self, runner: CliRunner) -> None:
        """After enabling, source becomes 'config'."""
        import json

        from apm_cli.commands.experimental import experimental

        runner.invoke(experimental, ["enable", "verbose-version"])
        result = runner.invoke(experimental, ["list", "--json"])
        rows = json.loads(result.output)

        vv = [r for r in rows if r["name"] == "verbose_version"][0]
        assert vv["enabled"] is True
        assert vv["source"] == "config"

    def test_json_output_has_no_non_json_text(self, runner: CliRunner) -> None:
        """--json must NOT emit preamble, symbols, or colours to stdout."""
        import json

        from apm_cli.commands.experimental import experimental

        result = runner.invoke(experimental, ["list", "--json"])
        # The entire output must be valid JSON (no preamble, no symbols)
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        # No Rich markup or status symbols
        assert "[" not in result.output.split("\n")[0] or result.output.strip().startswith("[")


# ---------------------------------------------------------------------------
# Malformed value reset (C1)
# ---------------------------------------------------------------------------


class TestMalformedValueReset:
    """Tests for bulk reset of malformed (non-bool) config overrides."""

    def test_reset_cleans_malformed_string_override(self, runner: CliRunner, tmp_path) -> None:
        """reset --yes removes a registered flag with a string value (e.g. 'true')."""
        import json as _json

        import apm_cli.config as _conf

        from apm_cli.commands.experimental import experimental

        # Write a malformed config directly
        config_dir = tmp_path / ".apm-malformed"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_text(
            _json.dumps({"experimental": {"verbose_version": "true"}}),
            encoding="utf-8",
        )

        # Redirect config to the crafted file
        import apm_cli.config as _mod

        orig_dir = _mod.CONFIG_DIR
        orig_file = _mod.CONFIG_FILE
        _mod.CONFIG_DIR = str(config_dir)
        _mod.CONFIG_FILE = str(config_file)
        _mod._config_cache = None

        try:
            result = runner.invoke(experimental, ["reset", "--yes"])
            assert result.exit_code == 0
            assert "Nothing to reset" not in result.output

            data = _json.loads(config_file.read_text(encoding="utf-8"))
            assert "verbose_version" not in data.get("experimental", {})
        finally:
            _mod.CONFIG_DIR = orig_dir
            _mod.CONFIG_FILE = orig_file
            _mod._config_cache = None

    def test_reset_cleans_mixed_overrides_stale_and_malformed(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """reset --yes handles bool override + malformed value + stale key together."""
        import json as _json

        import apm_cli.config as _conf

        from apm_cli.commands.experimental import experimental

        config_dir = tmp_path / ".apm-mixed"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        # One valid bool override, one malformed string, one stale unknown key
        config_file.write_text(
            _json.dumps({
                "experimental": {
                    "verbose_version": "true",     # malformed (string, not bool)
                    "old_removed_flag": True,       # stale (not in FLAGS)
                }
            }),
            encoding="utf-8",
        )

        import apm_cli.config as _mod

        orig_dir = _mod.CONFIG_DIR
        orig_file = _mod.CONFIG_FILE
        _mod.CONFIG_DIR = str(config_dir)
        _mod.CONFIG_FILE = str(config_file)
        _mod._config_cache = None

        try:
            result = runner.invoke(experimental, ["reset", "--yes"])
            assert result.exit_code == 0
            assert "Nothing to reset" not in result.output
            assert "Reset all experimental features to defaults" in result.output

            data = _json.loads(config_file.read_text(encoding="utf-8"))
            exp_section = data.get("experimental", {})
            assert exp_section == {}
        finally:
            _mod.CONFIG_DIR = orig_dir
            _mod.CONFIG_FILE = orig_file
            _mod._config_cache = None
