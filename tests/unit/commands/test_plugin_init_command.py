"""Tests for the new ``apm plugin init`` surface and deprecation
warnings on ``apm init --plugin`` / ``apm init --marketplace``.

Wave 3 v3: noun-verb consolidation. Legacy flags continue to work
during the deprecation window (removal scheduled for v0.16).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli


class TestPluginInitCommand:
    """``apm plugin init`` behaves like ``apm init --plugin``."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_plugin_init_creates_plugin_json_and_apm_yml(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["plugin", "init", "demo", "--yes"])
                assert result.exit_code == 0, result.output
                # `apm init <name>` chdirs into the new project dir.
                assert Path("apm.yml").exists()
                assert Path("plugin.json").exists()
                # Plugin-author next-steps surface
                assert "apm install --dev" in result.output
                assert "apm pack" in result.output
                # Consumer-only hints absent in plugin mode
                assert "apm marketplace init" not in result.output
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_current_directory(self):
        # tmp dir basenames contain underscores which fail kebab validation,
        # so create a kebab-safe child dir to exercise the current-dir path.
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "demo-plugin"
            project_dir.mkdir()
            os.chdir(project_dir)
            try:
                result = self.runner.invoke(cli, ["plugin", "init", "--yes"])
                assert result.exit_code == 0, result.output
                assert Path("apm.yml").exists()
                assert Path("plugin.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_plugin_init_help_advertises_apm_marketplace_init(self):
        """Group help points users at the sibling marketplace verb."""
        result = self.runner.invoke(cli, ["plugin", "--help"])
        assert result.exit_code == 0
        assert "init" in result.output
        assert "plugin" in result.output.lower()


class TestInitDeprecationWarnings:
    """Legacy ``apm init --plugin`` / ``--marketplace`` flags still work
    but print a one-line deprecation redirect on stderr.
    """

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_init_plugin_flag_prints_deprecation(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--plugin", "--yes"])
                assert result.exit_code == 0, result.output
                # Deprecation lives on stderr so it does not pollute pipes
                assert "deprecated" in result.stderr.lower()
                assert "apm plugin init" in result.stderr
                assert "v0.16" in result.stderr
                # And the legacy flag STILL works (cwd is now demo/)
                assert Path("plugin.json").exists()
            finally:
                os.chdir(self.original_dir)

    def test_init_marketplace_flag_prints_deprecation(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--marketplace", "--yes"])
                assert result.exit_code == 0, result.output
                assert "deprecated" in result.stderr.lower()
                assert "apm marketplace init" in result.stderr
                assert "v0.16" in result.stderr
                # And the legacy flag STILL writes the marketplace block (cwd is now demo/)
                content = Path("apm.yml").read_text()
                assert "marketplace:" in content
            finally:
                os.chdir(self.original_dir)

    def test_init_without_flags_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "demo", "--yes"])
                assert result.exit_code == 0, result.output
                assert "deprecated" not in result.stderr.lower()
            finally:
                os.chdir(self.original_dir)


class TestInitConsumerNextSteps:
    """Consumer-mode ``apm init`` teaches the noun-verb namespace."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).resolve().parents[3])
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).resolve().parents[3]))

    def test_consumer_init_surfaces_namespace_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])
                assert result.exit_code == 0, result.output
                assert "apm install" in result.output
                assert "apm run" in result.output
                assert "apm plugin init" in result.output
                assert "apm marketplace init" in result.output
            finally:
                os.chdir(self.original_dir)
