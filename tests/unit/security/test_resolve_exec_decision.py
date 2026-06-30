"""Tests for the unified executable-trust resolver (issue #1873).

``resolve_exec_decision`` implements the deny-wins, first-match-wins
precedence ladder shared by the install gate and the policy audit. The
8 rows of the precedence table in #1873 are each pinned here.
"""

from __future__ import annotations

from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    LAYER_DEFAULT_DENY,
    LAYER_ENFORCE_DEGRADED,
    LAYER_GATE_DISABLED,
    LAYER_ORG_DENY,
    LAYER_ORG_DENY_ALL,
    LAYER_ORG_RECOMMEND,
    LAYER_PROJECT_ALLOW,
    LAYER_PROJECT_DENY,
    LAYER_USER_ALLOW,
    LAYER_USER_DENY,
    TRUST_DENIED,
    TRUST_DEPLOYED,
    TRUST_GATED,
    ExecTrustContext,
    exec_status_for_declaration,
    materialize_exec_map,
    resolve_exec_decision,
)

PKG = "owner/repo#v1.0"
NAME = "owner/repo"


def _ctx(**kw) -> ExecTrustContext:
    base = dict(
        gate_enabled=True,
        org_deny_all=False,
        org_deny=frozenset(),
        org_require=frozenset(),
        org_recommend=frozenset(),
        org_enforce=frozenset(),
        org_bin_deny_all=False,
        org_bin_deny=frozenset(),
        project_allow={},
        project_deny={},
        user_allow={},
        user_deny={},
    )
    base.update(kw)
    return ExecTrustContext(**base)


class TestGateDisabled:
    def test_gate_disabled_allows_everything(self):
        d = resolve_exec_decision(_ctx(gate_enabled=False), PKG, EXEC_TYPE_HOOKS)
        assert d.allowed is True
        assert d.deciding_layer == LAYER_GATE_DISABLED
        assert d.trust_state == TRUST_DEPLOYED


class TestRow1OrgDeny:
    def test_org_deny_all_denies_absolutely(self):
        d = resolve_exec_decision(_ctx(org_deny_all=True), PKG, EXEC_TYPE_HOOKS)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY_ALL
        assert d.trust_state == TRUST_DENIED

    def test_org_deny_list_denies(self):
        d = resolve_exec_decision(_ctx(org_deny=frozenset({NAME})), PKG, EXEC_TYPE_MCP)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY

    def test_org_deny_beats_project_allow(self):
        d = resolve_exec_decision(
            _ctx(org_deny=frozenset({NAME}), project_allow={PKG: {EXEC_TYPE_HOOKS: True}}),
            PKG,
            EXEC_TYPE_HOOKS,
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY

    def test_legacy_bin_deploy_denies_bin_only(self):
        ctx = _ctx(org_bin_deny=frozenset({NAME}))
        assert resolve_exec_decision(ctx, PKG, EXEC_TYPE_BIN).allowed is False
        # bin_deploy is bin-scoped: hooks are unaffected by the legacy alias.
        d_hooks = resolve_exec_decision(ctx, PKG, EXEC_TYPE_HOOKS)
        assert d_hooks.deciding_layer != LAYER_ORG_DENY_ALL

    def test_legacy_bin_deploy_denies_normalized_github_url(self):
        ctx = _ctx(org_bin_deny=frozenset({"https://github.com/OWNER/REPO.git"}))
        d = resolve_exec_decision(ctx, PKG, EXEC_TYPE_BIN)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY


class TestRow2UserDeny:
    def test_user_deny_denies(self):
        d = resolve_exec_decision(
            _ctx(user_deny={PKG: {EXEC_TYPE_HOOKS: True}}), PKG, EXEC_TYPE_HOOKS
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_USER_DENY

    def test_user_deny_beats_org_recommend(self):
        d = resolve_exec_decision(
            _ctx(org_recommend=frozenset({NAME}), user_deny={PKG: {EXEC_TYPE_HOOKS: True}}),
            PKG,
            EXEC_TYPE_HOOKS,
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_USER_DENY


class TestProjectDeny:
    def test_project_deny_denies(self):
        d = resolve_exec_decision(
            _ctx(project_deny={PKG: {EXEC_TYPE_BIN: True}}), PKG, EXEC_TYPE_BIN
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_PROJECT_DENY


class TestRows34EnforceDegrades:
    def test_enforce_does_not_force_execute_in_v1(self):
        # v1: enforce must NEVER force-allow ahead of a user opinion; it
        # degrades to recommend (allow, user-overridable). With no user
        # opinion it allows but is attributed to the degraded layer.
        d = resolve_exec_decision(_ctx(org_enforce=frozenset({NAME})), PKG, EXEC_TYPE_HOOKS)
        assert d.allowed is True
        assert d.deciding_layer == LAYER_ENFORCE_DEGRADED

    def test_enforce_is_overridable_by_user_deny(self):
        d = resolve_exec_decision(
            _ctx(org_enforce=frozenset({NAME}), user_deny={PKG: {EXEC_TYPE_HOOKS: True}}),
            PKG,
            EXEC_TYPE_HOOKS,
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_USER_DENY


class TestRow5ProjectAllow:
    def test_project_allow_allows(self):
        d = resolve_exec_decision(
            _ctx(project_allow={PKG: {EXEC_TYPE_HOOKS: True}}), PKG, EXEC_TYPE_HOOKS
        )
        assert d.allowed is True
        assert d.deciding_layer == LAYER_PROJECT_ALLOW
        assert d.trust_state == TRUST_DEPLOYED

    def test_project_allow_is_per_exec_type(self):
        # Allowing hooks does not allow bin.
        d = resolve_exec_decision(
            _ctx(project_allow={PKG: {EXEC_TYPE_HOOKS: True}}), PKG, EXEC_TYPE_BIN
        )
        assert d.allowed is False
        assert d.deciding_layer == LAYER_DEFAULT_DENY

    def test_project_allow_matches_versionless_name(self):
        d = resolve_exec_decision(
            _ctx(project_allow={NAME: {EXEC_TYPE_HOOKS: True}}), PKG, EXEC_TYPE_HOOKS
        )
        assert d.allowed is True


class TestRow6UserAllow:
    def test_user_allow_allows(self):
        d = resolve_exec_decision(
            _ctx(user_allow={PKG: {EXEC_TYPE_HOOKS: True}}), PKG, EXEC_TYPE_HOOKS
        )
        assert d.allowed is True
        assert d.deciding_layer == LAYER_USER_ALLOW


class TestRow7OrgRecommend:
    def test_org_recommend_allows_overridable(self):
        d = resolve_exec_decision(_ctx(org_recommend=frozenset({NAME})), PKG, EXEC_TYPE_HOOKS)
        assert d.allowed is True
        assert d.deciding_layer == LAYER_ORG_RECOMMEND
        assert d.trust_state == TRUST_DEPLOYED


class TestRow8DefaultDeny:
    def test_no_opinion_default_denies_but_approvable(self):
        d = resolve_exec_decision(_ctx(), PKG, EXEC_TYPE_HOOKS)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_DEFAULT_DENY
        # default-deny is approvable (gated), not hard denied.
        assert d.trust_state == TRUST_GATED


class TestShadowedLayers:
    def test_user_deny_records_org_recommend_as_shadowed(self):
        d = resolve_exec_decision(
            _ctx(org_recommend=frozenset({NAME}), user_deny={PKG: {EXEC_TYPE_HOOKS: True}}),
            PKG,
            EXEC_TYPE_HOOKS,
        )
        assert LAYER_ORG_RECOMMEND in d.shadowed_layers


# -------------------------------------------------------------------
# materialize_exec_map: deny-wins effective allow-map (issue #1873)
# -------------------------------------------------------------------


class TestMaterializeExecMap:
    def test_gate_disabled_returns_none(self):
        assert materialize_exec_map(_ctx(gate_enabled=False)) is None

    def test_project_allow_emits_key_and_versionblind_alias(self):
        m = materialize_exec_map(_ctx(project_allow={PKG: {EXEC_TYPE_HOOKS: True}}))
        assert m is not None
        # exact key is present and allowed for hooks only
        assert m[PKG] == {EXEC_TYPE_HOOKS: True}
        # version-blind name alias is emitted so any installed version matches
        assert m[NAME] == {EXEC_TYPE_HOOKS: True}

    def test_org_deny_beats_project_allow_excluded_from_map(self):
        # deny-wins: an org-denied package never lands in the allow-map.
        m = materialize_exec_map(
            _ctx(
                project_allow={PKG: {EXEC_TYPE_HOOKS: True}},
                org_deny=frozenset({NAME}),
            )
        )
        # gate is enabled (project block present) but the denied pair is absent.
        assert m is not None
        assert EXEC_TYPE_HOOKS not in m.get(PKG, {})
        assert EXEC_TYPE_HOOKS not in m.get(NAME, {})

    def test_default_deny_yields_empty_map(self):
        # gate enabled by a project deny entry, but nothing is allowed.
        m = materialize_exec_map(_ctx(project_deny={PKG: {EXEC_TYPE_HOOKS: True}}))
        assert m is not None
        assert m == {}


# -------------------------------------------------------------------
# exec_status_for_declaration: lockfile worst-case folding (Gap B)
# -------------------------------------------------------------------


class TestExecStatusForDeclaration:
    def test_no_exec_types_returns_none(self):
        assert exec_status_for_declaration(_ctx(), [PKG], ()) is None

    def test_gate_disabled_returns_none(self):
        assert (
            exec_status_for_declaration(_ctx(gate_enabled=False), [PKG], (EXEC_TYPE_HOOKS,)) is None
        )

    def test_all_allowed_is_deployed(self):
        ctx = _ctx(project_allow={PKG: {EXEC_TYPE_HOOKS: True, EXEC_TYPE_BIN: True}})
        status = exec_status_for_declaration(ctx, [PKG], (EXEC_TYPE_HOOKS, EXEC_TYPE_BIN))
        assert status == TRUST_DEPLOYED

    def test_any_denied_folds_to_denied(self):
        ctx = _ctx(
            project_allow={PKG: {EXEC_TYPE_HOOKS: True}},
            org_deny=frozenset({NAME}),
        )
        # hooks denied by org ceiling -> worst-case denied even if others allow.
        status = exec_status_for_declaration(ctx, [PKG], (EXEC_TYPE_HOOKS, EXEC_TYPE_BIN))
        assert status == TRUST_DENIED

    def test_unapproved_folds_to_gated(self):
        # gate enabled via project deny entry on a different key; the declared
        # package has no opinion -> default-deny -> gated_pending_approval.
        ctx = _ctx(project_deny={"other/pkg": {EXEC_TYPE_HOOKS: True}})
        status = exec_status_for_declaration(ctx, [PKG], (EXEC_TYPE_HOOKS,))
        assert status == TRUST_GATED


class TestOrgDenyGlob:
    """M1: deny is the ceiling and supports fnmatch globs (deny side ONLY)."""

    def test_glob_denies_matching_package(self):
        ctx = _ctx(org_deny=frozenset({"evil/*"}))
        d = resolve_exec_decision(ctx, "evil/backdoor#v1", EXEC_TYPE_HOOKS)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY
        assert d.trust_state == TRUST_DENIED

    def test_glob_denies_second_matching_package(self):
        ctx = _ctx(org_deny=frozenset({"evil/*"}))
        d = resolve_exec_decision(ctx, "evil/x#v2", EXEC_TYPE_MCP)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY

    def test_glob_does_not_deny_lookalike(self):
        # ``good/evil-lookalike`` must NOT match ``evil/*`` -- the glob is
        # anchored to the segment, not a substring.
        ctx = _ctx(org_deny=frozenset({"evil/*"}))
        d = resolve_exec_decision(ctx, "good/evil-lookalike#v1", EXEC_TYPE_HOOKS)
        assert d.allowed is False  # default-deny, but NOT via the org ceiling
        assert d.deciding_layer == LAYER_DEFAULT_DENY

    def test_deny_glob_is_ceiling_user_cannot_widen(self):
        # A user allow cannot widen past an org deny glob -- deny always wins.
        ctx = _ctx(
            org_deny=frozenset({"evil/*"}),
            user_allow={"evil/backdoor#v1": {EXEC_TYPE_HOOKS: True}},
        )
        d = resolve_exec_decision(ctx, "evil/backdoor#v1", EXEC_TYPE_HOOKS)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY

    def test_exact_deny_still_works(self):
        ctx = _ctx(org_deny=frozenset({"evil/backdoor"}))
        d = resolve_exec_decision(ctx, "evil/backdoor#v1", EXEC_TYPE_HOOKS)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY

    def test_bin_deny_glob(self):
        ctx = _ctx(org_bin_deny=frozenset({"evil/*"}))
        d = resolve_exec_decision(ctx, "evil/tool#v1", EXEC_TYPE_BIN)
        assert d.allowed is False
        assert d.deciding_layer == LAYER_ORG_DENY
