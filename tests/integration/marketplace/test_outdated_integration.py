"""Integration tests for ``apm marketplace outdated``.

Strategy
--------
Tests write a real marketplace.yml and invoke the ``outdated`` command
via CliRunner with RefResolver.list_remote_refs patched.

The test matrix covers:
- version-range entries with upgrades available in and out of range.
- Ref-pinned entries are skipped with a note.
- Exit code is always 0 (outdated is informational).
- Offline mode.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import outdated
from apm_cli.marketplace.ref_resolver import RemoteRef


# ---------------------------------------------------------------------------
# Fixtures / YAML content
# ---------------------------------------------------------------------------

_OUTDATED_YML = """\
name: outdated-test
description: Marketplace for outdated tests
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: alpha
    source: org/alpha
    version: "^1.0.0"
    tags:
      - test
  - name: beta
    source: org/beta
    version: "^2.0.0"
    tags:
      - test
  - name: pinned
    source: org/pinned
    ref: v1.0.0
    tags:
      - test
"""


def _refs_alpha():
    """alpha: v1.0.0 (current range), v1.1.0 (upgrade in range), v2.0.0 (major)."""
    return [
        RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40),
        RemoteRef(name="refs/tags/v1.1.0", sha="b" * 40),
        RemoteRef(name="refs/tags/v2.0.0", sha="c" * 40),
    ]


def _refs_beta():
    """beta: v2.0.0 only -- no newer version available."""
    return [
        RemoteRef(name="refs/tags/v2.0.0", sha="d" * 40),
    ]


def _refs_pinned():
    """pinned: one tag (the pinned one); should be skipped by outdated."""
    return [
        RemoteRef(name="refs/tags/v1.0.0", sha="e" * 40),
    ]


def _side_effect(owner_repo: str):
    return {
        "org/alpha": _refs_alpha(),
        "org/beta": _refs_beta(),
        "org/pinned": _refs_pinned(),
    }.get(owner_repo, [])


def _run_outdated(tmp_path: Path, extra_args=(), yml_content=_OUTDATED_YML):
    runner = CliRunner()
    yml = tmp_path / "marketplace.yml"
    yml.write_text(yml_content, encoding="utf-8")
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        # Symlink the written marketplace.yml into the isolated FS CWD
        import os
        import shutil
        cwd = Path(os.getcwd())
        shutil.copy(str(yml), str(cwd / "marketplace.yml"))
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=_side_effect,
        ):
            result = runner.invoke(outdated, list(extra_args), catch_exceptions=False)
    return result


def _run_outdated_cwd(extra_args=()):
    """Run outdated in a preconfigured isolated FS (caller sets up CWD)."""
    runner = CliRunner()
    with patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        side_effect=_side_effect,
    ):
        result = runner.invoke(outdated, list(extra_args), catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutdatedVersionRanges:
    """Verify upgrade classification for version-range entries."""

    def test_exit_code_always_zero(self, tmp_path: Path):
        """outdated must exit 0 regardless of upgrade availability."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch(
                "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
                side_effect=_side_effect,
            ):
                result = runner.invoke(outdated, [], catch_exceptions=False)
        assert result.exit_code == 0

    def test_package_names_appear_in_output(self, tmp_path: Path):
        """Output must mention every package entry."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch(
                "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
                side_effect=_side_effect,
            ):
                result = runner.invoke(outdated, [], catch_exceptions=False)
        combined = result.output
        assert "alpha" in combined
        assert "beta" in combined
        assert "pinned" in combined

    def test_pinned_entry_is_skipped(self, tmp_path: Path):
        """Ref-pinned entries must be reported as skipped (not as upgrades)."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch(
                "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
                side_effect=_side_effect,
            ):
                result = runner.invoke(outdated, [], catch_exceptions=False)
        combined = result.output
        # Skipped entries should show [i] or "Pinned" in the table
        assert "pinned" in combined
        assert ("[i]" in combined or "Pinned" in combined or "skipped" in combined.lower())

    def test_upgrade_available_in_range(self, tmp_path: Path):
        """alpha ^1.0.0 has v1.1.0 available in range; output must show [!] or similar."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch(
                "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
                side_effect=_side_effect,
            ):
                result = runner.invoke(outdated, [], catch_exceptions=False)
        combined = result.output
        # alpha should show an upgrade marker; v1.1.0 should appear
        assert "v1.1.0" in combined

    def test_major_outside_range_is_noted(self, tmp_path: Path):
        """alpha has v2.0.0 which is outside ^1.0.0; it should appear in output."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            with patch(
                "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
                side_effect=_side_effect,
            ):
                result = runner.invoke(outdated, [], catch_exceptions=False)
        combined = result.output
        # v2.0.0 is the latest overall; it should appear in the overall-latest column
        assert "v2.0.0" in combined


class TestOutdatedMissingYml:
    """outdated without marketplace.yml exits 1."""

    def test_missing_yml_exits_1(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(outdated, [], catch_exceptions=False)
        assert result.exit_code == 1


class TestOutdatedOffline:
    """--offline flag is accepted and does not crash."""

    def test_offline_flag_exits_zero_or_one_not_two(self, tmp_path: Path):
        """--offline with empty cache must not crash with exit 2."""
        runner = CliRunner()
        (tmp_path / "marketplace.yml").write_text(_OUTDATED_YML, encoding="utf-8")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
            import shutil
            shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
            result = runner.invoke(outdated, ["--offline"], catch_exceptions=False)
        # Offline miss is reported per-row; exit code should still be 0 or 1, not 2
        assert result.exit_code != 2
        assert "Traceback" not in result.output
