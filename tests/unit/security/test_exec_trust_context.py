"""executable-trust-context owner: one resolver of executable trust per command (#4121)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.policy import outcome_routing
from apm_cli.security import executables, trust
from apm_cli.security.executables import (
    AUTHORIZED_FIRST_PARTY,
    AUTHORIZED_GATE_DISABLED,
    AUTHORIZED_REPLAY,
    LAYER_DEFAULT_DENY,
    LAYER_ENFORCE_DEGRADED,
    LAYER_ORG_DENY,
    LAYER_ORG_RECOMMEND,
    LAYER_PROJECT_ALLOW,
    LAYER_PROJECT_DENY,
    LAYER_USER_ALLOW,
    LAYER_USER_DENY,
    TRUST_DENIED,
    TRUST_DEPLOYED,
    TRUST_GATED,
    EffectiveExecMap,
    ExecDecision,
    ExecTrustContext,
    authorized_exec_map,
    compose_trust_bin,
    effective_exec_map_for_project,
    exec_trust_context_for_project,
    package_type_approved,
    resolve_exec_decision,
    resolve_project_exec_allow_map,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-executable-trust-context")]


# -- bin posture -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approved", "trust_bin", "non_interactive", "outcome"),
    [
        (False, True, False, (True, "not_approved")),
        (False, None, True, (True, "not_approved")),
        (True, False, False, (True, "not_trusted")),
        (True, False, True, (True, "not_trusted")),
        (True, None, True, (True, "not_trusted")),
        (True, None, False, (False, None)),
        (True, True, True, (False, None)),
        (True, True, False, (False, None)),
    ],
)
def test_bin_runs_only_when_approved_and_trusted(
    approved: bool, trust_bin: bool | None, non_interactive: bool, outcome: tuple
) -> None:
    assert compose_trust_bin(approved, trust_bin, non_interactive=non_interactive) == outcome


# -- blanket authorization ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", [AUTHORIZED_GATE_DISABLED, AUTHORIZED_REPLAY, AUTHORIZED_FIRST_PARTY]
)
def test_a_known_reason_authorizes_every_executable(reason: str) -> None:
    allow_map = authorized_exec_map(reason)
    assert isinstance(allow_map, EffectiveExecMap)
    assert allow_map.authorized_by == reason
    assert dict(allow_map) == {}
    assert package_type_approved(allow_map, ["acme/x"], "hooks") is True


def test_an_unknown_reason_is_refused() -> None:
    with pytest.raises(ValueError) as refused:
        authorized_exec_map("because")
    assert str(refused.value) == "Unknown executable authorization: 'because'"


# -- package approvals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allow_map", "keys", "approved"),
    [
        (None, ["acme/x"], False),
        ([("acme/x", {"hooks": True})], ["acme/x"], False),
        ({"acme/x": {"hooks": True}}, ["acme/y", "acme/x"], True),
        ({"acme/x": {"hooks": True}}, ["acme/y"], False),
        ({"acme/x": {"hooks": False}}, ["acme/x"], False),
        ({"acme/x": {"mcp": True}}, ["acme/x"], False),
        ({"acme/x": {"hooks": True}}, [], False),
        (EffectiveExecMap({"acme/x": {"hooks": True}}), ["acme/x"], True),
        (EffectiveExecMap(), ["acme/x"], False),
    ],
)
def test_package_approval_reads_only_literal_grants(
    allow_map: object, keys: list[str], approved: bool
) -> None:
    assert package_type_approved(allow_map, keys, "hooks") is approved


# -- the project trust context ---------------------------------------------------------------


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def build(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(built=len(calls))

    monkeypatch.setattr(executables, "build_exec_trust_context", build)
    monkeypatch.setattr(executables, "_readable_lockfile", lambda root: f"<lock in {root.name}>")
    monkeypatch.setattr(executables, "_ALIAS_DEPRECATION_WARNED", False)
    return calls


def _manifest(tmp_path: Path, body: str) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    path = project / "apm.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_manifest_grants_feed_the_context(built: list[dict], tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "name: p\nexecutables:\n  allow:\n    acme/x: {hooks: true}\n")
    policy = object()
    ctx = exec_trust_context_for_project(
        manifest,
        policy=policy,
        fallback_allow_executables={"ignored/fallback": {"hooks": True}},
        migrate_user_legacy=False,
        include_user=False,
    )
    assert ctx.built == 1
    assert built == [
        {
            "policy": policy,
            "project_data": {"name": "p", "executables": {"allow": {"acme/x": {"hooks": True}}}},
            "migrate_user_legacy": False,
            "include_user": False,
            "lockfile": "<lock in project>",
        }
    ]


def test_without_a_manifest_the_fallback_grants_apply(built: list[dict], tmp_path: Path) -> None:
    grants = {"acme/x": {"mcp": True}}
    exec_trust_context_for_project(
        tmp_path / "missing" / "apm.yml", policy=None, fallback_allow_executables=grants
    )
    assert built[0]["project_data"] == {"allowExecutables": grants}
    assert (built[0]["migrate_user_legacy"], built[0]["include_user"]) == (True, True)
    assert built[0]["lockfile"] == "<lock in missing>"


@pytest.mark.parametrize("fallback", [None, [("acme/x", {})]])
def test_without_a_manifest_or_usable_fallback_there_is_no_project_data(
    built: list[dict], tmp_path: Path, fallback: object
) -> None:
    exec_trust_context_for_project(
        tmp_path / "apm.yml",
        policy=None,
        fallback_allow_executables=fallback,  # type: ignore[arg-type]
    )
    assert built[0]["project_data"] is None


def test_a_non_mapping_manifest_falls_back(built: list[dict], tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "- just\n- a list\n")
    exec_trust_context_for_project(manifest, policy=None, fallback_allow_executables={"a/b": {}})
    assert built[0]["project_data"] == {"allowExecutables": {"a/b": {}}}


def test_the_deprecated_alias_warns_once_through_the_logger(
    built: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "warn_content_blind_grants_once", Mock())
    manifest = _manifest(tmp_path, "name: p\nallowExecutables:\n  acme/x: {hooks: true}\n")
    logger = Mock()
    exec_trust_context_for_project(manifest, policy=None, logger=logger)
    exec_trust_context_for_project(manifest, policy=None, logger=logger)
    assert logger.warning.call_count == 1
    assert "'allowExecutables' in apm.yml is deprecated" in logger.warning.call_args.args[0]


def test_an_empty_alias_does_not_warn(
    built: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "warn_content_blind_grants_once", Mock())
    manifest = _manifest(tmp_path, "name: p\nallowExecutables:\n")
    logger = Mock()
    exec_trust_context_for_project(manifest, policy=None, logger=logger)
    logger.warning.assert_not_called()


def test_content_blind_grants_are_reported_only_with_a_logger(
    built: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warn = Mock()
    monkeypatch.setattr(trust, "warn_content_blind_grants_once", warn)
    manifest = _manifest(tmp_path, "name: p\n")
    exec_trust_context_for_project(manifest, policy=None)
    warn.assert_not_called()
    logger = object()
    ctx = exec_trust_context_for_project(manifest, policy=None, logger=logger)
    warn.assert_called_once_with(ctx, logger)


# -- the per-command allow map -------------------------------------------------------------------


@pytest.mark.parametrize("given", [None, EffectiveExecMap({"acme/x": {"hooks": True}})])
def test_a_map_the_caller_resolved_is_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: object
) -> None:
    monkeypatch.setattr(outcome_routing, "resolve_trust_policy", Mock(side_effect=AssertionError))
    assert (
        resolve_project_exec_allow_map(
            tmp_path, object(), user_scope=False, executable_context=(given, True)
        )
        is given
    )


@pytest.mark.parametrize("user_scope", [False, True])
def test_an_unresolved_map_resolves_policy_and_grants_from_the_scope_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_scope: bool
) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    trust_policy = Mock(return_value="<policy>")
    effective = Mock(return_value="<map>")
    monkeypatch.setattr(outcome_routing, "resolve_trust_policy", trust_policy)
    monkeypatch.setattr(executables, "effective_exec_map_for_project", effective)
    logger = object()
    package = SimpleNamespace(allow_executables={"acme/x": {"hooks": True}})

    result = resolve_project_exec_allow_map(
        tmp_path / "p",
        package,
        user_scope=user_scope,
        executable_context=("<stale>", False),
        no_policy=True,
        logger=logger,
    )

    assert result == "<map>"
    trust_policy.assert_called_once_with(
        (tmp_path / "apm-home") if user_scope else (tmp_path / "p"), no_policy=True, logger=logger
    )
    effective.assert_called_once_with(
        tmp_path / "p",
        user_scope=user_scope,
        policy="<policy>",
        fallback_allow_executables={"acme/x": {"hooks": True}},
        logger=logger,
    )


def test_a_package_without_grants_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(outcome_routing, "resolve_trust_policy", Mock(return_value=None))
    effective = Mock()
    monkeypatch.setattr(executables, "effective_exec_map_for_project", effective)
    resolve_project_exec_allow_map(tmp_path, object(), user_scope=False)
    assert effective.call_args.kwargs["fallback_allow_executables"] is None
    assert effective.call_args.kwargs["policy"] is None


@pytest.mark.parametrize("user_scope", [False, True])
def test_the_project_map_materializes_the_scope_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_scope: bool
) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    context = Mock(return_value="<ctx>")
    monkeypatch.setattr(executables, "exec_trust_context_for_project", context)
    monkeypatch.setattr(executables, "materialize_exec_map", lambda ctx: f"map({ctx})")
    logger = object()
    assert (
        effective_exec_map_for_project(
            tmp_path / "p",
            policy="<policy>",
            fallback_allow_executables={"a/b": {}},
            logger=logger,
            migrate_user_legacy=False,
            user_scope=user_scope,
        )
        == "map(<ctx>)"
    )
    context.assert_called_once_with(
        (tmp_path / "apm-home" / "apm.yml") if user_scope else (tmp_path / "p" / "apm.yml"),
        policy="<policy>",
        fallback_allow_executables={"a/b": {}},
        logger=logger,
        migrate_user_legacy=False,
    )


# -- the decision ladder -------------------------------------------------------------------------


PKG = "acme/tool"
GRANT = {PKG: {"hooks": True}}


def _trust(**fields: object) -> ExecTrustContext:
    values: dict[str, object] = {
        "gate_enabled": True,
        "org_deny_all": False,
        "org_deny": frozenset(),
        "org_require": frozenset(),
        "org_recommend": frozenset(),
        "org_enforce": frozenset(),
        "org_bin_deny_all": False,
        "org_bin_deny": frozenset(),
        "project_allow": {},
        "project_deny": {},
        "user_allow": {},
        "user_deny": {},
    }
    values.update(fields)
    return ExecTrustContext(**values)  # type: ignore[arg-type]


ALL_GRANTS = {
    "project_allow": GRANT,
    "user_allow": GRANT,
    "org_recommend": frozenset({PKG}),
}


@pytest.mark.parametrize(
    ("deny", "layer"),
    [
        ({"org_deny": frozenset({PKG})}, LAYER_ORG_DENY),
        ({"user_deny": GRANT}, LAYER_USER_DENY),
        ({"project_deny": GRANT}, LAYER_PROJECT_DENY),
    ],
)
def test_each_deny_wins_and_names_every_grant_it_shadows(deny: dict, layer: str) -> None:
    decision = resolve_exec_decision(_trust(**ALL_GRANTS, **deny), f"{PKG}#v1", "hooks")
    assert decision == ExecDecision(
        False,
        layer,
        TRUST_DENIED,
        (LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW, LAYER_ORG_RECOMMEND),
    )


@pytest.mark.parametrize(
    ("deny", "layer"),
    [
        ({"org_deny": frozenset({PKG})}, LAYER_ORG_DENY),
        ({"user_deny": GRANT}, LAYER_USER_DENY),
        ({"project_deny": GRANT}, LAYER_PROJECT_DENY),
    ],
)
def test_a_deny_shadows_only_grants_for_that_executable_type(deny: dict, layer: str) -> None:
    grants = {"project_allow": {PKG: {"mcp": True}}, "org_enforce": frozenset({PKG})}
    deny = {
        key: ({PKG: {"hooks": True, "mcp": True}} if isinstance(value, dict) else value)
        for key, value in deny.items()
    }
    decision = resolve_exec_decision(_trust(**grants, **deny), PKG, "hooks")
    assert decision == ExecDecision(False, layer, TRUST_DENIED, (LAYER_ORG_RECOMMEND,))


@pytest.mark.parametrize(
    ("grants", "decision"),
    [
        ({"project_allow": GRANT}, ExecDecision(True, LAYER_PROJECT_ALLOW, TRUST_DEPLOYED)),
        ({"user_allow": GRANT}, ExecDecision(True, LAYER_USER_ALLOW, TRUST_DEPLOYED)),
        (
            {"org_recommend": frozenset({PKG})},
            ExecDecision(True, LAYER_ORG_RECOMMEND, TRUST_DEPLOYED),
        ),
        (
            {"org_enforce": frozenset({PKG})},
            ExecDecision(True, LAYER_ENFORCE_DEGRADED, TRUST_DEPLOYED),
        ),
        (
            {"org_enforce": frozenset({PKG}), "org_recommend": frozenset({PKG})},
            ExecDecision(True, LAYER_ORG_RECOMMEND, TRUST_DEPLOYED),
        ),
        ({}, ExecDecision(False, LAYER_DEFAULT_DENY, TRUST_GATED)),
    ],
)
def test_the_first_grant_rung_decides(grants: dict, decision: ExecDecision) -> None:
    assert resolve_exec_decision(_trust(**grants), PKG, "hooks") == decision


def test_a_recommendation_for_another_package_does_not_hide_degraded_enforcement() -> None:
    ctx = _trust(org_enforce=frozenset({PKG}), org_recommend=frozenset({"other/pkg"}))
    assert resolve_exec_decision(ctx, PKG, "hooks").deciding_layer == LAYER_ENFORCE_DEGRADED


def test_the_project_map_defaults_to_project_scope_and_user_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = Mock(return_value="<ctx>")
    monkeypatch.setattr(executables, "exec_trust_context_for_project", context)
    monkeypatch.setattr(executables, "materialize_exec_map", lambda ctx: ctx)
    effective_exec_map_for_project(tmp_path, policy=None)
    context.assert_called_once_with(
        tmp_path / "apm.yml",
        policy=None,
        fallback_allow_executables=None,
        logger=None,
        migrate_user_legacy=True,
    )


def test_policy_applies_unless_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trust_policy = Mock(return_value=None)
    monkeypatch.setattr(outcome_routing, "resolve_trust_policy", trust_policy)
    monkeypatch.setattr(executables, "effective_exec_map_for_project", Mock())
    resolve_project_exec_allow_map(tmp_path, object(), user_scope=False)
    assert trust_policy.call_args.kwargs == {"no_policy": False, "logger": None}
