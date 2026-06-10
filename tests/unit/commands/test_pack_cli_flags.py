"""Tests for new CLI flags in pack command (phase-3c, T-3c-01..12).

Covers:
- --marketplace filter validation (unknown format -> error)
- --marketplace-path FORMAT=PATH parsing + validation
- --json flag emits valid JSON on failure
"""

from __future__ import annotations

import json as _json
import textwrap as _tw
from pathlib import Path as _Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd


@pytest.fixture(autouse=True)
def _reset_console_state():
    """Reset console singleton; --json mode flips a global stream flag."""
    from apm_cli.utils.console import _reset_console

    yield
    _reset_console()


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


class TestMarketplaceOutputRemoved:
    """T-3c-11: --marketplace-output was removed in v0.16 (breaking change, #1318)."""

    def test_removed_flag_is_unknown_option(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--marketplace-output", "test.json"])
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower() or isinstance(
            result.exception, SystemExit
        )


# ---------------------------------------------------------------------------
# Wave 4 release-gate flags: --check-versions / --check-clean
# ---------------------------------------------------------------------------


_APM_ALIGNED = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 1.0.0
"""

_APM_MISALIGNED = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 0.9.0
"""


def _write_project(tmp_path: _Path, apm_yml: str, *, pkg_version: str = "1.0.0") -> _Path:
    (tmp_path / "apm.yml").write_text(_tw.dedent(apm_yml), encoding="utf-8")
    pkg_dir = tmp_path / "packages" / "local-tool"
    pkg_dir.mkdir(parents=True)
    pkg_dir.joinpath("apm.yml").write_text(
        f"name: local-tool\ndescription: Tool.\nversion: {pkg_version}\n",
        encoding="utf-8",
    )
    return tmp_path


class TestHelpExitCodes:
    """Help text should document exit codes 3 and 4."""

    def test_exit_code_3_documented(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "3" in result.output
        assert "--check-versions" in result.output

    def test_exit_code_4_documented(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "4" in result.output
        assert "--check-clean" in result.output


class TestCheckVersionsFlag:
    """--check-versions release gate."""

    def test_flag_recognized(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert "--check-versions" in result.output

    def test_skip_when_no_marketplace_block(self, tmp_path: _Path, monkeypatch) -> None:
        # apm.yml without a marketplace block -> skip gate, exit 0
        (tmp_path / "apm.yml").write_text(
            "name: x\ndescription: y\nversion: 1.0.0\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--dry-run"])
        # Build itself should succeed or fail with code 1 (not 3) since gate skipped.
        assert result.exit_code != 3

    def test_passes_with_aligned_versions(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--dry-run", "--offline"])
        # Either gate passed (no exit 3) or bundle build hit unrelated failure;
        # the meaningful assertion is: exit code is not 3 (gate did not trip).
        assert result.exit_code != 3

    def test_fails_with_misaligned_versions(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_MISALIGNED, pkg_version="0.9.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--dry-run", "--offline"])
        assert result.exit_code == 3

    def test_json_envelope_carries_version_alignment(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        assert "version_alignment" in data
        assert data["version_alignment"] is not None

    def test_json_envelope_drift_null_when_not_requested(
        self, tmp_path: _Path, monkeypatch
    ) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        assert "drift" in data
        assert data["drift"] is None


class TestCheckCleanFlag:
    """--check-clean release gate."""

    def test_flag_recognized(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert "--check-clean" in result.output

    def test_skip_when_no_marketplace_block(self, tmp_path: _Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(
            "name: x\ndescription: y\nversion: 1.0.0\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run"])
        assert result.exit_code != 4

    def test_fails_when_on_disk_missing(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])
        # No marketplace.json on disk -> "missing" -> exit 4.
        assert result.exit_code == 4

    def test_json_envelope_carries_drift(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline", "--json"])
        data = _json.loads(result.output)
        assert "drift" in data
        assert data["drift"] is not None
        assert data["drift"]["ok"] is False

    def test_drift_error_includes_amend_recipe(self, tmp_path: _Path, monkeypatch) -> None:
        """Drift error output must include the commit --amend recovery recipe."""
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])
        assert result.exit_code == 4
        assert "commit --amend" in result.output

    def test_drift_error_includes_force_with_lease(self, tmp_path: _Path, monkeypatch) -> None:
        """Drift error output must include the force-with-lease recovery recipe."""
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])
        assert result.exit_code == 4
        assert "force-with-lease" in result.output

    def test_drift_error_includes_output_path(self, tmp_path: _Path, monkeypatch) -> None:
        """Drift error output must embed the affected path in the git add recipe line."""
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])
        assert result.exit_code == 4
        # Assert on a recipe-specific line that embeds the path; "marketplace.json"
        # alone was already present in the pre-recipe drift output (path display line).
        assert "git add" in result.output
        assert "marketplace.json" in result.output


class TestBothFlagsCombined:
    """Combined --check-versions + --check-clean: version exit (3) wins."""

    def test_both_flags_misaligned_versions_wins_exit_3(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_MISALIGNED, pkg_version="0.9.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--check-clean", "--dry-run", "--offline"]
        )
        # version-misalignment exit 3 takes precedence over drift exit 4
        assert result.exit_code == 3

    def test_both_flags_aligned_but_drift_exits_4(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--check-clean", "--dry-run", "--offline"]
        )
        # versions pass; drift fails (no marketplace.json on disk) -> exit 4
        assert result.exit_code == 4

    def test_json_envelope_carries_both_payloads(self, tmp_path: _Path, monkeypatch) -> None:
        _write_project(tmp_path, _APM_ALIGNED)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd,
            ["--check-versions", "--check-clean", "--dry-run", "--offline", "--json"],
        )
        data = _json.loads(result.output)
        assert data["version_alignment"] is not None
        assert data["drift"] is not None
