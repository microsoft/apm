"""Unit tests for marketplace-based ref checking in ``apm outdated``."""

from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.commands.outdated import (
    OutdatedRow,
    _check_marketplace_ref,
    _check_one_dep,
)
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _marketplace_dep(
    repo_url="acme-org/skill-auth",
    discovered_via="acme-tools",
    marketplace_plugin_name="skill-auth",
    resolved_ref="v2.1.0",
    resolved_commit="aabbccddee",
):
    """Build a LockedDependency with marketplace provenance."""
    return LockedDependency(
        resolved_ref=resolved_ref,
        resolved_commit=resolved_commit,
        repo_url=repo_url,
        discovered_via=discovered_via,
        marketplace_plugin_name=marketplace_plugin_name,
    )


def _plugin(name="skill-auth", ref="v2.1.0", version="2.1.0"):
    """Build a MarketplacePlugin with a github source."""
    return MarketplacePlugin(
        name=name,
        source={"type": "github", "repo": "acme-org/skill-auth", "ref": ref},
        version=version,
    )


def _manifest(*plugins, name="acme-tools"):
    return MarketplaceManifest(name=name, plugins=tuple(plugins))


def _source(name="acme-tools"):
    return MarketplaceSource(name=name, owner="acme-org", repo="marketplace")


# ---------------------------------------------------------------------------
# _check_marketplace_ref
# ---------------------------------------------------------------------------


class TestCheckMarketplaceRef:
    """Ref-based outdated check against marketplace entry."""

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_up_to_date(self, mock_get_mkt, mock_fetch):
        """Same ref -> up-to-date."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin(ref="v2.1.0"))
        dep = _marketplace_dep(resolved_ref="v2.1.0")

        result = _check_marketplace_ref(dep, verbose=False)
        assert result is not None
        assert result.status == "up-to-date"

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_outdated(self, mock_get_mkt, mock_fetch):
        """Different ref -> outdated."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin(ref="v3.0.0", version="3.0.0"))
        dep = _marketplace_dep(resolved_ref="v2.1.0")

        result = _check_marketplace_ref(dep, verbose=False)
        assert result is not None
        assert result.status == "outdated"
        assert "3.0.0" in result.latest

    def test_no_marketplace_provenance(self):
        """Non-marketplace dep returns None (fall through)."""
        dep = LockedDependency(
            resolved_ref="main",
            resolved_commit="abc123",
            repo_url="owner/repo",
        )
        result = _check_marketplace_ref(dep, verbose=False)
        assert result is None

    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_marketplace_not_found(self, mock_get_mkt):
        """Unknown marketplace returns None (fall through to git check)."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        mock_get_mkt.side_effect = MarketplaceNotFoundError("nope")
        dep = _marketplace_dep()
        result = _check_marketplace_ref(dep, verbose=False)
        assert result is None

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_plugin_not_in_manifest(self, mock_get_mkt, mock_fetch):
        """Plugin removed from marketplace returns None."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin(name="other-plugin"))
        dep = _marketplace_dep()
        result = _check_marketplace_ref(dep, verbose=False)
        assert result is None

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_uses_resolved_commit_when_no_ref(self, mock_get_mkt, mock_fetch):
        """Falls back to resolved_commit when resolved_ref is empty."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin(ref="aabbccddee"))
        dep = _marketplace_dep(resolved_ref="", resolved_commit="aabbccddee")

        result = _check_marketplace_ref(dep, verbose=False)
        assert result is not None
        assert result.status == "up-to-date"


# ---------------------------------------------------------------------------
# _check_one_dep -- marketplace path integration
# ---------------------------------------------------------------------------


class TestCheckOneDepMarketplace:
    """_check_one_dep delegates to _check_marketplace_ref for marketplace deps."""

    @patch("apm_cli.commands.outdated._check_marketplace_ref")
    def test_marketplace_dep_uses_ref_check(self, mock_ref_check):
        """When _check_marketplace_ref returns a result, it is used."""
        mock_ref_check.return_value = OutdatedRow(
            package="skill-auth@acme-tools",
            current="v2.1.0",
            latest="v3.0.0",
            status="outdated",
            source="marketplace: acme-tools",
        )
        dep = _marketplace_dep()
        result = _check_one_dep(dep, downloader=MagicMock(), verbose=False)
        assert result.status == "outdated"
        mock_ref_check.assert_called_once()

    @patch("apm_cli.commands.outdated._check_marketplace_ref")
    def test_fallthrough_to_git_when_ref_check_returns_none(self, mock_ref_check):
        """When _check_marketplace_ref returns None, fall through to git check."""
        mock_ref_check.return_value = None
        dep = _marketplace_dep()
        downloader = MagicMock()
        downloader.get_remote_refs.return_value = [
            RemoteRef(
                ref_type=GitReferenceType.TAG,
                name="v2.1.0",
                commit_sha="aabbccddee",
            ),
        ]
        result = _check_one_dep(dep, downloader=downloader, verbose=False)
        assert result is not None
