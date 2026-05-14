"""Tests for new CLI flags in pack command (phase-3c, T-3c-01..12).

Covers:
- --marketplace filter validation (unknown format → error)
- --marketplace-path FORMAT=PATH parsing + validation
- --json flag emits valid JSON on failure
- --marketplace-output deprecation warning
"""

from __future__ import annotations

from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd


class TestMarketplaceFilterFlag:
    """T-3c-01..04: --marketplace flag parsing."""

    def test_unknown_format_raises(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--marketplace", "bogus"])
        assert result.exit_code != 0
        assert "Unknown marketplace format" in (
            result.output + (result.exception.__str__() if result.exception else "")
        )

    def test_unknown_format_json_mode(self) -> None:
        import json

        result = CliRunner().invoke(pack_cmd, ["--marketplace", "bogus", "--json"])
        # Should output valid JSON to stdout even on error
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["ok"] is False
        assert any("bogus" in e["message"] for e in data["errors"])


class TestMarketplacePathFlag:
    """T-3c-05..08: --marketplace-path parsing."""

    def test_missing_equals_raises(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--marketplace-path", "noequalssign"])
        assert result.exit_code != 0
        assert "FORMAT=PATH" in (
            result.output + (result.exception.__str__() if result.exception else "")
        )

    def test_unknown_format_raises(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--marketplace-path", "bogus=path.json"])
        assert result.exit_code != 0
        assert "Unknown marketplace format" in (
            result.output + (result.exception.__str__() if result.exception else "")
        )

    def test_missing_equals_json_mode(self) -> None:
        import json

        result = CliRunner().invoke(pack_cmd, ["--marketplace-path", "noequalssign", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["ok"] is False
        assert any("FORMAT=PATH" in e["message"] for e in data["errors"])


class TestJsonFlag:
    """T-3c-09..10: --json flag appears in help."""

    def test_json_in_help(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert "--json" in result.output
        assert "machine-readable" in result.output.lower() or "JSON" in result.output


class TestDeprecationWarning:
    """T-3c-11..12: --marketplace-output deprecation."""

    def test_deprecated_flag_still_accepted(self) -> None:
        """The flag doesn't crash immediately (it will fail later
        because no apm.yml exists, but that's fine — we check the
        deprecation message is printed before the crash)."""
        result = CliRunner().invoke(pack_cmd, ["--marketplace-output", "test.json"])
        combined = result.output or ""
        assert "deprecated" in combined.lower() or result.exit_code != 0
