"""Integration tests for ``apm marketplace check``.

Strategy
--------
Tests write a real marketplace.yml and invoke the ``check`` command via
CliRunner with RefResolver.list_remote_refs patched.

Scenarios covered:
- All entries resolvable -> exit 0.
- One entry unreachable -> exit 1 with error in output.
- Offline mode reports cached-only error per-row.
- Missing marketplace.yml -> exit 1.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import check
from apm_cli.marketplace.errors import GitLsRemoteError, OfflineMissError
from apm_cli.marketplace.ref_resolver import RemoteRef


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

_CHECK_YML = """\
name: check-test
description: Marketplace for check tests
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: plugin-a
    source: org/plugin-a
    version: "^1.0.0"
    tags:
      - test
  - name: plugin-b
    source: org/plugin-b
    ref: v2.0.0
    tags:
      - test
"""

_SINGLE_ENTRY_YML = """\
name: single
description: Single-entry check
version: 1.0.0
owner:
  name: Test Org
packages:
  - name: only-plugin
    source: org/only-plugin
    version: "^1.0.0"
    tags:
      - test
"""


def _refs_ok(owner_repo: str):
    """All packages have satisfying refs."""
    return {
        "org/plugin-a": [
            RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40),
            RemoteRef(name="refs/tags/v1.2.0", sha="b" * 40),
        ],
        "org/plugin-b": [
            RemoteRef(name="refs/tags/v2.0.0", sha="c" * 40),
        ],
        "org/only-plugin": [
            RemoteRef(name="refs/tags/v1.0.0", sha="a" * 40),
        ],
    }.get(owner_repo, [])


def _refs_plugin_a_missing(owner_repo: str):
    """plugin-a returns no refs (simulating a remote error via empty list)."""
    if owner_repo == "org/plugin-a":
        return []
    return _refs_ok(owner_repo)


def _run_check(tmp_path: Path, yml_content, extra_args=(), side_effect=None):
    if side_effect is None:
        side_effect = _refs_ok

    runner = CliRunner()
    (tmp_path / "marketplace.yml").write_text(yml_content, encoding="utf-8")

    with runner.isolated_filesystem(temp_dir=str(tmp_path)) as cwd:
        import shutil
        shutil.copy(str(tmp_path / "marketplace.yml"), cwd + "/marketplace.yml")
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=side_effect,
        ):
            result = runner.invoke(check, list(extra_args), catch_exceptions=False)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckAllReachable:
    """When all entries are resolvable, check exits 0."""

    def test_exit_code_zero_all_ok(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        assert result.exit_code == 0

    def test_success_message_in_output(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        combined = result.output
        assert "OK" in combined or "[+]" in combined

    def test_package_names_appear(self, tmp_path: Path):
        result = _run_check(tmp_path, _CHECK_YML, side_effect=_refs_ok)
        assert "plugin-a" in result.output
        assert "plugin-b" in result.output


class TestCheckOneUnreachable:
    """When one entry is unreachable, check exits 1."""

    def _side_effect_raise(self, owner_repo: str):
        if owner_repo == "org/plugin-a":
            raise GitLsRemoteError(
                package="plugin-a",
                summary="git ls-remote failed for 'org/plugin-a'",
                hint="Check network.",
            )
        return _refs_ok(owner_repo)

    def test_exit_code_one_on_unreachable(self, tmp_path: Path):
        result = _run_check(
            tmp_path, _CHECK_YML, side_effect=self._side_effect_raise
        )
        assert result.exit_code == 1

    def test_error_summary_in_output(self, tmp_path: Path):
        result = _run_check(
            tmp_path, _CHECK_YML, side_effect=self._side_effect_raise
        )
        combined = result.output
        # Either [x] marker or "issues" text should appear
        assert "[x]" in combined or "issue" in combined.lower() or "error" in combined.lower()

    def test_error_entry_named_in_output(self, tmp_path: Path):
        result = _run_check(
            tmp_path, _CHECK_YML, side_effect=self._side_effect_raise
        )
        assert "plugin-a" in result.output

    def test_other_entries_still_reported(self, tmp_path: Path):
        """Entries that succeed must still appear in the output."""
        result = _run_check(
            tmp_path, _CHECK_YML, side_effect=self._side_effect_raise
        )
        assert "plugin-b" in result.output


class TestCheckOffline:
    """--offline flag reports cached-only status."""

    @staticmethod
    def _raise_offline_miss(owner_repo: str):
        """Simulate RefResolver in offline mode with an empty cache."""
        raise OfflineMissError(package="", remote=owner_repo)

    def test_offline_mode_exits_nonzero_on_empty_cache(self, tmp_path: Path):
        """Offline with empty cache must fail (OfflineMissError per entry)."""
        result = _run_check(
            tmp_path,
            _CHECK_YML,
            extra_args=["--offline"],
            side_effect=self._raise_offline_miss,
        )
        # No cache -> all entries fail; exit 1 expected
        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_offline_mode_does_not_crash(self, tmp_path: Path):
        """Offline flag must not produce a Python traceback."""
        result = _run_check(
            tmp_path,
            _CHECK_YML,
            extra_args=["--offline"],
            side_effect=self._raise_offline_miss,
        )
        assert "Traceback" not in result.output


class TestCheckMissingYml:
    """check without marketplace.yml exits 1."""

    def test_missing_yml_exits_1(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(check, [], catch_exceptions=False)
        assert result.exit_code == 1
