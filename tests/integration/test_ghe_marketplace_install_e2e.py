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
        """Regression trap for the cross-repo routing semantics + #1326 sentinel.

        A ``*.ghe.com`` marketplace with a bare cross-repo dict source bears
        the same superficial symptoms as #1285 -- canonical emerges bare,
        parse defaults to ``github.com``. Resolver-level routing is
        deliberately preserved (PR #1292 scoped its host backfill to
        in-marketplace sources only) and the bare-cross-repo case is flagged
        via :class:`~apm_cli.marketplace.resolver.CrossRepoMisconfigRisk`
        sentinel. The install command consumes the sentinel at the
        pre-validation gate (#1326) to fail-closed before any HTTP probe
        reaches the potentially-attacker-controlled ``github.com`` URL.
        This test locks the resolver-layer contract: the routing stays
        unchanged and the sentinel attaches with the metadata the install
        gate needs to render its refusal message.
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
        # falls back to ``github.com``. Resolver layer is intentionally
        # unchanged; the dependency-confusion fix lives at the install
        # command's pre-validation gate (#1326) which consumes the sentinel
        # attached below.
        assert result.canonical == "anotherorg/anothertool/plugins/x"
        dep_ref = DependencyReference.parse(result.canonical)
        auth = AuthResolver()
        ctx = auth.resolve_for_dep(dep_ref)
        assert ctx.host_info.host == "github.com"

        # #1326: sentinel must attach so the install command's pre-validation
        # gate has the metadata to render the refusal message.
        risk = result.cross_repo_misconfig_risk
        assert risk is not None
        assert risk.marketplace_host == _GHE_HOST
        assert risk.bare_repo_field == "anotherorg/anothertool"
        assert risk.suggested_qualified_repo == f"{_GHE_HOST}/anotherorg/anothertool"


@pytest.mark.integration
class TestCrossRepoFailClosedIntegration:
    """End-to-end: #1326 fail-closed gate blocks bare cross-repo install on
    ``*.ghe.com`` marketplaces before any outbound validation HTTP runs.

    Threat model: an enterprise operator's ``corp.ghe.com`` marketplace
    declares ``repo: platform-team/shared-tool`` (bare ``owner/repo``) intending
    the enterprise repo. ``DependencyReference.parse`` defaults missing hosts
    to ``github.com``. An attacker who pre-registers ``platform-team/shared-tool``
    on public ``github.com`` would have the validator succeed against the
    attacker repo and install attacker content under the enterprise's APM
    context. The #1305 fix only surfaced a hint on validation FAILURE; this
    gate is the missing half that closes the success-path attack.

    Gate semantics:

    - Resolver attaches ``CrossRepoMisconfigRisk`` for the ambiguous form
      (sentinel logic at ``_compute_cross_repo_misconfig_risk`` in
      ``marketplace/resolver.py`` is unchanged).
    - Install consumes the sentinel immediately after resolver returns --
      before ``_validate_package_exists`` is called. No HTTP probe to the
      potentially-attacker-controlled URL ever runs.
    - Escape hatch: marketplace.json author host-qualifies ``repo:``
      (``corp.ghe.com/owner/repo`` OR ``github.com/owner/repo``). That
      prevents the sentinel from attaching at resolver layer and install
      proceeds. No new CLI flag, env var, or schema field is introduced.
    """

    @pytest.fixture(autouse=True)
    def _isolate_github_host_env(self, monkeypatch):
        monkeypatch.delenv("GITHUB_HOST", raising=False)

    def test_cross_repo_bare_blocks_install_before_validation(self):
        """The attack PoC: bare cross-repo on enterprise marketplace.

        ``_validate_package_exists`` is wrapped in a Mock returning True --
        simulating the attacker having pre-staged ``platform-team/shared-tool``
        on github.com. If the gate were absent the install would silently
        succeed; the test asserts:

        1. The mock is **never called** (gate fires pre-validation, no HTTP
           leak to attacker-controlled host).
        2. Independent ``requests.get`` / ``requests.head`` probes are also
           never issued (defense-in-depth: catches a future refactor that
           moves validation away from ``_validate_package_exists``).
        3. The package lands in ``invalid_outcomes`` (install rejected).
        4. The reason string names both escape-hatch forms so the operator
           can choose qualify-to-enterprise vs qualify-to-github.com.
        """
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
            ) as mock_validate,
            patch("apm_cli.install.validation.requests.get") as mock_http_get,
            patch(
                "apm_cli.install.validation.requests.head",
                create=True,
            ) as mock_http_head,
        ):
            result = _resolve_package_references(
                ["shared-tool@my-marketplace"],
                [],
                set(),
                logger=InstallLogger(verbose=False),
            )

        valid_outcomes, invalid_outcomes = result[0], result[1]

        # (1) Gate fires pre-validation: validate function never called.
        assert mock_validate.call_count == 0, (
            "fail-closed gate must reject before _validate_package_exists so "
            "no probe reaches the potentially-attacker-controlled github.com URL"
        )
        # (2) HTTP-layer defense-in-depth: even if a future refactor moves
        # validation elsewhere, no outbound probe should fire on the gated
        # path. Patching at ``apm_cli.install.validation.requests.{get,head}``
        # catches any caller that still reaches the validator module.
        assert mock_http_get.call_count == 0
        assert mock_http_head.call_count == 0
        # (2) Install rejected.
        assert valid_outcomes == []
        assert len(invalid_outcomes) == 1
        rejected_package, reason = invalid_outcomes[0]
        assert rejected_package == "shared-tool@my-marketplace"
        # (3) Reason names both escape hatches. All host substrings are
        # anchored with surrounding quote/backtick characters so CodeQL's
        # ``py/incomplete-url-substring-sanitization`` pattern recognizer
        # does not flag bare-host membership checks (see tests/**/CLAUDE.md).
        assert f"'{_GHE_HOST}'" in reason
        # Bare repo is echoed back inside the backtick-delimited `repo:` form.
        assert "`repo: platform-team/shared-tool`" in reason
        # Concrete qualification value for the enterprise path, anchored.
        assert f"'{_GHE_HOST}/platform-team/shared-tool'" in reason
        # Concrete qualification value for the cross-host path, anchored.
        assert "'github.com/platform-team/shared-tool'" in reason
        # Issue reference makes the gate's provenance grep-able.
        assert "#1326" in reason

    def test_cross_repo_qualified_to_enterprise_proceeds(self):
        """Escape hatch (same-host intent): ``repo: corp.ghe.com/owner/repo``.

        Host-qualifying to the enterprise host means the resolver does not
        attach the sentinel (``_needs_canonical_host_prefix`` returns False
        when canonical already starts with the host). Install proceeds
        through ``_validate_package_exists`` as normal.
        """
        from apm_cli.commands.install import _resolve_package_references
        from apm_cli.core.command_logger import InstallLogger

        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": f"{_GHE_HOST}/platform-team/shared-tool",
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
            ) as mock_validate,
        ):
            result = _resolve_package_references(
                ["shared-tool@my-marketplace"],
                [],
                set(),
                logger=InstallLogger(verbose=False),
            )

        valid_outcomes, invalid_outcomes = result[0], result[1]
        # Gate did NOT fire (qualified form = no sentinel = no refusal).
        assert mock_validate.call_count == 1
        assert len(valid_outcomes) == 1
        assert invalid_outcomes == []

    def test_cross_repo_qualified_to_github_com_proceeds(self):
        """Escape hatch (declared cross-host intent): ``repo: github.com/owner/repo``.

        Host-qualifying to ``github.com`` makes the cross-host intent
        explicit. Resolver does not attach the sentinel; install proceeds.
        This is how operators with legitimate github.com open-source
        dependencies on enterprise marketplaces declare intent without
        falling into the bare-shorthand ambiguity.
        """
        from apm_cli.commands.install import _resolve_package_references
        from apm_cli.core.command_logger import InstallLogger

        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": "github.com/platform-team/shared-tool",
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
            ) as mock_validate,
        ):
            result = _resolve_package_references(
                ["shared-tool@my-marketplace"],
                [],
                set(),
                logger=InstallLogger(verbose=False),
            )

        valid_outcomes, invalid_outcomes = result[0], result[1]
        assert mock_validate.call_count == 1
        assert len(valid_outcomes) == 1
        assert invalid_outcomes == []
