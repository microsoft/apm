"""Tests for ``apm marketplace check`` subcommand."""

from __future__ import annotations

import textwrap
from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.errors import (
    GitLsRemoteError,
    MarketplaceYmlError,  # noqa: F401
    OfflineMissError,
)
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.yml_schema import (
    MarketplaceOwner,
    MarketplaceYml,
    PackageEntry,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
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
        tags: [testing]
      - name: pkg-beta
        source: acme-org/pkg-beta
        version: "~2.0.0"
        tags: [utility]
""")

_YML_WITH_REF = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: pinned-pkg
        source: acme-org/pinned-pkg
        ref: v1.0.0
""")

_YML_SINGLE = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: solo
        source: acme-org/solo
        version: "^1.0.0"
""")

_REFS_GOOD = [
    RemoteRef(name="refs/tags/v1.0.0", sha=_SHA_A),
    RemoteRef(name="refs/tags/v1.1.0", sha=_SHA_B),
]

_REFS_BETA_GOOD = [
    RemoteRef(name="refs/tags/v2.0.0", sha=_SHA_A),
    RemoteRef(name="refs/tags/v2.0.1", sha=_SHA_B),
]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def yml_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path -- all entries OK
# ---------------------------------------------------------------------------


class TestCheckAllOK:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_all_entries_pass(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_GOOD, _REFS_BETA_GOOD]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 0
        assert "All 2 entries OK" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_shows_package_names(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_GOOD, _REFS_BETA_GOOD]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert "pkg-alpha" in result.output
        assert "pkg-beta" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_success_icon_shown(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_GOOD, _REFS_BETA_GOOD]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert "[+]" in result.output


# ---------------------------------------------------------------------------
# Entry with explicit ref
# ---------------------------------------------------------------------------


class TestCheckExplicitRef:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_ref_found(self, MockResolver, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/v1.0.0", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 0
        assert "pinned-pkg" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_ref_not_found(self, MockResolver, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("COLUMNS", "200")
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        # Return tags that don't include v1.0.0
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/v2.0.0", sha=_SHA_B),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "1 entries have issues" in result.output


# ---------------------------------------------------------------------------
# Failed entries
# ---------------------------------------------------------------------------


class TestCheckFailures:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_one_failure_exits_1(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        # First package OK, second fails
        mock_inst.list_remote_refs.side_effect = [
            _REFS_GOOD,
            GitLsRemoteError(package="pkg-beta", summary="Auth failed", hint="Check token"),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "1 entries have issues" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_all_failures_exits_1(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = GitLsRemoteError(
            package="", summary="Network down", hint="Check connection"
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "2 entries have issues" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_failure_icon_shown(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = GitLsRemoteError(
            package="", summary="Fail", hint=""
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert "[x]" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_no_matching_version(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        # Return tags that don't match the version range
        mock_inst.list_remote_refs.side_effect = [
            [RemoteRef(name="refs/tags/v0.1.0", sha=_SHA_A)],
            _REFS_BETA_GOOD,
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "1 entries have issues" in result.output


# ---------------------------------------------------------------------------
# Missing yml / schema error
# ---------------------------------------------------------------------------


class TestCheckMissingYml:
    def test_missing_yml_exits_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1

    def test_schema_error_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("invalid: thing\n", encoding="utf-8")
        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


class TestCheckOffline:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_offline_label_shown(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = OfflineMissError(
            package="", remote="acme-org/pkg-alpha"
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check", "--offline"])
        assert "Offline mode" in result.output or "offline" in result.output.lower()
        MockResolver.assert_called_once_with(offline=True)

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_offline_cache_miss_fails_entry(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = OfflineMissError(
            package="", remote="acme-org/pkg-alpha"
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check", "--offline"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------


class TestCheckVerbose:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_verbose_no_crash(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_GOOD, _REFS_BETA_GOOD]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check", "--verbose"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Resolver cleanup
# ---------------------------------------------------------------------------


class TestCheckResolverCleanup:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_resolver_close_called(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_GOOD, _REFS_BETA_GOOD]
        mock_inst.close = MagicMock()

        runner.invoke(marketplace, ["check"])
        mock_inst.close.assert_called_once()

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_resolver_close_on_failure(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = GitLsRemoteError(
            package="", summary="Fail", hint=""
        )
        mock_inst.close = MagicMock()

        runner.invoke(marketplace, ["check"])
        mock_inst.close.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestCheckEdgeCases:
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_single_entry_all_ok(self, MockResolver, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_SINGLE, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = _REFS_GOOD
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 0
        assert "All 1 entries OK" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_generic_exception_handled(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = RuntimeError("Unexpected")
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "Unexpected" in result.output


# ---------------------------------------------------------------------------
# Duplicate package name detection
# ---------------------------------------------------------------------------


class TestCheckDuplicateNames:
    """Defence-in-depth duplicate name check in the check command."""

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    @patch("apm_cli.commands.marketplace.check._load_config_or_exit")
    def test_duplicate_names_warned(
        self,
        mock_load,
        MockResolver,
        runner,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")

        # Return a MarketplaceYml with duplicate package names
        mock_load.return_value = (
            tmp_path,
            MarketplaceYml(
                name="test",
                description="Test",
                version="1.0.0",
                owner=MarketplaceOwner(name="Owner"),
                packages=(
                    PackageEntry(
                        name="learning",
                        source="acme/repo",
                        subdir="general",
                        version="^1.0.0",
                    ),
                    PackageEntry(
                        name="learning",
                        source="acme/repo",
                        subdir="special",
                        version="^1.0.0",
                    ),
                ),
            ),
        )

        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/v1.0.0", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert "Duplicate package name 'learning'" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    @patch("apm_cli.commands.marketplace.check._load_config_or_exit")
    def test_no_warning_when_unique(
        self,
        mock_load,
        MockResolver,
        runner,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")

        mock_load.return_value = (
            tmp_path,
            MarketplaceYml(
                name="test",
                description="Test",
                version="1.0.0",
                owner=MarketplaceOwner(name="Owner"),
                packages=(
                    PackageEntry(
                        name="alpha",
                        source="acme/alpha",
                        version="^1.0.0",
                    ),
                    PackageEntry(
                        name="beta",
                        source="acme/beta",
                        version="^1.0.0",
                    ),
                ),
            ),
        )

        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/v1.0.0", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert "Duplicate" not in result.output
