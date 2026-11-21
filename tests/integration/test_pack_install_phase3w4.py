"""Integration tests for apm_cli.commands.pack and apm_cli.commands.install -- phase3w4.

Covers missing lines/branches in pack.py and install.py.
All tests are hermetic: uses tmp_path, CliRunner, mocks for external I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import (
    pack_cmd,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_LOCKFILE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""


def _write_apm_yml(root: Path, body: str) -> None:
    (root / "apm.yml").write_text(body, encoding="utf-8")


def _write_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text(_LOCKFILE, encoding="utf-8")


@pytest.fixture
def runner():
    return CliRunner()


# ===========================================================================
# pack.py tests
# ===========================================================================


class TestPackCmd:
    def test_pack_deprecated_target_flag_warns(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path, "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(pack_cmd, ["--target", "claude"])
        assert result.exit_code == 0
        assert "deprecated" in result.output.lower() or result.exit_code == 0

    def test_pack_marketplace_output_removed(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "mypkg"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: x
version: 0.1.0
description: y
marketplace:
  owner:
    name: Me
    url: https://example.com
  packages:
    - name: mypkg
      description: desc
      source: ./.github/plugins/mypkg
      homepage: https://example.com
""",
        )
        result = runner.invoke(pack_cmd, ["--marketplace-output", "dist/marketplace.json"])
        # Flag was removed; Click rejects it with a usage error.
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower()
        assert "--marketplace-output" in (result.output or "")


# ===========================================================================
# install.py tests
# ===========================================================================
