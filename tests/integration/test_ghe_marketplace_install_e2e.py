"""Integration test: marketplace install on ``*.ghe.com`` hosts targets enterprise auth.

Closes the regression-trap gap flagged by the review panel for PR #1292
(closes #1285): the unit tests in ``tests/unit/marketplace/`` cover the
resolver layer directly but stop at the canonical string. This test drives
the full pipeline through to :meth:`AuthResolver.resolve_for_dep` so the
auth-routing contract -- enterprise host, never ``github.com`` fallback --
is machine-verified end-to-end, satisfying the secure-by-default and
governed-by-policy invariants the panel called out (#1304).

Stubs at two seams only:

- ``get_marketplace_by_name`` / ``fetch_or_cache``: skip the marketplace
  registry + manifest network I/O. These return ``MarketplaceSource``
  registry-config (trust boundary the auth-expert confirmed clean), not
  manifest content.
- ``AuthResolver._resolve_token``: skip env/gh-cli/credential-helper I/O so
  the test is deterministic and does not depend on the runner having tokens.
  The ``host_info`` field on the returned ``AuthContext`` is still real
  (built by ``classify_host``) -- that is the routing contract under test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import resolve_marketplace_plugin
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.github_host import default_host

_GHE_HOST = "corp.ghe.com"
_OWNER = "myorg"
_REPO = "my-marketplace"


def _make_source(host: str) -> MarketplaceSource:
    return MarketplaceSource(
        name=_REPO,
        owner=_OWNER,
        repo=_REPO,
        host=host,
        branch="main",
    )


def _make_manifest(plugin: MarketplacePlugin) -> MarketplaceManifest:
    return MarketplaceManifest(name=_REPO, plugins=(plugin,), plugin_root="")


def _stub_resolve_token(self, host_info, org):
    """Replacement for ``AuthResolver._resolve_token``.

    Returns ``(None, "none", "basic")`` so ``resolve`` builds an ``AuthContext``
    deterministically without touching ``gh``, env vars, or the credential
    helper. ``host_info`` is the real value from ``classify_host`` -- which is
    the routing decision we are asserting on.
    """
    return None, "none", "basic"


@pytest.mark.integration
class TestGHEMarketplaceInstallAuthRouting:
    """End-to-end: marketplace install on ``*.ghe.com`` routes AuthResolver at the enterprise host."""

    @pytest.fixture(autouse=True)
    def _isolate_github_host_env(self, monkeypatch):
        """#1285 explicitly notes ``GITHUB_HOST=corp.ghe.com`` is NOT a viable workaround.

        Clear it so the bug-fix path (canonical carries host) is what is actually
        tested, not env masking the missing prefix.
        """
        monkeypatch.delenv("GITHUB_HOST", raising=False)

    @pytest.fixture(autouse=True)
    def _stub_token_resolution(self):
        with patch.object(AuthResolver, "_resolve_token", _stub_resolve_token):
            yield

    @pytest.mark.parametrize(
        "label,plugin_source",
        [
            ("relative-source", "./plugins/my-plugin"),
            (
                "dict-bare-repo",
                {"type": "github", "repo": f"{_OWNER}/{_REPO}", "path": "plugins/my-plugin"},
            ),
        ],
    )
    def test_ghe_marketplace_backfills_host_on_bare_canonical(self, label, plugin_source):
        """#1285 regression trap: cases where ``resolve_plugin_source`` emits a bare canonical.

        These are the cases the fix actually mutates -- without the host-prefix backfill
        the canonical lacks ``corp.ghe.com/`` and ``DependencyReference.parse`` falls back
        to ``github.com``. Verified locally: reverting ``_needs_canonical_host_prefix``
        to ``return False`` makes both parametrized cases fail at all three layers
        (canonical, parse host, ``AuthContext.host_info.host``) -- a defense-in-depth
        trap rather than a single boundary check.
        """
        plugin = MarketplacePlugin(name="my-plugin", source=plugin_source)
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=_make_source(_GHE_HOST),
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=_make_manifest(plugin),
            ),
        ):
            result = resolve_marketplace_plugin("my-plugin", _REPO)

        # Layer 1: canonical carries the enterprise host
        expected_canonical = f"{_GHE_HOST}/{_OWNER}/{_REPO}/plugins/my-plugin"
        assert result.canonical == expected_canonical, f"[{label}] canonical mismatch"

        # Layer 2: re-parsing the canonical recovers the GHE host -- this is the
        # boundary the install pipeline crosses at
        # apm_cli.install.package_resolution.resolve_parsed_dependency_reference
        # when marketplace_dep_ref is None (the GitHub-family path).
        dep_ref = DependencyReference.parse(result.canonical)
        assert dep_ref.host == _GHE_HOST
        assert dep_ref.repo_url == f"{_OWNER}/{_REPO}"
        assert dep_ref.virtual_path == "plugins/my-plugin"

        # Layer 3: AuthResolver targets the enterprise host, not github.com fallback
        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == _GHE_HOST, (
            f"[{label}] auth resolved at {ctx.host_info.host!r}, not the GHE host -- "
            "this is the silent github.com fallback that #1285 fixed"
        )
        assert ctx.host_info.kind == "ghe_cloud"

    def test_ghe_marketplace_host_qualified_dict_source_routes_idempotently(self):
        """Idempotency lock (NOT a #1285 regression trap).

        When the manifest dict source carries a host-qualified ``repo`` (e.g.
        ``corp.ghe.com/myorg/my-marketplace``), ``_resolve_github_source`` already
        emits the host on the canonical -- the prefix step is a no-op here. The
        contract this case locks is "the idempotent guard does not double-prefix
        and the install still routes correctly", not the regression trap (the case
        passes regardless of whether the fix is enabled, verified locally).
        """
        plugin = MarketplacePlugin(
            name="my-plugin",
            source={
                "type": "github",
                "repo": f"{_GHE_HOST}/{_OWNER}/{_REPO}",
                "path": "plugins/my-plugin",
            },
        )
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=_make_source(_GHE_HOST),
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=_make_manifest(plugin),
            ),
        ):
            result = resolve_marketplace_plugin("my-plugin", _REPO)

        # Single (not double) host prefix
        assert result.canonical == f"{_GHE_HOST}/{_OWNER}/{_REPO}/plugins/my-plugin"

        dep_ref = DependencyReference.parse(result.canonical)
        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == _GHE_HOST
        assert ctx.host_info.kind == "ghe_cloud"

    def test_github_com_marketplace_keeps_github_default(self):
        """Regression: ``github.com`` marketplace is unchanged (bare canonical, parse default)."""
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=_make_source("github.com"),
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=_make_manifest(plugin),
            ),
        ):
            result = resolve_marketplace_plugin("my-plugin", _REPO)

        assert result.canonical == f"{_OWNER}/{_REPO}/plugins/my-plugin"
        dep_ref = DependencyReference.parse(result.canonical)
        # default_host() applies because the bare canonical carries no host.
        # For github.com marketplaces this is the documented + correct behaviour.
        assert (dep_ref.host or default_host()) == "github.com"

        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == "github.com"
        assert ctx.host_info.kind == "github"

    def test_cross_repo_locks_known_silent_misroute(self):
        """Regression trap for the cross-repo bug class tracked separately in #1305.

        A ``*.ghe.com`` marketplace with a cross-repo dict source bears the same
        symptoms as #1285 -- canonical emerges bare, parse defaults to ``github.com``.
        This is intentionally out of scope of PR #1292; #1305 tracks the fix
        (which belongs in the install-time error handler, not the resolver).
        The test locks the current behaviour so the future #1305 fix has an
        explicit before/after diff to assert against.
        """
        plugin = MarketplacePlugin(
            name="cross-repo",
            source={
                "type": "github",
                "repo": "anotherorg/anothertool",
                "path": "plugins/x",
            },
        )
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=_make_source(_GHE_HOST),
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=_make_manifest(plugin),
            ),
        ):
            result = resolve_marketplace_plugin("cross-repo", _REPO)

        # Pre-existing behaviour: no host prefix for cross-repo (#1305 to fix).
        assert result.canonical == "anotherorg/anothertool/plugins/x"
        dep_ref = DependencyReference.parse(result.canonical)
        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == "github.com", (
            "If this assertion fails, the cross-repo silent mis-route bug from "
            "#1305 has been fixed -- update this test to reflect the new behaviour."
        )
