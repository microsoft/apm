"""Tests for ``apm marketplace build`` subcommand."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.builder import BuildOptions, BuildReport, ResolvedPackage
from apm_cli.marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40

_BASIC_YML = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: pkg-alpha
        source: acme-org/pkg-alpha
        version: "^1.0.0"
        description: Alpha package
        tags: [testing]
      - name: pkg-beta
        source: acme-org/pkg-beta
        version: "~2.0.0"
        tags: [utility]
""")


def _make_report(
    resolved=None, errors=(), dry_run=False,
    unchanged=0, added=2, updated=0, removed=0,
):
    """Build a fake BuildReport."""
    if resolved is None:
        resolved = (
            ResolvedPackage(
                name="pkg-alpha",
                source_repo="acme-org/pkg-alpha",
                subdir=None,
                ref="v1.2.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=("testing",),
                is_prerelease=False,
            ),
            ResolvedPackage(
                name="pkg-beta",
                source_repo="acme-org/pkg-beta",
                subdir="src/plugin",
                ref="v2.0.1",
                sha=_SHA_B,
                requested_version="~2.0.0",
                tags=("utility",),
                is_prerelease=False,
            ),
        )
    return BuildReport(
        resolved=resolved,
        errors=errors,
        warnings=(),
        unchanged_count=unchanged,
        added_count=added,
        updated_count=updated,
        removed_count=removed,
        output_path=Path("marketplace.json"),
        dry_run=dry_run,
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def yml_cwd(tmp_path, monkeypatch):
    """Set CWD to tmp_path and write a valid marketplace.yml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBuildHappyPath:
    """build command -- success scenarios."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_basic_build_success(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 0
        assert "Built marketplace.json" in result.output
        assert "2 packages" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_build_table_contains_package_names(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 0
        assert "pkg-alpha" in result.output
        assert "pkg-beta" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_build_table_contains_version_refs(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        result = runner.invoke(marketplace, ["build"])
        assert "v1.2.0" in result.output
        assert "v2.0.1" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_build_table_shows_sha_prefix(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        result = runner.invoke(marketplace, ["build"])
        assert _SHA_A[:8] in result.output
        assert _SHA_B[:8] in result.output


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestBuildDryRun:
    """build --dry-run scenarios."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_dry_run_message(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(dry_run=True)

        result = runner.invoke(marketplace, ["build", "--dry-run"])
        assert result.exit_code == 0
        assert "Dry run" in result.output
        assert "not written" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_dry_run_no_built_message(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(dry_run=True)

        result = runner.invoke(marketplace, ["build", "--dry-run"])
        assert "Built marketplace.json" not in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_dry_run_passes_option_to_builder(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(dry_run=True)

        runner.invoke(marketplace, ["build", "--dry-run"])
        opts = MockBuilder.call_args[1].get("options") or MockBuilder.call_args[0][1]
        assert opts.dry_run is True


# ---------------------------------------------------------------------------
# Flag forwarding
# ---------------------------------------------------------------------------


class TestBuildFlags:
    """Verify CLI flags are forwarded to BuildOptions."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_offline_flag(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        runner.invoke(marketplace, ["build", "--offline"])
        opts = MockBuilder.call_args[1].get("options") or MockBuilder.call_args[0][1]
        assert opts.offline is True

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_include_prerelease_flag(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        runner.invoke(marketplace, ["build", "--include-prerelease"])
        opts = MockBuilder.call_args[1].get("options") or MockBuilder.call_args[0][1]
        assert opts.include_prerelease is True

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_verbose_flag(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report()

        result = runner.invoke(marketplace, ["build", "--verbose"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Missing / bad marketplace.yml
# ---------------------------------------------------------------------------


class TestBuildMissingYml:
    """build command -- no marketplace.yml."""

    def test_missing_yml_exits_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "No marketplace.yml found" in result.output

    def test_missing_yml_suggests_init(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["build"])
        assert "init" in result.output


class TestBuildSchemaError:
    """build command -- invalid marketplace.yml."""

    def test_schema_error_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("not: valid\n", encoding="utf-8")
        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 2
        assert "schema error" in result.output.lower() or "required" in result.output.lower()

    def test_bad_yaml_syntax_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(":\n  - !!invalid", encoding="utf-8")
        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Build errors
# ---------------------------------------------------------------------------


class TestBuildErrors:
    """build command -- BuildError subclass handling."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_no_matching_version_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = NoMatchingVersionError(
            "pkg-alpha", "^1.0.0"
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "pkg-alpha" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_ref_not_found_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = RefNotFoundError(
            "pkg-alpha", "v99.0.0", "acme-org/pkg-alpha"
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_git_ls_remote_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = GitLsRemoteError(
            package="pkg-alpha",
            summary="Authentication failed",
            hint="Check your GITHUB_TOKEN",
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "Authentication failed" in result.output
        assert "GITHUB_TOKEN" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_offline_miss_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = OfflineMissError(
            package="pkg-alpha", remote="acme-org/pkg-alpha"
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "offline" in result.output.lower() or "cache" in result.output.lower()

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_head_not_allowed_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = HeadNotAllowedError(
            package="pkg-alpha", ref="main"
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "main" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_generic_build_error(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = BuildError(
            "Something unexpected", package="pkg-alpha"
        )

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Empty packages
# ---------------------------------------------------------------------------


class TestBuildEdgeCases:
    """Edge cases for the build command."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_empty_packages_list(self, MockBuilder, runner, yml_cwd):
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(resolved=(), added=0)

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 0
        assert "0 packages" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_single_package(self, MockBuilder, runner, yml_cwd):
        single = (
            ResolvedPackage(
                name="only-one",
                source_repo="acme-org/only-one",
                subdir=None,
                ref="v3.0.0",
                sha=_SHA_A,
                requested_version="^3.0.0",
                tags=(),
                is_prerelease=False,
            ),
        )
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(resolved=single, added=1)

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 0
        assert "only-one" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_prerelease_package(self, MockBuilder, runner, yml_cwd):
        pre = (
            ResolvedPackage(
                name="beta-pkg",
                source_repo="acme-org/beta-pkg",
                subdir=None,
                ref="v2.0.0-rc.1",
                sha=_SHA_A,
                requested_version="^2.0.0",
                tags=(),
                is_prerelease=True,
            ),
        )
        mock_inst = MockBuilder.return_value
        mock_inst.build.return_value = _make_report(resolved=pre, added=1)

        result = runner.invoke(marketplace, ["build", "--include-prerelease"])
        assert result.exit_code == 0
        assert "v2.0.0-rc.1" in result.output


# ---------------------------------------------------------------------------
# Verbose traceback (L3)
# ---------------------------------------------------------------------------


class TestBuildVerboseTraceback:
    """build --verbose -- traceback on unexpected failure."""

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_verbose_shows_traceback_on_unexpected_error(
        self, MockBuilder, runner, yml_cwd
    ):
        """When --verbose is passed and build raises an unexpected error,
        stderr should contain the full traceback."""
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = RuntimeError("unexpected internal failure")

        result = runner.invoke(marketplace, ["build", "--verbose"])
        assert result.exit_code == 1
        assert "Traceback" in result.output
        assert "unexpected internal failure" in result.output

    @patch("apm_cli.commands.marketplace.MarketplaceBuilder")
    def test_no_traceback_without_verbose(self, MockBuilder, runner, yml_cwd):
        """Without --verbose the traceback is suppressed."""
        mock_inst = MockBuilder.return_value
        mock_inst.build.side_effect = RuntimeError("unexpected internal failure")

        result = runner.invoke(marketplace, ["build"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "Build failed" in result.output
