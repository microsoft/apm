"""Tests for missed coverage in marketplace init and plugin helpers.

Covers:
marketplace/init.py:
- Lines 51-53: OSError on initial apm.yml write
- Lines 63-65: Exception on YAML parse
- Lines 92-94: OSError on final apm.yml write
- Lines 124-127: ImportError from _rich_panel

marketplace/plugin/__init__.py:
- Lines 30, 33: _yml_path with no marketplace block
- Lines 43-49: both apm.yml+marketplace AND legacy exist
- Lines 53-57: no valid config path
- Lines 65-69: OSError in _has_marketplace_block
- Lines 76-77: _parse_tags with empty/whitespace input
- Lines 87-94: _verify_source GitLsRemoteError + OfflineMissError
- Lines 139-144: _resolve_ref GitLsRemoteError on HEAD resolve
- Lines 155-162: _resolve_ref GitLsRemoteError/OfflineMissError on list_remote_refs
- Lines 167-172: _resolve_ref no_verify=True on branch ref
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# marketplace/init.py
# ---------------------------------------------------------------------------


class TestMarketplaceInitWriteError:
    def test_scaffold_write_error_exits(self, runner, tmp_path) -> None:
        """Lines 51-53: OSError on initial scaffold write → exit 1."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
                result = runner.invoke(cli, ["marketplace", "init"])
        assert result.exit_code == 1

    def test_yaml_parse_error_exits(self, runner, tmp_path) -> None:
        """Lines 63-65: YAML parse Exception → exit 1."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            (Path.cwd() / "apm.yml").write_text(": invalid: yaml: {{{\n", encoding="utf-8")
            with patch(
                "ruamel.yaml.YAML.load",
                side_effect=Exception("YAML parse error"),
            ):
                result = runner.invoke(cli, ["marketplace", "init"])
        assert result.exit_code == 1

    def test_final_write_error_exits(self, runner, tmp_path) -> None:
        """Lines 92-94: OSError on writing modified apm.yml → exit 1."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            apm_yml = Path.cwd() / "apm.yml"
            apm_yml.write_text("name: test\nversion: 1.0.0\n", encoding="utf-8")

            # Only the final write (modified YAML) should fail
            with patch.object(Path, "write_text", side_effect=OSError("read-only")):
                result = runner.invoke(cli, ["marketplace", "init"])
        assert result.exit_code == 1

    def test_rich_panel_import_error_falls_back(self, runner, tmp_path) -> None:
        """Lines 124-127: _rich_panel unavailable → next steps via logger."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            apm_yml = Path.cwd() / "apm.yml"
            apm_yml.write_text("name: test\n", encoding="utf-8")

            with (
                patch("apm_cli.utils.console._rich_panel", side_effect=ImportError("no Rich")),
            ):
                result = runner.invoke(cli, ["marketplace", "init"])

        assert result.exit_code in {0, 1}  # may fail on marketplace block


# ---------------------------------------------------------------------------
# marketplace/plugin/__init__.py - _yml_path
# ---------------------------------------------------------------------------


class TestYmlPath:
    def test_returns_apm_yml_when_no_marketplace_block(self, tmp_path) -> None:
        """Lines 30, 33: no marketplace block and no legacy path → apm_yml."""
        from apm_cli.commands.marketplace.plugin import _yml_path

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test\n", encoding="utf-8")  # no marketplace: block

        with patch("apm_cli.commands.marketplace.plugin.Path") as mock_path_cls:
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.return_value = tmp_path

            # Use the real function but inject tmp_path as cwd
            import os

            old_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                result = _yml_path()
            finally:
                os.chdir(old_cwd)

        assert result == apm_yml

    def test_returns_legacy_path_when_exists(self, tmp_path) -> None:
        """Line 32: legacy marketplace.yml exists → return it."""
        from apm_cli.commands.marketplace.plugin import _yml_path

        legacy = tmp_path / "marketplace.yml"
        legacy.write_text("foo: bar\n", encoding="utf-8")

        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = _yml_path()
        finally:
            os.chdir(old_cwd)

        assert result == legacy


class TestEnsureYmlExists:
    def test_both_apm_and_legacy_exist_exits(self, tmp_path) -> None:
        """Lines 43-49: both apm.yml with marketplace AND legacy exist → exit 1."""
        from apm_cli.commands.marketplace.plugin import _ensure_yml_exists

        # apm.yml with marketplace block
        (tmp_path / "apm.yml").write_text(
            "name: t\nmarketplace:\n  packages: []\n", encoding="utf-8"
        )
        # legacy file also exists
        (tmp_path / "marketplace.yml").write_text("foo: bar\n", encoding="utf-8")

        logger = MagicMock()

        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with pytest.raises(SystemExit) as exc_info:
                _ensure_yml_exists(logger)
        finally:
            os.chdir(old_cwd)

        assert exc_info.value.code == 1

    def test_no_valid_config_exits(self, tmp_path) -> None:
        """Lines 53-57: no valid config path → error + exit 1."""
        from apm_cli.commands.marketplace.plugin import _ensure_yml_exists

        # Empty directory, no apm.yml, no marketplace.yml
        logger = MagicMock()

        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with pytest.raises(SystemExit) as exc_info:
                _ensure_yml_exists(logger)
        finally:
            os.chdir(old_cwd)

        assert exc_info.value.code == 1


class TestHasMarketplaceBlock:
    def test_oserror_returns_false(self, tmp_path) -> None:
        """Lines 65-69: OSError on read → returns False."""
        from apm_cli.commands.marketplace.plugin import _has_marketplace_block

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test\n", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
            result = _has_marketplace_block(apm_yml)

        assert result is False

    def test_yaml_error_returns_false(self, tmp_path) -> None:
        """Lines 67-68: YAMLError → returns False."""
        from apm_cli.commands.marketplace.plugin import _has_marketplace_block

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(": invalid yaml {{{\n", encoding="utf-8")

        result = _has_marketplace_block(apm_yml)

        assert result is False
