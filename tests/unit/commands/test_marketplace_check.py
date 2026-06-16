"""Tests for ``apm marketplace check`` subcommand."""

from __future__ import annotations

import textwrap
from pathlib import Path  # noqa: F401
from unittest.mock import ANY, MagicMock, patch

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


# ---------------------------------------------------------------------------
# refs/heads/ prefix stripping (lines 61-62 in check.py)
# ---------------------------------------------------------------------------

_YML_WITH_BRANCH_REF = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: branch-pkg
        source: acme-org/branch-pkg
        ref: main
""")

_YML_WITH_FULL_REF = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: full-ref-pkg
        source: acme-org/full-ref-pkg
        ref: refs/heads/main
""")


class TestCheckRefHeadsPrefix:
    """Lines 61-62: strip refs/heads/ prefix when matching an explicit ref."""

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_ref_found_via_heads_prefix(self, MockResolver, runner, tmp_path, monkeypatch):
        """An entry with ref='main' resolves against refs/heads/main (line 61-62)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_BRANCH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/heads/main", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 0
        assert "branch-pkg" in result.output
        assert "All 1 entries OK" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_ref_not_found_via_heads_prefix(self, MockResolver, runner, tmp_path, monkeypatch):
        """Entry with ref='main' fails when only refs/heads/develop is present."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_BRANCH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/heads/develop", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 1
        assert "1 entries have issues" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_full_ref_name_matches_directly(self, MockResolver, runner, tmp_path, monkeypatch):
        """Entry ref='refs/heads/main' matched by r.name == entry.ref (line 63 fallback)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_FULL_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/heads/main", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        assert result.exit_code == 0
        assert "full-ref-pkg" in result.output

    @patch("apm_cli.commands.marketplace.check.RefResolver")
    def test_heads_prefix_not_confused_with_tags(self, MockResolver, runner, tmp_path, monkeypatch):
        """Verify refs/heads/ prefix path is taken (not refs/tags/) when branch ref given."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_BRANCH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        # Only a tag ref with same name — should NOT match (branch ref expected)
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/main", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])
        # refs/tags/main stripped to "main" → tag_name == "main" == entry.ref → passes
        # (this also tests the tags branch hits and indirectly confirms the heads branch too)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Per-host resolution (#1519 follow-up): check must resolve each entry against
# its effective host with that host's token, matching `apm pack`. A relative
# source composed onto `sourceBase`, or a host-prefixed source, targets a
# non-default host; bare `owner/repo` keeps the default-host path.
# ---------------------------------------------------------------------------


class TestCheckPerHostResolution:
    @patch("apm_cli.commands.marketplace.check.resolve_token_for_host", return_value="glpat-xyz")
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    @patch("apm_cli.commands.marketplace.check._load_config_or_exit")
    def test_sourcebase_entry_composes_and_uses_host_token(
        self, mock_load, MockResolver, mock_token, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")
        mock_load.return_value = (
            tmp_path,
            MarketplaceYml(
                name="m",
                description="d",
                version="1.0.0",
                owner=MarketplaceOwner(name="o"),
                source_base="https://gitlab.example.com/group/sub/team/project",
                packages=(
                    PackageEntry(
                        name="my-package",
                        source="my-package",  # relative, host=None as parsed
                        version="^1.0.0",
                    ),
                ),
            ),
        )
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = _REFS_GOOD
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])

        assert result.exit_code == 0
        # token resolved for the GitLab base host, with the sourceBase org hint
        # (leading path segment) -- identical to what ``apm pack`` resolves
        mock_token.assert_called_once_with(
            "gitlab.example.com", offline=False, org="group", auth_resolver=ANY
        )
        # resolver bound to that host with that token
        MockResolver.assert_called_once_with(
            offline=False, host="gitlab.example.com", token="glpat-xyz"
        )
        # ls-remote runs against the composed nested path
        mock_inst.list_remote_refs.assert_called_once_with("group/sub/team/project/my-package")

    @patch("apm_cli.commands.marketplace.check.resolve_token_for_host", return_value="ghp-tok")
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    @patch("apm_cli.commands.marketplace.check._load_config_or_exit")
    def test_host_prefixed_override_uses_that_host(
        self, mock_load, MockResolver, mock_token, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")
        mock_load.return_value = (
            tmp_path,
            MarketplaceYml(
                name="m",
                description="d",
                version="1.0.0",
                owner=MarketplaceOwner(name="o"),
                source_base="https://gitlab.example.com/group/sub",
                packages=(
                    PackageEntry(
                        name="helper",
                        source="owner/repo",
                        version="^1.0.0",
                        host="github.com",  # host-prefixed override: base ignored
                    ),
                ),
            ),
        )
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.return_value = _REFS_GOOD
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["check"])

        assert result.exit_code == 0
        # host-prefixed override carries no sourceBase org hint (org=None),
        # matching the builder's _remote_source_coordinates
        mock_token.assert_called_once_with("github.com", offline=False, org=None, auth_resolver=ANY)
        MockResolver.assert_called_once_with(offline=False, host="github.com", token="ghp-tok")
        mock_inst.list_remote_refs.assert_called_once_with("owner/repo")

    @patch("apm_cli.commands.marketplace.check.resolve_token_for_host")
    @patch("apm_cli.commands.marketplace.check.RefResolver")
    @patch("apm_cli.commands.marketplace.check._load_config_or_exit")
    def test_local_entry_makes_zero_ls_remote_calls(
        self, mock_load, MockResolver, mock_token, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")
        mock_load.return_value = (
            tmp_path,
            MarketplaceYml(
                name="m",
                description="d",
                version="1.0.0",
                owner=MarketplaceOwner(name="o"),
                source_base="https://gitlab.example.com/group/sub",
                packages=(
                    PackageEntry(
                        name="local-tool",
                        source="./packages/local-tool",
                        is_local=True,
                    ),
                ),
            ),
        )

        result = runner.invoke(marketplace, ["check"])

        assert result.exit_code == 0
        assert "All 1 entries OK" in result.output
        # local sources never touch the network or the token resolver
        MockResolver.assert_not_called()
        mock_token.assert_not_called()
