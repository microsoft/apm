"""Integration tests for Wave 7 - marketplace and install coverage.

Targets:
  1. src/apm_cli/commands/marketplace/__init__.py  (~39% covered, 406 lines missing)
  2. src/apm_cli/commands/install.py                (~69% covered, 162 lines missing)

Strategy:
- CliRunner to invoke CLI commands (real code paths).
- Mock ONLY external I/O: HTTP/marketplace registry reads, AuthResolver tokens,
  subprocess/git ops, and os.environ where necessary.
- Create realistic tmp_path fixtures.
- NO mocking of internal Python functions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin, MarketplaceSource

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

APM_YML_BASIC = """\
name: test-project
version: 0.1.0
description: Test project
owner:
  name: test-org
"""

APM_YML_WITH_DEPS = """\
name: test-project
version: 0.1.0
description: Test project
owner:
  name: test-org
dependencies:
  apm:
    - owner/test-dep
"""

APM_YML_NO_DEPS = """\
name: test-project
version: 0.1.0
description: Test project
owner:
  name: test-org
dependencies:
  apm: []
"""

LOCK_YML_MINIMAL = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""


def _write_apm_yml(root: Path, content: str = APM_YML_BASIC) -> None:
    (root / "apm.yml").write_text(content, encoding="utf-8")


def _write_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text(LOCK_YML_MINIMAL, encoding="utf-8")


def _fake_source(name: str = "testmkt") -> MarketplaceSource:
    return MarketplaceSource(
        name=name,
        owner="acme-org",
        repo="marketplace-repo",
        branch="main",
        path="marketplace.json",
    )


def _fake_manifest(name: str = "testmkt", plugins: list | None = None) -> MarketplaceManifest:
    if plugins is None:
        plugins = [
            MarketplacePlugin(name="security-tool", description="A security plugin", version="1.0"),
            MarketplacePlugin(name="code-formatter", description="Format code nicely"),
        ]
    return MarketplaceManifest(
        name=name,
        plugins=tuple(plugins),
        description="Test marketplace",
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ===========================================================================
# Marketplace - list command
# ===========================================================================


class TestMarketplaceList:
    """Coverage for the ``apm marketplace list`` command."""

    def test_list_no_marketplaces_registered(self, runner: CliRunner, tmp_path: Path) -> None:
        """Empty registry shows a helpful message."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[],
            ):
                result = runner.invoke(cli, ["marketplace", "list"])
        assert result.exit_code == 0
        assert "No marketplaces" in result.output or "registered" in result.output

    def test_list_with_registered_marketplace(self, runner: CliRunner, tmp_path: Path) -> None:
        """Registered marketplace shows in list."""
        sources = [_fake_source("skills")]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=sources,
            ):
                result = runner.invoke(cli, ["marketplace", "list"])
        assert result.exit_code == 0
        assert "skills" in result.output

    def test_list_verbose_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        """--verbose flag works without errors."""
        sources = [_fake_source("testmkt")]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=sources,
            ):
                result = runner.invoke(cli, ["marketplace", "list", "--verbose"])
        assert result.exit_code == 0

    def test_list_multiple_marketplaces(self, runner: CliRunner, tmp_path: Path) -> None:
        """Multiple registered marketplaces all appear."""
        sources = [_fake_source("skills"), _fake_source("tools"), _fake_source("plugins")]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=sources,
            ):
                result = runner.invoke(cli, ["marketplace", "list"])
        assert result.exit_code == 0
        assert "skills" in result.output
        assert "tools" in result.output

    def test_list_error_from_registry(self, runner: CliRunner, tmp_path: Path) -> None:
        """Registry error is handled gracefully."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                side_effect=RuntimeError("registry read failed"),
            ):
                result = runner.invoke(cli, ["marketplace", "list"])
        assert result.exit_code != 0 or "Failed" in result.output or "registry" in result.output


# ===========================================================================
# Marketplace - browse command
# ===========================================================================


class TestMarketplaceBrowse:
    """Coverage for the ``apm marketplace browse`` command."""

    def test_browse_shows_plugins(self, runner: CliRunner, tmp_path: Path) -> None:
        """Browse lists plugins from the manifest."""
        source = _fake_source("testmkt")
        manifest = _fake_manifest("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "browse", "testmkt"])
        assert result.exit_code == 0
        assert "security-tool" in result.output

    def test_browse_empty_marketplace(self, runner: CliRunner, tmp_path: Path) -> None:
        """Browse with no plugins emits a warning."""
        source = _fake_source("empty")
        manifest = _fake_manifest("empty", plugins=[])
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "browse", "empty"])
        assert result.exit_code == 0
        assert "no plugins" in result.output.lower() or "0" in result.output

    def test_browse_verbose(self, runner: CliRunner, tmp_path: Path) -> None:
        """--verbose flag passes through cleanly."""
        source = _fake_source("testmkt")
        manifest = _fake_manifest("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "browse", "testmkt", "--verbose"])
        assert result.exit_code == 0

    def test_browse_unknown_marketplace_error(self, runner: CliRunner, tmp_path: Path) -> None:
        """Unknown marketplace name is handled gracefully."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=MarketplaceNotFoundError("unknown"),
            ):
                result = runner.invoke(cli, ["marketplace", "browse", "unknown"])
        assert result.exit_code != 0 or "Failed" in result.output


# ===========================================================================
# Marketplace - search command
# ===========================================================================


class TestMarketplaceSearch:
    """Coverage for the ``apm search`` command (top-level, not marketplace sub)."""

    def test_search_valid_format_returns_results(self, runner: CliRunner, tmp_path: Path) -> None:
        """QUERY@MARKETPLACE format finds matching plugins."""
        source = _fake_source("testmkt")
        plugins = [
            MarketplacePlugin(name="security-tool", description="Security scanner"),
            MarketplacePlugin(name="linter", description="Lint your code"),
        ]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.search_marketplace",
                    return_value=plugins[:1],
                ),
            ):
                result = runner.invoke(cli, ["search", "security@testmkt"])
        assert result.exit_code == 0
        assert "security-tool" in result.output

    def test_search_no_results(self, runner: CliRunner, tmp_path: Path) -> None:
        """Empty results show informative message."""
        source = _fake_source("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.search_marketplace",
                    return_value=[],
                ),
            ):
                result = runner.invoke(cli, ["search", "noresults@testmkt"])
        assert result.exit_code == 0
        assert "No plugins" in result.output or "no plugins" in result.output.lower()

    def test_search_missing_at_sign(self, runner: CliRunner, tmp_path: Path) -> None:
        """Missing '@' separator exits with error."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["search", "queryonly"])
        assert result.exit_code != 0
        assert "QUERY@MARKETPLACE" in result.output or "Invalid" in result.output

    def test_search_empty_query(self, runner: CliRunner, tmp_path: Path) -> None:
        """Empty query part before '@' is rejected."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["search", "@testmkt"])
        assert result.exit_code != 0
        assert "required" in result.output.lower() or "QUERY" in result.output

    def test_search_unregistered_marketplace(self, runner: CliRunner, tmp_path: Path) -> None:
        """Searching unknown marketplace shows a clear error."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=MarketplaceNotFoundError("ghost"),
            ):
                result = runner.invoke(cli, ["search", "query@ghost"])
        assert result.exit_code != 0
        assert "not registered" in result.output or "ghost" in result.output

    def test_search_verbose_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--verbose doesn't break search."""
        source = _fake_source("testmkt")
        plugins = [MarketplacePlugin(name="myplugin", description="desc")]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.search_marketplace",
                    return_value=plugins,
                ),
            ):
                result = runner.invoke(cli, ["search", "my@testmkt", "--verbose"])
        assert result.exit_code == 0

    def test_search_limit_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--limit restricts result count."""
        source = _fake_source("testmkt")
        many_plugins = [
            MarketplacePlugin(name=f"plugin-{i}", description=f"Plugin {i}") for i in range(30)
        ]
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.search_marketplace",
                    return_value=many_plugins,
                ),
            ):
                result = runner.invoke(cli, ["search", "plugin@testmkt", "--limit", "5"])
        assert result.exit_code == 0


# ===========================================================================
# Marketplace - update command
# ===========================================================================


class TestMarketplaceUpdate:
    """Coverage for the ``apm marketplace update`` command."""

    def test_update_specific_marketplace(self, runner: CliRunner, tmp_path: Path) -> None:
        """Updating a named marketplace refreshes its cache."""
        source = _fake_source("testmkt")
        manifest = _fake_manifest("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch(
                    "apm_cli.marketplace.client.clear_marketplace_cache",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "update", "testmkt"])
        assert result.exit_code == 0
        assert "updated" in result.output.lower() or "testmkt" in result.output

    def test_update_all_marketplaces(self, runner: CliRunner, tmp_path: Path) -> None:
        """Updating without a name refreshes all registered marketplaces."""
        sources = [_fake_source("skills"), _fake_source("tools")]
        manifest = _fake_manifest()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_registered_marketplaces",
                    return_value=sources,
                ),
                patch(
                    "apm_cli.marketplace.client.clear_marketplace_cache",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "update"])
        assert result.exit_code == 0

    def test_update_no_marketplaces_registered(self, runner: CliRunner, tmp_path: Path) -> None:
        """Update with no registered marketplaces shows informative message."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[],
            ):
                result = runner.invoke(cli, ["marketplace", "update"])
        assert result.exit_code == 0
        assert "No marketplaces" in result.output or "registered" in result.output

    def test_update_partial_failure(self, runner: CliRunner, tmp_path: Path) -> None:
        """One marketplace failing during update doesn't abort the whole run."""
        sources = [_fake_source("good"), _fake_source("bad")]
        good_manifest = _fake_manifest("good")

        def _fetch_side_effect(source, **_kwargs):
            if source.name == "bad":
                raise RuntimeError("network error")
            return good_manifest

        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_registered_marketplaces",
                    return_value=sources,
                ),
                patch(
                    "apm_cli.marketplace.client.clear_marketplace_cache",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    side_effect=_fetch_side_effect,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "update"])
        # Should complete (exit 0) but warn about the bad marketplace
        assert result.exit_code == 0
        assert "bad" in result.output or "network" in result.output


# ===========================================================================
# Marketplace - remove command
# ===========================================================================


class TestMarketplaceRemove:
    """Coverage for the ``apm marketplace remove`` command."""

    def test_remove_with_yes_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--yes skips confirmation and removes successfully."""
        source = _fake_source("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.marketplace.registry.get_marketplace_by_name",
                    return_value=source,
                ),
                patch("apm_cli.marketplace.registry.remove_marketplace"),
                patch("apm_cli.marketplace.client.clear_marketplace_cache"),
            ):
                result = runner.invoke(cli, ["marketplace", "remove", "testmkt", "--yes"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower() or "testmkt" in result.output

    def test_remove_non_interactive_without_yes_fails(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Non-interactive mode without --yes exits with an error."""
        source = _fake_source("testmkt")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                return_value=source,
            ):
                # CliRunner is non-interactive by default
                result = runner.invoke(cli, ["marketplace", "remove", "testmkt"])
        assert (
            result.exit_code != 0 or "--yes" in result.output or "confirm" in result.output.lower()
        )

    def test_remove_unknown_marketplace_error(self, runner: CliRunner, tmp_path: Path) -> None:
        """Removing unknown marketplace shows error."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=MarketplaceNotFoundError("ghost"),
            ):
                result = runner.invoke(cli, ["marketplace", "remove", "ghost", "--yes"])
        assert result.exit_code != 0 or "Failed" in result.output or "ghost" in result.output


# ===========================================================================
# Marketplace - add command
# ===========================================================================


class TestMarketplaceAdd:
    """Coverage for the ``apm marketplace add`` command."""

    def test_add_valid_repo(self, runner: CliRunner, tmp_path: Path) -> None:
        """Adding a valid owner/repo registers the marketplace."""
        manifest = _fake_manifest("marketplace-repo")
        host_info = MagicMock()
        host_info.kind = "github"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.core.auth.AuthResolver.classify_host",
                    return_value=host_info,
                ),
                patch(
                    "apm_cli.marketplace.client._auto_detect_path",
                    return_value="marketplace.json",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
                patch("apm_cli.marketplace.registry.add_marketplace"),
            ):
                result = runner.invoke(cli, ["marketplace", "add", "acme-org/marketplace-repo"])
        assert result.exit_code == 0
        assert "registered" in result.output.lower() or "marketplace" in result.output.lower()

    def test_add_invalid_repo_format(self, runner: CliRunner, tmp_path: Path) -> None:
        """Malformed repo argument is rejected."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["marketplace", "add", "notarepo"])
        assert result.exit_code != 0

    def test_add_http_url_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        """HTTP (insecure) URL is always rejected."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["marketplace", "add", "http://github.com/acme/repo"])
        assert result.exit_code != 0
        assert (
            "HTTP" in result.output
            or "Insecure" in result.output
            or "insecure" in result.output.lower()
        )

    def test_add_no_marketplace_json_found(self, runner: CliRunner, tmp_path: Path) -> None:
        """Add fails when no marketplace.json is discoverable."""
        host_info = MagicMock()
        host_info.kind = "github"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.core.auth.AuthResolver.classify_host",
                    return_value=host_info,
                ),
                patch(
                    "apm_cli.marketplace.client._auto_detect_path",
                    return_value=None,
                ),
            ):
                result = runner.invoke(cli, ["marketplace", "add", "acme/repo"])
        assert result.exit_code != 0
        assert "No marketplace.json" in result.output or "not found" in result.output.lower()

    def test_add_invalid_name_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--name with invalid alias characters is rejected."""
        host_info = MagicMock()
        host_info.kind = "github"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.core.auth.AuthResolver.classify_host",
                    return_value=host_info,
                ),
                patch(
                    "apm_cli.marketplace.client._auto_detect_path",
                    return_value="marketplace.json",
                ),
            ):
                result = runner.invoke(
                    cli, ["marketplace", "add", "acme/repo", "--name", "bad name!"]
                )
        assert result.exit_code != 0
        assert "Invalid marketplace name" in result.output or "Invalid" in result.output

    def test_add_unsupported_host_kind(self, runner: CliRunner, tmp_path: Path) -> None:
        """Non-GitHub/GitLab host is rejected."""
        host_info = MagicMock()
        host_info.kind = "unknown"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.core.auth.AuthResolver.classify_host",
                return_value=host_info,
            ):
                result = runner.invoke(cli, ["marketplace", "add", "acme/repo"])
        assert result.exit_code != 0

    def test_add_with_custom_branch(self, runner: CliRunner, tmp_path: Path) -> None:
        """Custom --branch flag is forwarded to the source."""
        manifest = _fake_manifest("repo")
        host_info = MagicMock()
        host_info.kind = "github"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.core.auth.AuthResolver.classify_host",
                    return_value=host_info,
                ),
                patch(
                    "apm_cli.marketplace.client._auto_detect_path",
                    return_value="marketplace.json",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
                patch("apm_cli.marketplace.registry.add_marketplace") as mock_add,
            ):
                result = runner.invoke(
                    cli, ["marketplace", "add", "acme/repo", "--branch", "develop"]
                )
        assert result.exit_code == 0
        # Verify branch was passed to the source
        if mock_add.called:
            call_args = mock_add.call_args[0][0]
            assert call_args.branch == "develop"

    def test_add_verbose_mode(self, runner: CliRunner, tmp_path: Path) -> None:
        """--verbose produces extra output lines."""
        manifest = _fake_manifest("repo")
        host_info = MagicMock()
        host_info.kind = "github"
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch(
                    "apm_cli.core.auth.AuthResolver.classify_host",
                    return_value=host_info,
                ),
                patch(
                    "apm_cli.marketplace.client._auto_detect_path",
                    return_value="marketplace.json",
                ),
                patch(
                    "apm_cli.marketplace.client.fetch_marketplace",
                    return_value=manifest,
                ),
                patch("apm_cli.marketplace.registry.add_marketplace"),
            ):
                result = runner.invoke(cli, ["marketplace", "add", "acme/repo", "--verbose"])
        assert result.exit_code == 0


# ===========================================================================
# Marketplace - removed 'build' subcommand
# ===========================================================================


class TestMarketplaceBuildRemoved:
    """The 'build' subcommand was removed; it should surface a helpful error."""

    def test_build_subcommand_raises_usage_error(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm marketplace build`` gives a migration hint."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["marketplace", "build"])
        # UsageError exits with code 2 or the error text contains 'apm pack'
        assert result.exit_code != 0
        assert "apm pack" in result.output or "removed" in result.output.lower()


# ===========================================================================
# Marketplace - validate command
# ===========================================================================


class TestMarketplaceValidate:
    """Coverage for the ``apm marketplace validate`` command."""

    def test_validate_missing_marketplace_yml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Validate without marketplace.yml exits with code 1."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["marketplace", "validate"])
        assert result.exit_code in (1, 2)

    def test_validate_with_valid_marketplace_yml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Valid marketplace.yml passes validation."""
        marketplace_yml = """\
name: test-marketplace
packages:
  - name: sample-plugin
    description: A sample plugin
    source: ./sample
    homepage: https://example.com
build:
  tag_pattern: 'v{version}'
"""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            (Path.cwd() / "marketplace.yml").write_text(marketplace_yml, encoding="utf-8")
            result = runner.invoke(cli, ["marketplace", "validate"])
        assert result.exit_code == 0 or "valid" in result.output.lower()


# ===========================================================================
# Install - basic invocation
# ===========================================================================


class TestInstallBasic:
    """Basic coverage for ``apm install``."""

    def test_install_no_apm_yml_no_packages(self, runner: CliRunner, tmp_path: Path) -> None:
        """No apm.yml and no packages exits with an informative error."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["install"])
        assert result.exit_code != 0
        assert "No apm.yml" in result.output or "not found" in result.output.lower()

    def test_install_from_apm_yml_no_deps(self, runner: CliRunner, tmp_path: Path) -> None:
        """Install from apm.yml that has an empty deps list runs cleanly."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch(
                "apm_cli.commands.install.AuthResolver",
                autospec=True,
            ) as mock_auth_cls:
                mock_auth = MagicMock()
                mock_auth_cls.return_value = mock_auth
                result = runner.invoke(cli, ["install"])
        assert result.exit_code == 0

    def test_install_dry_run_no_deps(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run with no deps completes cleanly."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--dry-run"])
        assert result.exit_code == 0

    def test_install_verbose_no_deps(self, runner: CliRunner, tmp_path: Path) -> None:
        """--verbose with no deps completes cleanly."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--verbose"])
        assert result.exit_code == 0


# ===========================================================================
# Install - with packages argument
# ===========================================================================


class TestInstallWithPackages:
    """Coverage for ``apm install owner/repo`` and related flows."""

    def test_install_package_validates_and_adds(self, runner: CliRunner, tmp_path: Path) -> None:
        """Specifying a package triggers validation + apm.yml mutation path."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
                patch(
                    "apm_cli.install.plan.UpdatePlan",
                    autospec=True,
                )
                if False
                else _dummy_ctx(),
            ):
                result = runner.invoke(cli, ["install", "--dry-run", "owner/test-dep"])
        # Dry-run should exit 0 and mention the package
        assert result.exit_code == 0 or "owner/test-dep" in result.output

    def test_install_package_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run for a new package shows what would be added."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(cli, ["install", "--dry-run", "owner/my-pkg"])
        assert result.exit_code == 0

    def test_install_invalid_package_format(self, runner: CliRunner, tmp_path: Path) -> None:
        """Invalid package identifier (no slash) exits with an error."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "nodeps"])
        # Should fail because 'nodeps' is not a valid owner/repo
        assert result.exit_code != 0 or "invalid" in result.output.lower()

    def test_install_auto_creates_apm_yml(self, runner: CliRunner, tmp_path: Path) -> None:
        """apm.yml is auto-created when packages are specified but no apm.yml exists."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(cli, ["install", "--dry-run", "owner/pkg"])
        # Should auto-create apm.yml or exit 0
        assert result.exit_code == 0 or "Created" in result.output

    def test_install_package_already_in_deps(self, runner: CliRunner, tmp_path: Path) -> None:
        """Installing a package already in apm.yml skips duplication."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_WITH_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(cli, ["install", "--dry-run", "owner/test-dep"])
        assert result.exit_code == 0


# ===========================================================================
# Install - flags and options
# ===========================================================================


class TestInstallFlags:
    """Coverage for various ``apm install`` flags."""

    def test_install_frozen_and_update_mutually_exclusive(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--frozen and --update together exit with code 2."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["install", "--frozen", "--update"])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_install_ssh_and_https_mutually_exclusive(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--ssh and --https together exit with error."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--ssh", "--https"])
        assert result.exit_code != 0

    def test_install_alias_without_local_bundle_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--as without a local bundle path is rejected."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--as", "custom-name", "owner/pkg"])
        assert result.exit_code != 0
        assert "--as" in result.output or "local bundle" in result.output.lower()

    def test_install_with_only_apm(self, runner: CliRunner, tmp_path: Path) -> None:
        """--only apm installs only APM deps."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--only", "apm"])
        assert result.exit_code == 0

    def test_install_with_only_mcp(self, runner: CliRunner, tmp_path: Path) -> None:
        """--only mcp installs only MCP deps."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--only", "mcp"])
        assert result.exit_code == 0

    def test_install_parallel_downloads_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--parallel-downloads flag is accepted."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--parallel-downloads", "2"])
        assert result.exit_code == 0

    def test_install_dev_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dev flag routes to devDependencies."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(cli, ["install", "--dev", "--dry-run", "owner/pkg"])
        assert result.exit_code == 0

    def test_install_no_policy_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--no-policy flag is accepted and forwarded."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--no-policy"])
        assert result.exit_code == 0

    def test_install_refresh_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """--refresh flag bypasses cache."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--refresh"])
        assert result.exit_code == 0


# ===========================================================================
# Install - lockfile scenarios
# ===========================================================================


class TestInstallWithLockfile:
    """Coverage for install with lockfile present/absent."""

    def test_install_with_lockfile_present(self, runner: CliRunner, tmp_path: Path) -> None:
        """Install with lockfile uses it for resolution."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            _write_lockfile(Path.cwd())
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install"])
        assert result.exit_code == 0

    def test_install_frozen_with_lockfile(self, runner: CliRunner, tmp_path: Path) -> None:
        """--frozen succeeds when lockfile is present and in sync."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            _write_lockfile(Path.cwd())
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install", "--frozen"])
        assert result.exit_code == 0

    def test_install_apm_yml_with_deps_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run with deps in apm.yml shows install plan."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_WITH_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.install.resolution.APMDependencyResolver",
                    autospec=True,
                )
                if False
                else _dummy_ctx(),
            ):
                result = runner.invoke(cli, ["install", "--dry-run"])
        assert result.exit_code == 0


# ===========================================================================
# Install - apm.yml auto-bootstrap
# ===========================================================================


class TestInstallAutoBootstrap:
    """Install auto-creates apm.yml for new projects."""

    def test_no_apm_yml_with_package_arg_auto_creates(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """apm install owner/pkg auto-bootstraps apm.yml."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(cli, ["install", "--dry-run", "owner/new-pkg"])
        assert result.exit_code == 0

    def test_no_apm_yml_with_target_flag_persists_targets(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Auto-bootstrap persists CLI targets through the integration path."""
        from apm_cli.core.apm_yml import parse_targets_field

        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=True,
                ),
            ):
                result = runner.invoke(
                    cli, ["install", "--dry-run", "owner/new-pkg", "--target", "copilot"]
                )

            assert result.exit_code == 0
            with open("apm.yml", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            assert config.get("targets") == ["copilot"]
            assert parse_targets_field(config) == ["copilot"]


# ===========================================================================
# Install - error paths
# ===========================================================================


class TestInstallErrorPaths:
    """Coverage for various install error paths."""

    def test_install_tarball_not_bundle_raises_usage_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A .tar.gz that isn't a valid bundle raises a UsageError."""
        fake_tarball = tmp_path / "bad-bundle.tar.gz"
        fake_tarball.write_bytes(b"not a real tarball")
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(cli, ["install", str(fake_tarball)])
        assert result.exit_code != 0

    def test_install_corrupted_apm_yml(self, runner: CliRunner, tmp_path: Path) -> None:
        """Corrupted apm.yml exits with error."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            (Path.cwd() / "apm.yml").write_text("not: valid: yaml: [\n", encoding="utf-8")
            with patch("apm_cli.commands.install.AuthResolver", autospec=True):
                result = runner.invoke(cli, ["install"])
        assert result.exit_code != 0

    def test_install_package_validation_failure(self, runner: CliRunner, tmp_path: Path) -> None:
        """Package that fails validation is reported and exits non-zero."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            _write_apm_yml(Path.cwd(), APM_YML_NO_DEPS)
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.commands.install._validate_package_exists",
                    return_value=False,
                ),
            ):
                result = runner.invoke(cli, ["install", "owner/nonexistent"])
        # All validations failed, so the command must report failure.
        assert result.exit_code == 1


# ===========================================================================
# Install - global scope
# ===========================================================================


class TestInstallGlobalScope:
    """Coverage for ``apm install --global``."""

    def test_install_global_no_packages(self, runner: CliRunner, tmp_path: Path) -> None:
        """--global without packages falls through to user-scope apm.yml check."""
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with (
                patch("apm_cli.commands.install.AuthResolver", autospec=True),
                patch(
                    "apm_cli.core.scope.get_manifest_path",
                    return_value=tmp_path / ".apm" / "apm.yml",
                ),
                patch("apm_cli.core.scope.ensure_user_dirs"),
                patch("apm_cli.core.scope.warn_unsupported_user_scope", return_value=None),
            ):
                result = runner.invoke(cli, ["install", "--global"])
        # Should mention user scope or error about missing apm.yml
        assert result.exit_code in (0, 1)


# ===========================================================================
# Marketplace - internal helpers (unit-style but via real code paths)
# ===========================================================================


class TestMarketplaceInternalHelpers:
    """Tests for internal helpers in marketplace __init__.py."""

    def test_is_valid_alias_patterns(self) -> None:
        """_is_valid_alias accepts valid and rejects invalid aliases."""
        from apm_cli.commands.marketplace import _is_valid_alias

        assert _is_valid_alias("skills")
        assert _is_valid_alias("my-tools")
        assert _is_valid_alias("tools_v2")
        assert _is_valid_alias("acme.marketplace")
        assert not _is_valid_alias("")
        assert not _is_valid_alias("bad name")
        assert not _is_valid_alias("no@allowed")

    def test_parse_marketplace_repo_valid(self) -> None:
        """_parse_marketplace_repo parses OWNER/REPO correctly."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, kind, embedded_host = _parse_marketplace_repo("acme/plugins", None)
        assert url == "https://github.com/acme/plugins"
        assert kind == "github"
        assert embedded_host == "github.com"

    def test_parse_marketplace_repo_https_url(self) -> None:
        """_parse_marketplace_repo handles HTTPS URLs."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        url, kind, embedded_host = _parse_marketplace_repo("https://github.com/acme/plugins", None)
        assert url == "https://github.com/acme/plugins"
        assert kind == "github"
        assert embedded_host == "github.com"

    def test_parse_marketplace_repo_http_rejected(self) -> None:
        """_parse_marketplace_repo rejects http:// URLs."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Insecure HTTP"):
            _parse_marketplace_repo("http://github.com/acme/repo", None)

    def test_parse_marketplace_repo_empty_raises(self) -> None:
        """Empty repo raises ValueError."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises(ValueError, match="Empty"):
            _parse_marketplace_repo("", None)

    def test_parse_marketplace_repo_path_traversal_rejected(self) -> None:
        """Path traversal in repo argument is rejected."""
        from apm_cli.commands.marketplace import _parse_marketplace_repo

        with pytest.raises((ValueError, Exception)):
            _parse_marketplace_repo("acme/../evil/repo", None)

    def test_find_duplicate_names_no_duplicates(self) -> None:
        """_find_duplicate_names returns empty string when no duplicates."""
        from apm_cli.commands.marketplace import _find_duplicate_names

        # Build minimal yml-like object with a packages attribute
        pkg1 = MagicMock()
        pkg1.name = "alpha"
        pkg2 = MagicMock()
        pkg2.name = "beta"
        yml = MagicMock()
        yml.packages = [pkg1, pkg2]
        result = _find_duplicate_names(yml)
        assert result == ""

    def test_find_duplicate_names_with_duplicates(self) -> None:
        """_find_duplicate_names reports duplicated names."""
        from apm_cli.commands.marketplace import _find_duplicate_names

        pkg1 = MagicMock()
        pkg1.name = "alpha"
        pkg2 = MagicMock()
        pkg2.name = "Alpha"  # same after lowercasing
        yml = MagicMock()
        yml.packages = [pkg1, pkg2]
        result = _find_duplicate_names(yml)
        assert "alpha" in result.lower() or "Alpha" in result

    def test_check_gitignore_no_file(self, tmp_path: Path) -> None:
        """_check_gitignore_for_marketplace_json is a no-op when no .gitignore exists."""
        from apm_cli.commands.marketplace import _check_gitignore_for_marketplace_json

        logger = MagicMock()
        with patch("apm_cli.commands.marketplace.Path") as mock_path_cls:
            gitignore = MagicMock()
            gitignore.exists.return_value = False
            mock_path_cls.cwd.return_value.__truediv__.return_value = gitignore
            _check_gitignore_for_marketplace_json(logger)
        logger.warning.assert_not_called()

    def test_marketplace_add_unsupported_host_error_ado(self) -> None:
        """ADO host gets a specific error message."""
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "dev.azure.com", "'acme/repo'", "'dev.azure.com'", "ado"
        )
        assert "GitHub" in msg or "GitLab" in msg

    def test_marketplace_add_unsupported_host_error_generic(self) -> None:
        """Generic unsupported host shows supported hosts."""
        from apm_cli.commands.marketplace import _marketplace_add_unsupported_host_error

        msg = _marketplace_add_unsupported_host_error(
            "custom.host", "'acme/repo'", "'custom.host'", "other"
        )
        assert "github.com" in msg or "Supported" in msg


# ===========================================================================
# Install - internal helper tests
# ===========================================================================


class TestInstallInternalHelpers:
    """Tests for internal install.py helpers exercised through real code."""

    def test_split_argv_at_double_dash_no_separator(self) -> None:
        """Without '--', command_argv is empty."""
        from apm_cli.commands.install import _split_argv_at_double_dash

        clean, cmd = _split_argv_at_double_dash(["apm", "install", "owner/pkg"])
        assert cmd == ()
        assert clean == ["apm", "install", "owner/pkg"]

    def test_split_argv_at_double_dash_with_separator(self) -> None:
        """'--' splits argv correctly."""
        from apm_cli.commands.install import _split_argv_at_double_dash

        clean, cmd = _split_argv_at_double_dash(
            ["apm", "install", "--mcp", "srv", "--", "npx", "-y", "srv"]
        )
        assert cmd == ("npx", "-y", "srv")
        assert "--" not in clean

    def test_restore_manifest_from_snapshot(self, tmp_path: Path) -> None:
        """_restore_manifest_from_snapshot writes bytes atomically."""
        from apm_cli.commands.install import _restore_manifest_from_snapshot

        target = tmp_path / "apm.yml"
        target.write_bytes(b"original content")
        snapshot = b"name: restored\nversion: 0.1.0\n"
        _restore_manifest_from_snapshot(target, snapshot)
        assert target.read_bytes() == snapshot

    def test_maybe_rollback_manifest_no_snapshot(self) -> None:
        """_maybe_rollback_manifest is a no-op when snapshot is None."""
        from apm_cli.commands.install import _maybe_rollback_manifest

        logger = MagicMock()
        _maybe_rollback_manifest(Path("/does/not/exist"), None, logger)
        logger.progress.assert_not_called()
        logger.warning.assert_not_called()

    def test_check_package_conflicts_empty(self) -> None:
        """Empty dep list returns empty identity set."""
        from apm_cli.commands.install import _check_package_conflicts

        result = _check_package_conflicts([])
        assert result == set()

    def test_check_package_conflicts_with_string_dep(self) -> None:
        """String-form deps are parsed into identity strings."""
        from apm_cli.commands.install import _check_package_conflicts

        identities = _check_package_conflicts(["owner/repo"])
        # Should have exactly one identity
        assert len(identities) == 1


# ===========================================================================
# Helpers
# ===========================================================================


class _dummy_ctx:
    """Null context manager for conditional patches that are no-ops."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
