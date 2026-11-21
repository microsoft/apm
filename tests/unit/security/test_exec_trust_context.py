"""executable-trust-context owner: one resolver of executable trust per command (#4121)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

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

# Collaborators replaced at their own module; the owner's own resolution chain stays real.
TRUST_POLICY = "apm_cli.policy.outcome_routing.resolve_trust_policy"
CONTENT_BLIND = "apm_cli.security.trust.warn_content_blind_grants_once"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the user layer (APM_HOME and its config.json) under tmp_path."""
    apm_home = tmp_path / "apm-home"
    apm_home.mkdir()
    monkeypatch.setenv("APM_HOME", str(apm_home))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(apm_home / "config.json"))
    return apm_home


@pytest.fixture(autouse=True)
def content_blind(monkeypatch: pytest.MonkeyPatch) -> Mock:
    warn = Mock()
    monkeypatch.setattr(CONTENT_BLIND, warn)
    return warn


def _org(*recommended: str) -> SimpleNamespace:
    """An org policy recommending *recommended*, which allows every executable type."""
    executables = SimpleNamespace(
        recommend=list(recommended), deny=(), require=(), enforce=(), deny_all=False
    )
    return SimpleNamespace(executables=executables, bin_deploy=None)


def _grants(directory: Path, key: str) -> Path:
    """Write an apm.yml in *directory* that allows hooks for *key*."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "apm.yml"
    path.write_text(f"name: p\nexecutables:\n  allow:\n    {key}: {{hooks: true}}\n", "utf-8")
    return path


def _user_grant(home: Path, key: str) -> None:
    config = {"executables": {"allow": {key: {"hooks": True}}, "deny": {}}}
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _legacy_grant(home: Path, key: str) -> Path:
    legacy = home / "approvals.yml"
    legacy.write_text(f"{key}: {{hooks: true}}\n", encoding="utf-8")
    return legacy


def _manifest(tmp_path: Path, body: str) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    path = project / "apm.yml"
    path.write_text(body, encoding="utf-8")
    return path


def _fresh_executables() -> ModuleType:
    """Import the executables module afresh: its warn-once state is a new run's."""
    spec = importlib.util.find_spec("apm_cli.security.executables")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_manifest_grants_feed_the_context(home: Path, tmp_path: Path) -> None:
    manifest = _grants(tmp_path / "project", "acme/x")
    _user_grant(home, "acme/user")
    ctx = exec_trust_context_for_project(
        manifest,
        policy=_org("acme/org"),
        fallback_allow_executables={"ignored/fallback": {"hooks": True}},
        migrate_user_legacy=False,
        include_user=False,
    )
    assert ctx.project_allow == {"acme/x": {"hooks": True}}
    assert ctx.org_recommend == frozenset({"acme/org"})
    assert ctx.user_allow == {}
    assert ctx.gate_enabled is True


LOCK_WITH_MARKETPLACE_ROW = """lockfile_version: '1'
dependencies:
- repo_url: acme/tool
  discovered_via: mkt
  marketplace_plugin_name: tool
"""


def test_the_scope_lockfile_names_the_package_an_install_spec_grants(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "name: p\nexecutables:\n  allow:\n    tool@mkt: {mcp: true}\n")
    (manifest.parent / "apm.lock.yaml").write_text(LOCK_WITH_MARKETPLACE_ROW, encoding="utf-8")
    ctx = exec_trust_context_for_project(manifest, policy=None)
    assert ctx.project_allow["acme/tool"] == {"mcp": True}


def test_without_a_manifest_the_fallback_and_user_grants_apply(home: Path, tmp_path: Path) -> None:
    legacy = _legacy_grant(home, "acme/legacy")
    ctx = exec_trust_context_for_project(
        tmp_path / "missing" / "apm.yml",
        policy=None,
        fallback_allow_executables={"acme/x": {"mcp": True}},
    )
    assert ctx.project_allow == {"acme/x": {"mcp": True}}
    assert ctx.user_allow == {"acme/legacy": {"hooks": True}}
    assert not legacy.exists()


def test_legacy_user_grants_are_read_without_migrating_when_asked(
    home: Path, tmp_path: Path
) -> None:
    legacy = _legacy_grant(home, "acme/legacy")
    ctx = exec_trust_context_for_project(
        tmp_path / "apm.yml", policy=None, migrate_user_legacy=False
    )
    assert ctx.user_allow == {"acme/legacy": {"hooks": True}}
    assert legacy.exists()


@pytest.mark.parametrize("fallback", [None, [("acme/x", {})]])
def test_without_a_manifest_or_usable_fallback_there_is_no_project_data(
    tmp_path: Path, fallback: object
) -> None:
    ctx = exec_trust_context_for_project(
        tmp_path / "apm.yml",
        policy=None,
        fallback_allow_executables=fallback,  # type: ignore[arg-type]
    )
    assert (ctx.gate_enabled, ctx.project_allow) == (False, {})


def test_a_non_mapping_manifest_falls_back(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "- just\n- a list\n")
    ctx = exec_trust_context_for_project(
        manifest, policy=None, fallback_allow_executables={"a/b": {"hooks": True}}
    )
    assert ctx.project_allow == {"a/b": {"hooks": True}}


def test_the_deprecated_alias_warns_once_through_the_logger(tmp_path: Path) -> None:
    fresh = _fresh_executables()
    manifest = _manifest(tmp_path, "name: p\nallowExecutables:\n  acme/x: {hooks: true}\n")
    logger = Mock()
    fresh.exec_trust_context_for_project(manifest, policy=None, logger=logger)
    fresh.exec_trust_context_for_project(manifest, policy=None, logger=logger)
    assert logger.warning.call_count == 1
    assert "'allowExecutables' in apm.yml is deprecated" in logger.warning.call_args.args[0]


def test_an_empty_alias_does_not_warn(tmp_path: Path) -> None:
    fresh = _fresh_executables()
    manifest = _manifest(tmp_path, "name: p\nallowExecutables:\n")
    logger = Mock()
    fresh.exec_trust_context_for_project(manifest, policy=None, logger=logger)
    logger.warning.assert_not_called()


def test_content_blind_grants_are_reported_only_with_a_logger(
    tmp_path: Path, content_blind: Mock
) -> None:
    manifest = _manifest(tmp_path, "name: p\n")
    exec_trust_context_for_project(manifest, policy=None)
    content_blind.assert_not_called()
    logger = object()
    ctx = exec_trust_context_for_project(manifest, policy=None, logger=logger)
    content_blind.assert_called_once_with(ctx, logger)


# -- the per-command allow map -------------------------------------------------------------------


@pytest.mark.parametrize("given", [None, EffectiveExecMap({"acme/x": {"hooks": True}})])
def test_a_map_the_caller_resolved_is_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: object
) -> None:
    monkeypatch.setattr(TRUST_POLICY, Mock(side_effect=AssertionError))
    assert (
        resolve_project_exec_allow_map(
            tmp_path, object(), user_scope=False, executable_context=(given, True)
        )
        is given
    )


@pytest.mark.parametrize("user_scope", [False, True])
def test_an_unresolved_map_resolves_policy_and_grants_from_the_scope_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    home: Path,
    content_blind: Mock,
    user_scope: bool,
) -> None:
    trust_policy = Mock(return_value=_org("acme/org"))
    monkeypatch.setattr(TRUST_POLICY, trust_policy)
    _grants(home, "acme/home")
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

    trust_policy.assert_called_once_with(
        home if user_scope else (tmp_path / "p"), no_policy=True, logger=logger
    )
    # The project has no apm.yml, so the package's grants stand in for it.
    assert set(result) == {"acme/org", "acme/home" if user_scope else "acme/x"}
    assert content_blind.call_args.args[1] is logger


def test_a_package_without_grants_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(TRUST_POLICY, Mock(return_value=None))
    result = resolve_project_exec_allow_map(tmp_path, object(), user_scope=False)
    assert (dict(result), result.authorized_by) == ({}, AUTHORIZED_GATE_DISABLED)


@pytest.mark.parametrize("user_scope", [False, True])
def test_the_project_map_materializes_the_scope_context(
    tmp_path: Path, home: Path, content_blind: Mock, user_scope: bool
) -> None:
    _grants(tmp_path / "p", "acme/proj")
    _grants(home, "acme/home")
    legacy = _legacy_grant(home, "acme/legacy")
    logger = object()
    allow_map = effective_exec_map_for_project(
        tmp_path / "p",
        policy=_org("acme/org"),
        fallback_allow_executables={"a/b": {"hooks": True}},
        logger=logger,
        migrate_user_legacy=False,
        user_scope=user_scope,
    )
    scope_key = "acme/home" if user_scope else "acme/proj"
    assert set(allow_map) == {scope_key, "acme/org", "acme/legacy"}
    assert allow_map[scope_key] == {"hooks": True}
    assert legacy.exists()
    assert content_blind.call_args.args[1] is logger


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
    tmp_path: Path, home: Path, content_blind: Mock
) -> None:
    _grants(home, "acme/home")
    legacy = _legacy_grant(home, "acme/legacy")
    allow_map = effective_exec_map_for_project(
        tmp_path / "p", policy=None, fallback_allow_executables={"a/b": {"hooks": True}}
    )
    assert set(allow_map) == {"a/b", "acme/legacy"}
    assert not legacy.exists()
    content_blind.assert_not_called()


def test_policy_applies_unless_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trust_policy = Mock(return_value=None)
    monkeypatch.setattr(TRUST_POLICY, trust_policy)
    resolve_project_exec_allow_map(tmp_path, object(), user_scope=False)
    assert trust_policy.call_args.kwargs == {"no_policy": False, "logger": None}
