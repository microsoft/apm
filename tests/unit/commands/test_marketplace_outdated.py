"""Tests for ``apm marketplace outdated`` subcommand."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.errors import (
    BuildError,  # noqa: F401
    GitLsRemoteError,
    MarketplaceYmlError,  # noqa: F401
    OfflineMissError,
)
from apm_cli.marketplace.ref_resolver import RemoteRef

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40

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

_REFS_ALPHA = [
    RemoteRef(name="refs/tags/v1.0.0", sha=_SHA_A),
    RemoteRef(name="refs/tags/v1.1.0", sha=_SHA_B),
    RemoteRef(name="refs/tags/v1.2.0", sha=_SHA_C),
    RemoteRef(name="refs/tags/v2.0.0", sha=_SHA_D),
]

_REFS_BETA = [
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
# Happy path
# ---------------------------------------------------------------------------


class TestOutdatedHappyPath:
    """outdated -- basic success."""

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_shows_package_names(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        # Packages are outdated (no marketplace.json) so exit code is 1
        assert result.exit_code == 1
        assert "pkg-alpha" in result.output
        assert "pkg-beta" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_shows_latest_in_range(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        # Packages are outdated (no marketplace.json) so exit code is 1
        assert result.exit_code == 1
        # v1.2.0 is highest in ^1.0.0 range
        assert "v1.2.0" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_shows_latest_overall(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        # v2.0.0 is highest overall for alpha
        assert "v2.0.0" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_exit_code_one_when_outdated(self, MockResolver, runner, yml_cwd):
        """Exit code 1 when packages are outdated (CI-friendly)."""
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 1

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_with_marketplace_json_present(self, MockResolver, runner, yml_cwd):
        """Current versions read from marketplace.json."""
        mkt = {
            "plugins": [
                {"name": "pkg-alpha", "source": {"ref": "v1.0.0", "commit": _SHA_A}},
                {"name": "pkg-beta", "source": {"ref": "v2.0.0", "commit": _SHA_A}},
            ]
        }
        (yml_cwd / "marketplace.json").write_text(json.dumps(mkt), encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        # Packages are outdated (current != latest_in_range) so exit code is 1
        assert result.exit_code == 1
        assert "v1.0.0" in result.output  # current for alpha


# ---------------------------------------------------------------------------
# Ref-pinned entries (skipped)
# ---------------------------------------------------------------------------


class TestOutdatedRefPinned:
    """Entries with explicit ref: are skipped."""

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_ref_pinned_shows_skip_note(self, MockResolver, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_WITH_REF, encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0
        assert "pinned-pkg" in result.output
        assert "ref" in result.output.lower() or "skipped" in result.output.lower()


# ---------------------------------------------------------------------------
# Missing yml / schema error
# ---------------------------------------------------------------------------


class TestOutdatedMissingYml:
    def test_missing_yml_exits_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 1

    def test_schema_error_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("bad: true\n", encoding="utf-8")
        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Offline flag
# ---------------------------------------------------------------------------


class TestOutdatedOffline:
    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_offline_passed_to_resolver(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = OfflineMissError(
            package="", remote="acme-org/pkg-alpha"
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated", "--offline"])
        assert result.exit_code == 0  # outdated is informational
        MockResolver.assert_called_once_with(offline=True)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestOutdatedErrors:
    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_resolver_error_shows_in_table(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = GitLsRemoteError(
            package="", summary="Auth failed", hint="Check token"
        )
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0
        assert "pkg-alpha" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_no_matching_tags(self, MockResolver, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_YML_SINGLE, encoding="utf-8")
        mock_inst = MockResolver.return_value
        # Return tags that don't match the pattern
        mock_inst.list_remote_refs.return_value = [
            RemoteRef(name="refs/tags/release-1.0", sha=_SHA_A),
        ]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0
        assert "solo" in result.output


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------


class TestOutdatedVerbose:
    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_verbose_shows_upgradable_count(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated", "--verbose"])
        # Packages are outdated so exit code is 1
        assert result.exit_code == 1
        assert "upgradable" in result.output.lower()


# ---------------------------------------------------------------------------
# Status symbols
# ---------------------------------------------------------------------------


class TestOutdatedStatusSymbols:
    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_up_to_date_status(self, MockResolver, runner, yml_cwd):
        """When current == latest-in-range, status is [+]."""
        mkt = {
            "plugins": [
                {"name": "pkg-alpha", "source": {"ref": "v1.2.0", "commit": _SHA_C}},
                {"name": "pkg-beta", "source": {"ref": "v2.0.1", "commit": _SHA_B}},
            ]
        }
        (yml_cwd / "marketplace.json").write_text(json.dumps(mkt), encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_major_upgrade_status(self, MockResolver, runner, yml_cwd):
        """When latest-overall differs from latest-in-range, status is [*]."""
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        # Packages are outdated (no marketplace.json) so exit code is 1
        assert result.exit_code == 1
        # pkg-alpha has v2.0.0 outside ^1.0.0 range
        assert "[*]" in result.output


# ---------------------------------------------------------------------------
# Resolver cleanup
# ---------------------------------------------------------------------------


class TestOutdatedResolverCleanup:
    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_resolver_close_called(self, MockResolver, runner, yml_cwd):
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        runner.invoke(marketplace, ["outdated"])
        mock_inst.close.assert_called_once()


# ---------------------------------------------------------------------------
# Summary line (UX5)
# ---------------------------------------------------------------------------


class TestOutdatedSummaryLine:
    """outdated -- summary line and CI exit code."""

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_summary_line_when_outdated(self, MockResolver, runner, yml_cwd):
        """Summary line reports outdated and up-to-date counts."""
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert "package(s) can be updated" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_exit_code_zero_when_up_to_date(self, MockResolver, runner, yml_cwd):
        """Exit code 0 when all packages are up to date."""
        mkt = {
            "plugins": [
                {"name": "pkg-alpha", "source": {"ref": "v1.2.0", "commit": _SHA_C}},
                {"name": "pkg-beta", "source": {"ref": "v2.0.1", "commit": _SHA_B}},
            ]
        }
        (yml_cwd / "marketplace.json").write_text(json.dumps(mkt), encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0
        assert "All packages are up to date" in result.output

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_exit_code_one_when_outdated(self, MockResolver, runner, yml_cwd):
        """Exit code 1 when packages are outdated (CI-friendly)."""
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 1

    @patch("apm_cli.commands.marketplace.outdated.RefResolver")
    def test_summary_counts_up_to_date(self, MockResolver, runner, yml_cwd):
        """Up-to-date count reflects packages at latest in range."""
        mkt = {
            "plugins": [
                {"name": "pkg-alpha", "source": {"ref": "v1.2.0", "commit": _SHA_C}},
                {"name": "pkg-beta", "source": {"ref": "v2.0.1", "commit": _SHA_B}},
            ]
        }
        (yml_cwd / "marketplace.json").write_text(json.dumps(mkt), encoding="utf-8")
        mock_inst = MockResolver.return_value
        mock_inst.list_remote_refs.side_effect = [_REFS_ALPHA, _REFS_BETA]
        mock_inst.close = MagicMock()

        result = runner.invoke(marketplace, ["outdated"])
        assert result.exit_code == 0
        assert "All packages are up to date" in result.output
