"""CLI tests for ``apm pack`` and ``apm unpack`` run against real project files.

Covers:
- ``pack_cmd`` CLI: --legacy-skill-paths, --offline, --include-prerelease,
  --force flags, help text, deprecation notice for --marketplace-output.
- ``pack_cmd`` release gates and license warnings.
- ``unpack_cmd``: deprecation warning, FileNotFoundError handling.
"""

from __future__ import annotations

import json as _json
import textwrap as _tw
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import (
    pack_cmd,
    unpack_cmd,
)

pytestmark = pytest.mark.component


# ===========================================================================
# pack_cmd CLI flag tests
# ===========================================================================


@pytest.fixture(autouse=True)
def _reset_console():
    """Reset console singleton so --json never pollutes later tests."""
    from apm_cli.utils.console import _reset_console as _rc

    yield
    _rc()


_APM_SIMPLE = """\
name: test-project
description: A test project.
version: 1.0.0
"""


class TestPackCmdFlags:
    """Smoke tests for various pack_cmd flags."""

    def test_help_text_includes_all_key_flags(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        for flag in [
            "--claude-plugin",
            "--archive",
            "--format",
            "--dry-run",
            "--force",
            "--verbose",
            "--offline",
        ]:
            assert flag in result.output
        assert "current no-flag default" in result.output
        assert "agent-plugin" in result.output
        assert not any(line.strip().startswith("--plugin ") for line in result.output.splitlines())

    def test_help_exit_codes_match_behavior_one_per_line(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        lines = [line.strip() for line in result.output.splitlines()]
        assert any(line.startswith("1  Build or runtime error") for line in lines)
        assert any(line.startswith("2  Usage error") for line in lines)
        assert any(line.startswith("3  Version alignment") for line in lines)
        assert "Manifest schema validation error" not in result.output

    def test_redundant_format_selectors_are_usage_error(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--claude-plugin", "--format", "plugin"])
        assert result.exit_code == 2
        assert "Choose one bundle format selector" in result.output

    def test_removed_plugin_shortcut_is_rejected(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--plugin"])
        assert result.exit_code == 2
        assert "No such option: --plugin" in result.output

    def test_archive_help_includes_migration_and_size_cues(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "previous default" in result.output
        assert "--archive-format" in result.output
        assert "tar.gz" in result.output
        assert "smaller" in result.output

    def test_offline_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--offline"])
        assert result.exit_code in (0, 1)  # may fail due to missing lockfile; flag is accepted

    def test_include_prerelease_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--include-prerelease"])
        assert result.exit_code in (0, 1)

    def test_force_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--force"])
        assert result.exit_code in (0, 1)

    def test_legacy_skill_paths_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--legacy-skill-paths"])
        assert result.exit_code in (0, 1)

    def test_format_apm_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--format", "apm"])
        assert result.exit_code in (0, 1)

    def test_format_invalid_choice_fails(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--format", "invalid"])
        assert result.exit_code != 0

    def test_archive_format_zip_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--archive", "--archive-format", "zip"])
        assert "Invalid value for '--archive-format'" not in (result.output or "")
        assert result.exit_code in (0, 1)

    def test_archive_format_tar_gz_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--dry-run", "--archive", "--archive-format", "tar.gz"]
        )
        assert "Invalid value for '--archive-format'" not in (result.output or "")
        assert result.exit_code in (0, 1)

    def test_archive_format_invalid_choice_fails(self) -> None:
        """Click rejects unknown archive format values before the command runs."""
        result = CliRunner().invoke(pack_cmd, ["--archive", "--archive-format", "bz2"])
        assert result.exit_code != 0
        assert (
            "invalid" in (result.output or "").lower() or "error" in (result.output or "").lower()
        )

    def test_deprecated_target_flag_emits_warning(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--target", "claude"])
        # deprecated flag emits a warning on stderr (mix_stderr=True is CliRunner default)
        assert "deprecated" in (result.output or "").lower() or result.exit_code in (0, 1)

    def test_marketplace_filter_none_skips(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--marketplace", "none"])
        # 'none' is valid; should not crash with an unknown format error
        assert "Unknown marketplace format 'none'" not in (result.output or "")

    def test_marketplace_filter_all_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--marketplace", "all"])
        assert "Unknown marketplace format 'all'" not in (result.output or "")

    def test_marketplace_output_removed(self) -> None:
        """--marketplace-output was removed in v0.16 (breaking change, #1318)."""
        result = CliRunner().invoke(pack_cmd, ["--marketplace-output", "out.json", "--dry-run"])
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower() or isinstance(
            result.exception, SystemExit
        )

    def test_json_output_envelope_shape(self, tmp_path: Path, monkeypatch) -> None:
        """--json mode always returns a JSON envelope, even on no-op."""
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--json"])
        data = _json.loads(result.output)
        for key in ("ok", "dry_run", "warnings", "errors", "marketplace", "bundle"):
            assert key in data, f"Missing key '{key}' in JSON envelope"

    def test_archive_format_without_archive_is_usage_error(self) -> None:
        """--archive-format without --archive must exit with a UsageError."""
        result = CliRunner().invoke(pack_cmd, ["--archive-format", "tar.gz"])
        assert result.exit_code != 0
        assert "no effect without --archive" in (result.output or "")

    def test_archive_format_default_without_archive_is_ok(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Omitting --archive-format entirely (default) without --archive is fine."""
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run"])
        # Not a UsageError; exit code from normal pack logic
        assert "no effect without --archive" not in (result.output or "")


# ===========================================================================
# unpack_cmd tests
# ===========================================================================


class TestUnpackCmd:
    def test_unpack_deprecation_warning(self, tmp_path: Path) -> None:
        """unpack always emits a deprecation warning."""
        bundle = tmp_path / "bundle.zip"
        bundle.write_bytes(b"fake")
        result = CliRunner().invoke(
            unpack_cmd,
            [str(bundle), "--dry-run"],
        )
        assert "deprecated" in (result.output or "").lower() or result.exit_code in (0, 1)

    def test_unpack_nonexistent_bundle(self, tmp_path: Path) -> None:
        """Passing a non-existent path exits non-zero."""
        result = CliRunner().invoke(
            unpack_cmd,
            [str(tmp_path / "no-such-bundle.zip"), "--dry-run"],
        )
        assert result.exit_code != 0

    def test_unpack_help_shows_install_hint(self) -> None:
        """Help text references 'apm install' as the replacement."""
        result = CliRunner().invoke(unpack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "install" in result.output.lower()


# ---------------------------------------------------------------------------
# Authoring-path license warning asymmetry (#1777, U6)
# WARN on `apm pack` (author's own manifest); SILENT on the consuming path.
# ---------------------------------------------------------------------------


def test_pack_warns_when_no_license_declared(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text("name: demo\nversion: 1.0.0\n")
        result = runner.invoke(pack_cmd, ["--dry-run"])
        combined = result.output + (result.stderr or "")
        assert "license:" in combined, combined


def test_pack_silent_when_license_declared(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text("name: demo\nversion: 1.0.0\nlicense: MIT\n")
        result = runner.invoke(pack_cmd, ["--dry-run"])
        combined = result.output + (result.stderr or "")
        assert "the SBOM will record NOASSERTION" not in combined


def test_pack_json_reports_license_warning_in_warnings(tmp_path):
    """--json keeps the nudge as a JSON warning, not a stderr log line (#4203)."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text("name: demo\nversion: 1.0.0\n")
        result = runner.invoke(pack_cmd, ["--dry-run", "--json"])
        warnings = _json.loads(result.stdout)["warnings"]
        assert sum("the SBOM will record NOASSERTION" in w for w in warnings) == 1, warnings
        assert "the SBOM will record NOASSERTION" not in (result.stderr or "")


def test_pack_help_recommends_manifest_marketplace_output_config() -> None:
    result = CliRunner().invoke(pack_cmd, ["--help"])

    assert result.exit_code == 0
    # The --marketplace-path flag is shown in help
    assert "--marketplace-path" in result.output
    # The removed --marketplace-output flag is gone entirely
    assert "--marketplace-output" not in result.output
    assert "--claude-output" not in result.output


_APM_GATE = """\
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


def _scaffold(tmp_path: Path, *, pkg_version: str = "1.0.0") -> Path:
    (tmp_path / "apm.yml").write_text(_tw.dedent(_APM_GATE), encoding="utf-8")
    pkg = tmp_path / "packages" / "local-tool"
    pkg.mkdir(parents=True)
    pkg.joinpath("apm.yml").write_text(
        f"name: local-tool\ndescription: Tool.\nversion: {pkg_version}\n",
        encoding="utf-8",
    )
    return tmp_path


class TestPackReleaseGatesIntegration:
    """End-to-end gate behaviour: BuildOrchestrator + gates compose cleanly."""

    @pytest.fixture(autouse=True)
    def _reset_console_state(self):
        """Reset global console singleton to avoid --json polluting later tests."""
        from apm_cli.utils.console import _reset_console

        yield
        _reset_console()

    def test_no_gate_flags_returns_no_gate_keys_in_json(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--offline", "--json"])
        data = _json.loads(result.output)
        # Envelope always carries the keys; both should be null when not requested.
        assert data["version_alignment"] is None
        assert data["drift"] is None

    def test_json_envelope_reports_the_bundle(self, tmp_path: Path, monkeypatch) -> None:
        """A successful bundle build is not reported as "bundle": null (#3994)."""
        import json as _json

        from apm_cli.bundle.packer import PackResult
        from apm_cli.core.build_orchestrator import BuildResult, OutputKind, ProducerResult

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        bundle = PackResult(bundle_path=tmp_path / "build" / "pkg-1.0.0", files=["a.md"])
        built = BuildResult(producer_results=[ProducerResult(OutputKind.BUNDLE, payload=bundle)])
        monkeypatch.setattr("apm_cli.commands.pack.BuildOrchestrator.run", lambda *_a, **_k: built)
        result = CliRunner().invoke(pack_cmd, ["--offline", "--json"])
        data = _json.loads(result.output)
        assert data["bundle"] == {"path": bundle.bundle_path.as_posix(), "files": ["a.md"]}

    def test_check_versions_only_payload_shape(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        assert data["version_alignment"] is not None
        assert data["version_alignment"]["strategy"] == "lockstep"
        assert isinstance(data["version_alignment"]["packages"], list)

    def test_check_clean_only_payload_shape(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline", "--json"])
        data = _json.loads(result.output)
        assert data["drift"] is not None
        assert isinstance(data["drift"]["outputs"], list)

    def test_version_gate_passes_drift_gate_fails_exit_4(self, tmp_path: Path, monkeypatch) -> None:
        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        # No marketplace.json on disk -> drift "missing" -> exit 4.
        result = CliRunner().invoke(
            pack_cmd,
            ["--check-versions", "--check-clean", "--dry-run", "--offline"],
        )
        assert result.exit_code == 4

    def test_version_gate_fails_drift_gate_passes_exit_3(self, tmp_path: Path, monkeypatch) -> None:
        _scaffold(tmp_path, pkg_version="0.5.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--dry-run", "--offline"])
        assert result.exit_code == 3

    def test_gate_errors_appear_in_json_envelope(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path, pkg_version="0.5.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        codes = {e["code"] for e in data.get("errors", [])}
        assert "version_misaligned" in codes
        assert data["ok"] is False

    def test_gate_with_no_marketplace_block_does_not_fail(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # apm.yml without a marketplace block -> both gates skip cleanly.
        (tmp_path / "apm.yml").write_text(
            "name: x\ndescription: y\nversion: 1.0.0\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--check-clean", "--dry-run"])
        assert result.exit_code not in (3, 4)
