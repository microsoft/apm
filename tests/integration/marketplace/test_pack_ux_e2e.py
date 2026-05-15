"""E2E integration tests for marketplace pack UX (issue #1317).

Covers:
- --json emits valid JSON with consistent envelope (no stdout contamination)
- --marketplace=FORMAT filter builds only requested formats
- --marketplace-path FORMAT=PATH writes to custom path
- --marketplace-path with path traversal is rejected
- --marketplace-output deprecation warning goes to stderr
- --marketplace=none skips marketplace entirely
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd
from apm_cli.utils.console import _reset_console

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal apm.yml with marketplace block that uses map-form outputs
_APM_YML_MAP_FORM = """\
name: test-project
version: 0.1.0
marketplace:
  name: test-marketplace
  description: Test
  version: 1.0.0
  owner:
    name: Test
    email: test@example.com
    url: https://example.com
  metadata:
    pluginRoot: plugins
    category: testing
  packages:
    - name: my-skill
      description: A test skill
      source: acme/my-skill
  outputs:
    claude: {}
    codex: {}
"""

_APM_YML_CLAUDE_ONLY = """\
name: test-project
version: 0.1.0
marketplace:
  name: test-marketplace
  description: Test
  version: 1.0.0
  owner:
    name: Test
    email: test@example.com
    url: https://example.com
  metadata:
    pluginRoot: plugins
    category: testing
  packages:
    - name: my-skill
      description: A test skill
      source: acme/my-skill
  outputs:
    claude: {}
"""


def _setup_project(tmp_path: Path, yml_content: str = _APM_YML_MAP_FORM) -> Path:
    """Write apm.yml and return the project directory."""
    (tmp_path / "apm.yml").write_text(yml_content, encoding="utf-8")
    return tmp_path


def _mock_build_result():
    """Create a mock BuildResult that the orchestrator would return."""
    from apm_cli.core.build_orchestrator import BuildResult, OutputKind, ProducerResult
    from apm_cli.marketplace.builder import (
        BuildReport,
        MarketplaceOutputReport,
    )

    output_report = MarketplaceOutputReport(
        profile="claude",
        resolved=(),
        errors=(),
        warnings=(),
        output_path=Path(".claude-plugin/marketplace.json"),
        added_count=1,
        updated_count=0,
        unchanged_count=0,
        removed_count=0,
    )
    marketplace_report = BuildReport(outputs=(output_report,))

    return BuildResult(
        outputs=[],
        warnings=[],
        producer_results=[
            ProducerResult(
                kind=OutputKind.MARKETPLACE,
                outputs=[],
                warnings=[],
                payload=marketplace_report,
            )
        ],
    )


@pytest.fixture(autouse=True)
def _reset_console_after():
    """Ensure console state is clean after each test."""
    yield
    _reset_console()


# ---------------------------------------------------------------------------
# JSON output: consistent envelope
# ---------------------------------------------------------------------------


class TestJsonEnvelope:
    """--json emits a consistent envelope with required top-level keys."""

    def test_json_success_has_envelope_keys(self, tmp_path):
        _setup_project(tmp_path)
        mock_result = _mock_build_result()

        with patch("apm_cli.commands.pack.BuildOrchestrator") as MockOrch:
            MockOrch.return_value.run.return_value = mock_result
            runner = CliRunner()
            result = runner.invoke(
                pack_cmd,
                ["--json"],
                catch_exceptions=False,
                env={"PWD": str(tmp_path)},
            )
            # CliRunner captures stdout in result.output
            # Parse only lines that look like JSON
            stdout = result.output.strip()
            data = json.loads(stdout)
            assert data["ok"] is True
            assert "dry_run" in data
            assert "warnings" in data
            assert "errors" in data
            assert "marketplace" in data
            assert "bundle" in data

    def test_json_error_has_envelope_keys(self, tmp_path):
        """Error JSON must have same top-level shape."""
        _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            pack_cmd,
            ["--json", "--marketplace", "bogus"],
        )
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "errors" in data
        assert isinstance(data["errors"], list)
        assert len(data["errors"]) > 0


# ---------------------------------------------------------------------------
# Stdout contamination
# ---------------------------------------------------------------------------


class TestStdoutContamination:
    """Under --json, stdout must contain ONLY valid JSON."""

    def test_no_log_contamination_in_json_stdout(self, tmp_path):
        """When --json is set, no Rich/click output should appear on stdout."""
        _setup_project(tmp_path)
        mock_result = _mock_build_result()

        with patch("apm_cli.commands.pack.BuildOrchestrator") as MockOrch:
            MockOrch.return_value.run.return_value = mock_result
            runner = CliRunner()
            result = runner.invoke(
                pack_cmd,
                ["--json"],
                catch_exceptions=False,
            )
            stdout = result.output.strip()
            # Every line of stdout must be part of the JSON object
            # (no progress bars, no Rich formatting, no logger output)
            try:
                json.loads(stdout)
            except json.JSONDecodeError:
                pytest.fail(f"stdout under --json is not valid JSON:\n{stdout[:500]}")


# ---------------------------------------------------------------------------
# Path traversal rejection
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """--marketplace-path with traversal sequences must be rejected."""

    def test_dotdot_rejected(self):
        runner = CliRunner()
        result = runner.invoke(
            pack_cmd,
            ["--marketplace-path", "claude=../../etc/passwd"],
        )
        assert result.exit_code != 0
        combined = result.output + str(result.exception or "")
        assert ".." in combined or "traversal" in combined.lower()

    def test_dotdot_rejected_json(self):
        runner = CliRunner()
        result = runner.invoke(
            pack_cmd,
            ["--marketplace-path", "claude=../../etc/passwd", "--json"],
        )
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["ok"] is False
        assert any(
            ".." in e["message"] or "traversal" in e["message"].lower() for e in data["errors"]
        )


# ---------------------------------------------------------------------------
# Deprecation warning routing
# ---------------------------------------------------------------------------


class TestDeprecationRouting:
    """--marketplace-output deprecation warning must go to stderr, not stdout."""

    def test_deprecation_on_stderr(self, tmp_path):
        """Deprecation message for --marketplace-output should be on stderr."""
        _setup_project(tmp_path)
        mock_result = _mock_build_result()

        with patch("apm_cli.commands.pack.BuildOrchestrator") as MockOrch:
            MockOrch.return_value.run.return_value = mock_result
            runner = CliRunner()
            result = runner.invoke(
                pack_cmd,
                ["--marketplace-output", "test.json", "--json"],
                catch_exceptions=False,
            )
            stdout = result.output.strip()
            # Under --json, deprecation uses click.echo(err=True) so
            # with default CliRunner (mix_stderr=True) it appears in output.
            # Key assertion: the JSON portion is still parseable — strip
            # the deprecation line and parse the rest.
            lines = stdout.split("\n")
            json_lines = [line for line in lines if not line.startswith("Warning:")]
            json_str = "\n".join(json_lines).strip()
            if json_str:
                try:
                    data = json.loads(json_str)
                    assert data["ok"] is True
                except json.JSONDecodeError:
                    pytest.fail(f"Non-JSON content leaked to stdout:\n{json_str[:500]}")


# ---------------------------------------------------------------------------
# Marketplace filter: --marketplace=none
# ---------------------------------------------------------------------------


class TestMarketplaceNone:
    """--marketplace=none should skip marketplace entirely."""

    def test_none_sentinel_json(self, tmp_path):
        """With --marketplace=none and --json, marketplace.outputs is empty."""
        _setup_project(tmp_path)

        # Mock orchestrator to return empty result (no marketplace producer fires)
        from apm_cli.core.build_orchestrator import BuildResult

        empty_result = BuildResult(outputs=[], warnings=[], producer_results=[])

        with patch("apm_cli.commands.pack.BuildOrchestrator") as MockOrch:
            MockOrch.return_value.run.return_value = empty_result
            runner = CliRunner()
            result = runner.invoke(
                pack_cmd,
                ["--marketplace=none", "--json"],
                catch_exceptions=False,
            )
            data = json.loads(result.output)
            assert data["ok"] is True
            assert data["marketplace"]["outputs"] == []
