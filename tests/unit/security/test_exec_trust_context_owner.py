"""Owner tests for the executable trust context (executable-trust-context).

Table-driven at ``apm_cli.security.executables`` so every deny-wins decision,
the trust-context inputs and the user-facing trust warnings are pinned (see
tests/mutation/floors.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.executables as ex
import apm_cli.security.trust as trust
from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    LAYER_DEFAULT_DENY,
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
    ExecDecision,
    ExecTrustContext,
    _deny_glob_match,
    exec_trust_context_for_project,
    resolve_exec_decision,
    resolve_project_exec_allow_map,
)

pytestmark = pytest.mark.component

PKG = "owner/repo#v1.0"
NAME = "owner/repo"
SHADOWED = (LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW, LAYER_ORG_RECOMMEND)


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


def _granted(**kw) -> ExecTrustContext:
    # Every lower layer grants hooks; only the project layer also grants lsp.
    return _ctx(
        project_allow={NAME: {"hooks": True, "lsp": True}},
        user_allow={NAME: {"hooks": True}},
        org_recommend=frozenset({NAME}),
        **kw,
    )


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (
            _granted(org_deny=frozenset({NAME})),
            ExecDecision(False, LAYER_ORG_DENY, TRUST_DENIED, SHADOWED),
        ),
        (
            _granted(user_deny={NAME: {"hooks": True}}),
            ExecDecision(False, LAYER_USER_DENY, TRUST_DENIED, SHADOWED),
        ),
        (
            _granted(project_deny={NAME: {"hooks": True}}),
            ExecDecision(False, LAYER_PROJECT_DENY, TRUST_DENIED, SHADOWED),
        ),
        (
            _ctx(user_allow={NAME: {"hooks": True}}),
            ExecDecision(True, LAYER_USER_ALLOW, TRUST_DEPLOYED, ()),
        ),
        (
            _ctx(org_enforce=frozenset({NAME}), org_recommend=frozenset({NAME})),
            ExecDecision(True, LAYER_ORG_RECOMMEND, TRUST_DEPLOYED, ()),
        ),
        (
            _ctx(org_bin_deny_all=True),
            ExecDecision(False, LAYER_DEFAULT_DENY, TRUST_GATED, ()),
        ),
    ],
)
def test_hooks_decision_is_exact(ctx: ExecTrustContext, expected: ExecDecision) -> None:
    assert resolve_exec_decision(ctx, PKG, EXEC_TYPE_HOOKS) == expected


def test_shadowed_grants_are_scoped_to_the_denied_exec_type() -> None:
    ctx = _ctx(
        project_allow={NAME: {"lsp": True}},
        user_allow={NAME: {"lsp": True}},
        project_deny={NAME: {"hooks": True, "lsp": True}},
    )

    assert resolve_exec_decision(ctx, PKG, EXEC_TYPE_HOOKS).shadowed_layers == ()
    assert resolve_exec_decision(ctx, PKG, "lsp").shadowed_layers == (
        LAYER_PROJECT_ALLOW,
        LAYER_USER_ALLOW,
    )


def test_bin_deny_all_denies_only_bin() -> None:
    ctx = _ctx(org_bin_deny_all=True, project_allow={NAME: {"bin": True}})

    assert resolve_exec_decision(ctx, PKG, EXEC_TYPE_BIN) == ExecDecision(
        False, LAYER_ORG_DENY_ALL, TRUST_DENIED, (LAYER_PROJECT_ALLOW,)
    )


@pytest.mark.parametrize(
    ("name", "patterns", "expected"),
    [
        ("evil/x", ["evil/"], True),
        ("acmeX/y", ["acmeX"], True),
        ("X/y", ["X"], True),
        ("/x", ["/"], False),
        ("/x", ["//"], False),
        ("good/x", ["evil/*"], False),
        ("evil/x", ["evil/*"], True),
    ],
)
def test_deny_glob_match_parent_and_slash_patterns(
    name: str, patterns: list[str], expected: bool
) -> None:
    assert _deny_glob_match(name, patterns) is expected


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, symbol: str | None = None) -> None:
        self.warnings.append(message)


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "apm-home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.setattr(ex, "_user_config_file", lambda: home / "config.json")
    monkeypatch.setattr(ex, "_ALIAS_DEPRECATION_WARNED", False)
    monkeypatch.setattr(trust, "_CONTENT_BLIND_WARNED", False)
    return home


def _manifest(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "apm.yml"
    path.write_text("name: p\nversion: 1.0.0\n" + body, encoding="utf-8")
    return path


def test_fallback_grants_apply_only_without_a_manifest(user_home: Path, tmp_path: Path) -> None:
    fallback = {"fallback/pkg": {"hooks": True}}

    missing = exec_trust_context_for_project(
        tmp_path / "none" / "apm.yml", policy=None, fallback_allow_executables=fallback
    )
    assert missing.project_allow == fallback

    manifest = _manifest(tmp_path / "p", "executables:\n  allow:\n    own/pkg:\n      mcp: true\n")
    present = exec_trust_context_for_project(
        manifest, policy=None, fallback_allow_executables=fallback
    )
    assert present.project_allow == {"own/pkg": {"mcp": True}}


def test_alias_warning_only_for_the_deprecated_block(user_home: Path, tmp_path: Path) -> None:
    logger = _Logger()
    current = _manifest(tmp_path / "a", "executables:\n  allow:\n    own/pkg:\n      mcp: true\n")
    exec_trust_context_for_project(current, policy=None, logger=logger)
    assert not any("allowExecutables" in w for w in logger.warnings)

    legacy = _manifest(tmp_path / "b", "allowExecutables:\n  own/pkg:\n    mcp: true\n")
    exec_trust_context_for_project(legacy, policy=None, logger=logger)
    assert (
        "'allowExecutables' in apm.yml is deprecated; it now maps to 'executables.allow'. "
        "It will migrate automatically on your next 'apm approve'/'apm deny'."
    ) in logger.warnings


def test_content_blind_grants_warn_through_the_logger(user_home: Path, tmp_path: Path) -> None:
    logger = _Logger()
    manifest = _manifest(
        tmp_path / "p", "executables:\n  allow:\n    own/pkg:\n      hooks: true\n"
    )

    exec_trust_context_for_project(manifest, policy=None, logger=logger)

    assert logger.warnings == [
        "1 executable approval is not pinned to content: own/pkg (hooks). "
        "Upstream changes deploy without review.\n"
        "    Review: apm approve --list  |  Pin: apm approve <package>"
    ]


def test_user_layer_migrates_legacy_approvals_by_default(user_home: Path, tmp_path: Path) -> None:
    legacy = user_home / "approvals.yml"
    legacy.write_text("legacy/pkg:\n  hooks: true\n", encoding="utf-8")
    missing = tmp_path / "none" / "apm.yml"

    kept = exec_trust_context_for_project(missing, policy=None, migrate_user_legacy=False)
    assert kept.user_allow == {"legacy/pkg": {"hooks": True}}
    assert legacy.is_file()

    migrated = exec_trust_context_for_project(missing, policy=None)
    assert migrated.user_allow == {"legacy/pkg": {"hooks": True}}
    assert not legacy.exists()
    saved = json.loads((user_home / "config.json").read_text(encoding="utf-8"))
    assert saved["executables"]["allow"] == {"legacy/pkg": {"hooks": True}}

    excluded = exec_trust_context_for_project(missing, policy=None, include_user=False)
    assert excluded.user_allow == {}


def test_project_map_threads_policy_switch_logger_and_package_fallback(
    user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    calls: list[dict] = []

    def _resolve(project_root, **kwargs):
        calls.append({"root": project_root, **kwargs})

    monkeypatch.setattr("apm_cli.policy.outcome_routing.resolve_trust_policy", _resolve)
    logger = _Logger()
    package = SimpleNamespace(allow_executables={"pkg/fallback": {"hooks": True}})

    allow_map = resolve_project_exec_allow_map(
        root, package, user_scope=False, no_policy=True, logger=logger
    )

    assert calls == [{"root": root, "no_policy": True, "logger": logger}]
    assert allow_map == {"pkg/fallback": {"hooks": True}}
    assert [w.split(":")[0] for w in logger.warnings] == [
        "1 executable approval is not pinned to content"
    ]
