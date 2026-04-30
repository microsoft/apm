"""Integration tests for ``apm marketplace init``.

Strategy
--------
These tests exercise the real init command by invoking the Click
application through the CliRunner.  They verify scaffold creation,
idempotency guard, --force overwrite, gitignore warning, and that
the produced file parses via yml_schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest  # noqa: F401
from click.testing import CliRunner

from apm_cli.commands.marketplace import init
from apm_cli.marketplace.init_template import render_marketplace_yml_template
from apm_cli.marketplace.yml_schema import load_marketplace_yml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_init(tmp_path: Path, extra_args=(), catch_exceptions=True):
    """Invoke 'apm marketplace init' via CliRunner in *tmp_path*."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        # Write files into the isolated filesystem's CWD
        result = runner.invoke(
            init,
            list(extra_args),
            catch_exceptions=catch_exceptions,
        )
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitScaffold:
    """Verify scaffold creation behaviour."""

    def test_creates_marketplace_yml(self, tmp_path: Path):
        """init must write marketplace.yml in the current directory."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            result = runner.invoke(init, [], catch_exceptions=False)
            yml_path = Path(cwd) / "marketplace.yml"
            assert yml_path.exists(), "marketplace.yml was not created"
        assert result.exit_code == 0

    def test_template_content_is_valid_yml(self, tmp_path: Path):
        """The scaffold content must parse without errors."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            runner.invoke(init, [], catch_exceptions=False)
            yml_path = Path(cwd) / "marketplace.yml"
            # load_marketplace_yml must not raise
            parsed = load_marketplace_yml(yml_path)
        assert parsed.name == "my-marketplace"

    def test_success_message_in_output(self, tmp_path: Path):
        """init must print a success message."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(init, [], catch_exceptions=False)
        combined = result.output
        assert "marketplace.yml" in combined

    def test_verbose_shows_path(self, tmp_path: Path):
        """--verbose must show the output path."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:  # noqa: F841
            result = runner.invoke(init, ["--verbose"], catch_exceptions=False)
        assert "marketplace.yml" in result.output or "Path" in result.output

    def test_template_contains_packages_example(self, tmp_path: Path):
        """Scaffold must contain at least one example package entry."""
        template = render_marketplace_yml_template()
        assert "packages:" in template
        assert "source:" in template


class TestInitIdempotency:
    """Running init twice without --force must fail."""

    def test_second_run_without_force_exits_1(self, tmp_path: Path):
        """Second init in same directory must exit 1."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            runner.invoke(init, [], catch_exceptions=False)
            result = runner.invoke(init, [], catch_exceptions=False)
        assert result.exit_code == 1
        assert "already exists" in result.output or "--force" in result.output

    def test_force_overwrites_existing(self, tmp_path: Path):
        """--force must overwrite an existing marketplace.yml."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            # First run
            runner.invoke(init, [], catch_exceptions=False)
            yml_path = Path(cwd) / "marketplace.yml"
            yml_path.write_text("corrupted: true\n", encoding="utf-8")
            # Force overwrite
            result = runner.invoke(init, ["--force"], catch_exceptions=False)
            content = yml_path.read_text(encoding="utf-8")
        assert result.exit_code == 0
        # The scaffold must have replaced the corrupted content
        assert "my-marketplace" in content


class TestInitGitignoreWarning:
    """Warn when marketplace.json is gitignored."""

    def test_warning_when_marketplace_json_gitignored(self, tmp_path: Path):
        """A .gitignore entry for marketplace.json must produce a warning."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            gitignore = Path(cwd) / ".gitignore"
            gitignore.write_text("marketplace.json\n", encoding="utf-8")
            result = runner.invoke(init, [], catch_exceptions=False)
        combined = result.output
        assert "gitignore" in combined.lower() or "ignore" in combined.lower()

    def test_no_warning_without_gitignore(self, tmp_path: Path):
        """No gitignore warning when .gitignore does not exist."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            # Ensure no .gitignore exists
            gi = Path(cwd) / ".gitignore"
            if gi.exists():
                gi.unlink()
            result = runner.invoke(init, [], catch_exceptions=False)
        combined = result.output
        # Must succeed; no error about gitignore
        assert result.exit_code == 0
        assert "gitignore" not in combined.lower()
