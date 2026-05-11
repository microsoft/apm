"""End-to-end tests for invalid dependency format rejection.

Verifies that ``apm install`` exits non-zero with a clear error when
``apm.yml`` uses an unsupported dependency format (e.g. a flat list
instead of the structured ``dependencies: {apm: [...]}`` mapping).
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.models.apm_package import clear_apm_yml_cache

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


class TestInstallInvalidDepsFormatE2E:
    """``apm install`` must reject non-dict ``dependencies`` with exit 1."""

    def test_flat_list_deps_exits_nonzero(self, runner: CliRunner, tmp_path, monkeypatch) -> None:
        """Flat-list dependencies -> exit 1 with actionable error."""
        from apm_cli.cli import cli

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: test-project
                version: '1.0.0'
                dependencies:
                  - owner/repo
            """),
            encoding="utf-8",
        )
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "copilot-instructions.md").write_text("# test\n")

        with patch(_PATCH_UPDATES, return_value=None):
            result = runner.invoke(cli, ["install"], catch_exceptions=False)

        assert result.exit_code != 0, (
            f"Expected non-zero exit for flat-list deps; got 0.\nstdout:\n{result.stdout}"
        )
        combined = " ".join(((result.output or "") + (result.stderr or "")).split())
        assert "expected a mapping" in combined, (
            f"Expected 'expected a mapping' in output:\n{combined}"
        )

    def test_string_deps_exits_nonzero(self, runner: CliRunner, tmp_path, monkeypatch) -> None:
        """String dependencies -> exit 1 with actionable error."""
        from apm_cli.cli import cli

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: test-project
                version: '1.0.0'
                dependencies: owner/repo
            """),
            encoding="utf-8",
        )
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "copilot-instructions.md").write_text("# test\n")

        with patch(_PATCH_UPDATES, return_value=None):
            result = runner.invoke(cli, ["install"], catch_exceptions=False)

        assert result.exit_code != 0, (
            f"Expected non-zero exit for string deps; got 0.\nstdout:\n{result.stdout}"
        )
        combined = " ".join(((result.output or "") + (result.stderr or "")).split())
        assert "expected a mapping" in combined, (
            f"Expected 'expected a mapping' in output:\n{combined}"
        )

    def test_error_includes_structured_format_hint(
        self, runner: CliRunner, tmp_path, monkeypatch
    ) -> None:
        """Error output includes the structured-format example."""
        from apm_cli.cli import cli

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: test-project
                version: '1.0.0'
                dependencies:
                  - owner/repo
            """),
            encoding="utf-8",
        )
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "copilot-instructions.md").write_text("# test\n")

        with patch(_PATCH_UPDATES, return_value=None):
            result = runner.invoke(cli, ["install"], catch_exceptions=False)

        combined = (result.output or "") + (result.stderr or "")
        assert "apm:" in combined, (
            f"Expected structured-format hint with 'apm:' in output:\n{combined}"
        )
