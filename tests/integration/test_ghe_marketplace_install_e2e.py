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
        """Regression trap for the cross-repo routing semantics + #1305 sentinel.

        A ``*.ghe.com`` marketplace with a cross-repo dict source bears the
        same superficial symptoms as #1285 -- canonical emerges bare, parse
        defaults to ``github.com``. The #1305 fix deliberately preserves
        that resolver-level routing (a bare cross-repo ``repo`` also
        legitimately means "a github.com open-source dep from this enterprise
        marketplace") and instead attaches a
        :class:`~apm_cli.marketplace.resolver.CrossRepoMisconfigRisk` sentinel
        that the install command consults at the validation-failure boundary
        to emit an actionable host-qualify hint. This test locks both halves:
        the routing preservation (so the legitimate path is not regressed)
        and the sentinel attachment (so the hint emission has the metadata
        it needs).
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

        # Routing preservation: cross-repo canonical stays bare; parse still
        # falls back to ``github.com``. This is intentional -- the legitimate
        # cross-host path validates successfully and never needs to recover
        # the enterprise host. #1305 surfaces the diagnostic at install time
        # when the misconfigured path subsequently fails validation.
        assert result.canonical == "anotherorg/anothertool/plugins/x"
        dep_ref = DependencyReference.parse(result.canonical)
        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == "github.com"

        # #1305: sentinel must attach so the install command's
        # validation-fail branch has the metadata to emit the hint.
        risk = result.cross_repo_misconfig_risk
        assert risk is not None
        assert risk.marketplace_host == _GHE_HOST
        assert risk.bare_repo_field == "anotherorg/anothertool"
        assert risk.suggested_qualified_repo == f"{_GHE_HOST}/anotherorg/anothertool"


@pytest.mark.integration
class TestCrossRepoMisconfigHintIntegration:
    """End-to-end: the #1305 hint surfaces when a cross-repo bare entry on
    a ``*.ghe.com`` marketplace fails validation.

    Unit tests in ``tests/unit/commands/`` mock ``DependencyReference`` and
    ``resolve_marketplace_plugin``; this integration trap walks the real
    ``_resolve_package_references`` + real ``InstallLogger`` and asserts on
    the actual stdout the operator would see. Required by the PR review
    panel (test-coverage-expert: ``outcome: missing`` on a secure-by-default
    surface) and matches the e2e-integration convention PR #1292 established
    with ``test_ghe_marketplace_backfills_host_on_bare_canonical`` above.

    Stubs at one seam only:

    - ``_validate_package_exists``: forces the failure outcome that triggers
      the hint. The real validate path makes outbound HTTP calls; this stub
      keeps the test deterministic. Everything between the resolver sentinel
      and the logger render is the real code path.
    """

    @pytest.fixture(autouse=True)
    def _isolate_github_host_env(self, monkeypatch):
        monkeypatch.delenv("GITHUB_HOST", raising=False)

    def test_cross_repo_hint_emitted_on_validation_failure(self, capsys):
        """The canonical misconfiguration scenario from #1305 surfaces a
        warning-level hint identifying the marketplace host and the exact
        host-qualified ``repo`` value to use as a fix."""
        from apm_cli.commands.install import _resolve_package_references
        from apm_cli.core.command_logger import InstallLogger

        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/shared",
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
            patch(
                "apm_cli.commands.install._validate_package_exists",
                return_value=False,
            ),
        ):
            _resolve_package_references(
                ["shared-tool@my-marketplace"],
                [],
                set(),
                logger=InstallLogger(verbose=False),
            )

        captured = capsys.readouterr()
        emitted = captured.out
        # Hint identifies the plugin@marketplace
        assert "'shared-tool@my-marketplace'" in emitted
        # Marketplace host is named in the "registered on" clause
        # (anchored substring sidesteps CodeQL bare-host pattern recognizers)
        assert f"registered on '{_GHE_HOST}'" in emitted
        # The bare repo from marketplace.json is echoed back
        assert "`repo: platform-team/shared-tool`" in emitted
        # Concrete remediation value the operator can copy-paste
        assert f"'{_GHE_HOST}/platform-team/shared-tool'" in emitted
        # Auth-expert clause acknowledges the legitimate-cross-host
        # alternative so transient failures of real github.com deps are
        # not misdirected into adding an enterprise host prefix.
        assert "intentionally a github.com dependency" in emitted

    def test_legitimate_cross_host_validation_passes_no_hint(self, capsys):
        """The legitimate cross-host case (validation passes) emits no hint.

        This is the entire reason the diagnostic lives at the
        validation-failure boundary instead of resolver time."""
        from apm_cli.commands.install import _resolve_package_references
        from apm_cli.core.command_logger import InstallLogger

        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/shared",
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
            patch(
                "apm_cli.commands.install._validate_package_exists",
                return_value=True,
            ),
        ):
            _resolve_package_references(
                ["shared-tool@my-marketplace"],
                [],
                set(),
                logger=InstallLogger(verbose=False),
            )

        emitted = capsys.readouterr().out
        # No hint substrings on the successful path.
        assert "intentionally a github.com dependency" not in emitted
        assert "If you meant the enterprise host" not in emitted
