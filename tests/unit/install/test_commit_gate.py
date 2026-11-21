"""install-commit-order owner: every check passes before anything is committed (#4121)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install import commit_gate, integrity, source_helpers, template
from apm_cli.install.commit_gate import (
    check_before_deploy,
    check_before_lockfile,
    check_before_resolve,
    enforce_mcp_policy,
    policy_subject_mcp_deps,
    prospective_lockfile,
    refuse_blocked_mcp,
)
from apm_cli.install.errors import InstallFailureAlreadyRendered
from apm_cli.install.mcp import integration as mcp_integration
from apm_cli.policy import install_preflight, outcome_routing

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-commit-order")]


def _server(name: str, native: set[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, native_runtimes=native)


# -- which MCP servers policy judges -------------------------------------------------


def test_a_server_every_active_runtime_loads_natively_is_not_a_policy_subject() -> None:
    native = _server("n", {"claude", "copilot"})
    partial = _server("p", {"claude"})
    plain = _server("x")
    subjects = policy_subject_mcp_deps(
        [native, partial, plain], runtime="claude", target_decision=None, user_scope=False
    )
    assert subjects == [plain]
    decision = Mock()
    decision.runtime_targets_for_scope.return_value = ("claude", "copilot")
    assert policy_subject_mcp_deps(
        [native, partial, plain], runtime=None, target_decision=decision, user_scope=True
    ) == [partial, plain]
    decision.runtime_targets_for_scope.assert_called_once_with(user_scope=True)


@pytest.mark.parametrize("runtimes", [None, ()])
def test_without_active_runtimes_every_server_is_a_subject(runtimes: object) -> None:
    servers = [_server("n", {"claude"}), _server("x")]
    decision = Mock()
    decision.runtime_targets_for_scope.return_value = runtimes
    assert (
        policy_subject_mcp_deps(servers, runtime=None, target_decision=decision, user_scope=False)
        == servers
    )
    assert (
        policy_subject_mcp_deps(servers, runtime=None, target_decision=None, user_scope=False)
        == servers
    )


# -- enforcing MCP policy ---------------------------------------------------------------


@pytest.fixture
def preflight(monkeypatch: pytest.MonkeyPatch) -> Mock:
    called = Mock()
    monkeypatch.setattr(install_preflight, "run_policy_preflight", called)
    return called


def test_no_subject_and_no_target_projection_skips_policy(preflight: Mock, tmp_path: Path) -> None:
    enforce_mcp_policy(
        [_server("n", {"claude"})],
        brought_in_by=None,
        runtime="claude",
        target_decision=None,
        user_scope=False,
        project_root=tmp_path,
        no_policy=False,
        logger=None,
    )
    preflight.assert_not_called()


@pytest.mark.parametrize("user_scope", [False, True])
def test_policy_judges_the_subjects_from_the_scope_manifest(
    preflight: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_scope: bool
) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    server, logger = _server("x"), object()
    enforce_mcp_policy(
        [server],
        brought_in_by={"x": "acme/pkg"},
        runtime="claude",
        target_decision=None,
        user_scope=user_scope,
        project_root=tmp_path / "project",
        no_policy=True,
        logger=logger,
        dry_run=True,
    )
    preflight.assert_called_once_with(
        project_root=(tmp_path / "apm-home") if user_scope else (tmp_path / "project"),
        mcp_deps=[server],
        mcp_brought_in_by={"x": "acme/pkg"},
        no_policy=True,
        logger=logger,
        dry_run=True,
        effective_target=None,
        effective_target_source="effective target",
    )


def test_a_target_projection_is_judged_even_without_servers(
    preflight: Mock, tmp_path: Path
) -> None:
    enforce_mcp_policy(
        [],
        brought_in_by=None,
        runtime=None,
        target_decision=None,
        user_scope=False,
        project_root=tmp_path,
        no_policy=False,
        logger=None,
        effective_target=("copilot",),
        effective_target_source="--target flag",
    )
    kwargs = preflight.call_args.kwargs
    assert (kwargs["mcp_deps"], kwargs["effective_target"], kwargs["dry_run"]) == (
        [],
        ("copilot",),
        False,
    )
    assert kwargs["effective_target_source"] == "--target flag"


# -- the MCP refusal ---------------------------------------------------------------------

SUMMARY = "MCP server(s) blocked by org policy. MCP configs were NOT written."


@pytest.mark.parametrize(
    ("exc", "line"),
    [
        (None, SUMMARY),
        (RuntimeError("no reachable policy and fetch_failure_default=block"), None),
        (RuntimeError(""), SUMMARY),
        (SimpleNamespace(audit_result=object(), __str__=lambda self: "x"), SUMMARY),
    ],
)
def test_the_refusal_prints_its_reason_or_the_summary_then_fails(
    exc: object, line: str | None
) -> None:
    logger = Mock()
    with pytest.raises(InstallFailureAlreadyRendered) as raised:
        refuse_blocked_mcp(logger, exc)  # type: ignore[arg-type]
    expected = line if line is not None else str(exc)
    logger.refusal.assert_called_once_with(expected)
    logger.render_summary.assert_called_once_with()
    assert str(raised.value) == "MCP server(s) blocked by org policy"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True


def test_a_check_refusal_with_violations_prints_only_the_summary() -> None:
    class Checked(RuntimeError):
        audit_result = object()

    logger = Mock()
    with pytest.raises(InstallFailureAlreadyRendered):
        refuse_blocked_mcp(logger, Checked("3 violations"))
    logger.refusal.assert_called_once_with(SUMMARY)


# -- the prospective lockfile ---------------------------------------------------------------


def _locked(repo: str, **fields: object) -> LockedDependency:
    return LockedDependency(repo_url=repo, **fields)


def _ctx(**fields: object) -> SimpleNamespace:
    values = {
        "dry_run": False,
        "installed_packages": ["<installed>"],
        "dependency_graph": None,
        "deps_to_install": [],
        "package_hashes": {},
        "package_types": {},
        "update_plan_complete_dep_keys": set(),
        "existing_lockfile": None,
        "frozen": False,
        "no_policy": False,
        "project_root": Path("/project"),
        "apm_dir": Path("/project"),
        "scope": InstallScope.PROJECT,
        "install_mcp": False,
        "lockfile_only": False,
        "apm_package": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


@pytest.fixture
def acquired(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object]]:
    calls: list[tuple[object, object]] = []

    def from_installed(packages: object, graph: object) -> LockFile:
        calls.append((packages, graph))
        return LockFile(
            apm_version="9.9.9",
            dependencies={"acme/a": _locked("acme/a"), "acme/b": _locked("acme/b")},
        )

    monkeypatch.setattr(LockFile, "from_installed_packages", staticmethod(from_installed))
    return calls


def test_acquired_rows_take_their_hashes_and_types(acquired: list) -> None:
    ctx = _ctx(
        package_hashes={"acme/a": "sha256:aa", "acme/z": "sha256:zz"},
        package_types={"acme/b": "claude_plugin"},
        dependency_graph="<graph>",
    )
    prospective = prospective_lockfile(ctx)
    assert acquired == [(["<installed>"], "<graph>")]
    assert prospective.apm_version == "9.9.9"
    assert list(prospective.dependencies) == ["acme/a", "acme/b"]
    assert prospective.dependencies["acme/a"].content_hash == "sha256:aa"
    assert prospective.dependencies["acme/b"].content_hash is None
    assert prospective.dependencies["acme/b"].package_type == "claude_plugin"
    assert prospective.dependencies["acme/a"].package_type is None


def test_existing_rows_survive_only_when_active_and_not_reacquired(acquired: list) -> None:
    existing = LockFile(
        dependencies={
            "acme/a": _locked("acme/a", version="old"),
            "acme/kept": _locked("acme/kept"),
            "acme/dropped": _locked("acme/dropped"),
        }
    )
    ctx = _ctx(existing_lockfile=existing, update_plan_complete_dep_keys={"acme/a", "acme/kept"})
    prospective = prospective_lockfile(ctx)
    assert list(prospective.dependencies) == ["acme/kept", "acme/a", "acme/b"]
    assert prospective.dependencies["acme/kept"] is existing.dependencies["acme/kept"]
    assert prospective.dependencies["acme/a"].version is None


def test_a_dry_run_builds_rows_from_the_resolved_packages(
    acquired: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(
        source_helpers,
        "locked_parent_identity",
        lambda ctx, node: seen.append(ctx) or f"parent:{node}",
    )
    dep_a = SimpleNamespace(get_unique_key=lambda: "acme/a")
    dep_b = SimpleNamespace(get_unique_key=lambda: "acme/b")
    node = SimpleNamespace(depth=3, is_dev=1)
    tree = SimpleNamespace(get_node=lambda key: node if key == "acme/a" else None)
    graph = SimpleNamespace(dependency_tree=tree)
    ctx = _ctx(dry_run=True, deps_to_install=[dep_a, dep_b], dependency_graph=graph)
    prospective_lockfile(ctx)
    assert seen == [ctx, ctx]
    rows, passed_graph = acquired[0]
    assert passed_graph is graph
    assert rows == [
        (dep_a, None, 3, f"parent:{node}", True),
        (dep_b, None, 1, "parent:None", False),
    ]


def test_a_dry_run_without_a_graph_defaults_every_row(
    acquired: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_helpers, "locked_parent_identity", lambda ctx, node: None)
    dep = SimpleNamespace(get_unique_key=Mock(side_effect=AssertionError))
    prospective_lockfile(_ctx(dry_run=True, deps_to_install=[dep], dependency_graph=None))
    assert acquired[0][0] == [(dep, None, 1, None, False)]


# -- the three commit points --------------------------------------------------------------------


@pytest.fixture
def hashes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    mocks = {"installed": Mock(), "policy": Mock(), "trust": Mock(return_value="<policy>")}
    monkeypatch.setattr(integrity, "enforce_installed_hash_policy", mocks["installed"])
    monkeypatch.setattr(integrity, "enforce_policy_hashes", mocks["policy"])
    monkeypatch.setattr(outcome_routing, "resolve_trust_policy", mocks["trust"])
    return mocks


@pytest.mark.parametrize(
    "fields",
    [{"dry_run": True}, {"no_policy": True}, {"frozen": False}],
)
def test_a_committed_lockfile_is_judged_early_only_for_frozen_installs(
    hashes: dict[str, Mock], fields: dict
) -> None:
    check_before_resolve(_ctx(**{"frozen": True, **fields}), LockFile())
    check_before_resolve(_ctx(frozen=True), None)
    hashes["trust"].assert_not_called()
    hashes["policy"].assert_not_called()


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_a_frozen_install_judges_the_committed_lockfile_before_resolution(
    hashes: dict[str, Mock], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: InstallScope
) -> None:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    committed = LockFile()
    check_before_resolve(
        _ctx(frozen=True, scope=scope, project_root=tmp_path / "p", apm_dir=tmp_path / "meta"),
        committed,
    )
    hashes["trust"].assert_called_once_with(
        (tmp_path / "apm-home") if scope is InstallScope.USER else (tmp_path / "p"),
        announce=False,
        raise_blocking_errors=False,
    )
    hashes["policy"].assert_called_once_with(
        "<policy>", committed, tmp_path / "meta" / "apm.lock.yaml"
    )


def test_a_dry_run_deploy_check_skips_content_hashes(
    hashes: dict[str, Mock], acquired: list
) -> None:
    check_before_deploy(_ctx(dry_run=True, dependency_graph=None))
    hashes["installed"].assert_not_called()


@pytest.mark.parametrize("frozen", [False, True])
def test_the_deploy_check_judges_what_will_be_committed(
    hashes: dict[str, Mock], acquired: list, frozen: bool
) -> None:
    existing = LockFile(dependencies={"acme/e": _locked("acme/e")})
    ctx = _ctx(frozen=frozen, existing_lockfile=existing)
    check_before_deploy(ctx)
    judged_ctx, judged = hashes["installed"].call_args.args
    assert judged_ctx is ctx
    if frozen:
        assert judged is existing
    else:
        assert list(judged.dependencies) == ["acme/a", "acme/b"]


@pytest.mark.parametrize(
    ("fields", "checked"),
    [
        ({"install_mcp": True, "apm_package": object()}, True),
        ({"install_mcp": False, "apm_package": object()}, False),
        ({"install_mcp": True, "lockfile_only": True, "apm_package": object()}, False),
        ({"install_mcp": True, "apm_package": None}, False),
    ],
)
def test_the_deploy_check_collects_mcp_policy_only_when_mcp_installs(
    hashes: dict[str, Mock],
    acquired: list,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict,
    checked: bool,
) -> None:
    merged = Mock()
    monkeypatch.setattr(commit_gate, "_check_merged_mcp_policy", merged)
    ctx = _ctx(**fields)
    check_before_deploy(ctx)
    assert merged.called is checked
    if checked:
        assert merged.call_args.args[0] is ctx
        assert list(merged.call_args.args[1].dependencies) == ["acme/a", "acme/b"]


def test_the_lockfile_check_judges_the_built_lockfile(hashes: dict[str, Mock]) -> None:
    ctx, built = _ctx(), LockFile()
    check_before_lockfile(ctx, built)
    hashes["installed"].assert_called_once_with(ctx, built)


# -- the merged MCP policy check -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "view"), [(InstallScope.PROJECT, True), (InstallScope.USER, False)]
)
def test_the_merged_mcp_check_collects_silently_and_enforces_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: InstallScope, view: bool
) -> None:
    server = _server("x")
    current_view = SimpleNamespace(brought_in_by={"x": "acme/p"}) if view else None
    prepare = Mock(return_value=SimpleNamespace(dependencies=[server], current_view=current_view))
    enforce = Mock()
    monkeypatch.setattr(mcp_integration, "prepare_mcp_integration", prepare)
    allow_for: list[object] = []
    monkeypatch.setattr(
        template, "effective_allow_map", lambda ctx: allow_for.append(ctx) or "<allow>"
    )
    monkeypatch.setattr(commit_gate, "enforce_mcp_policy", enforce)
    package = Mock()
    package.get_all_mcp_dependencies.return_value = ("m1", "m2")
    logger = Mock(spec=["refusal", "policy_resolved"])
    ctx = _ctx(
        scope=scope,
        apm_package=package,
        apm_modules_dir=tmp_path / "apm_modules",
        apm_dir=tmp_path,
        trust_transitive_mcp=True,
        update_plan_complete_dep_keys={"acme/p"},
        claude_native_keys={"acme/n"},
        mcp_runtime="copilot",
        target_decision="<decision>",
        logger=logger,
        dry_run=True,
    )
    prospective = LockFile()

    commit_gate._check_merged_mcp_policy(ctx, prospective)

    kwargs = prepare.call_args.kwargs
    assert kwargs["apm_package"] is package
    assert kwargs["mcp_deps"] == ["m1", "m2"]
    assert kwargs["apm_modules_path"] == tmp_path / "apm_modules"
    assert kwargs["lock_path"] == tmp_path / "apm.lock.yaml"
    assert kwargs["should_install"] is True
    assert kwargs["trust_transitive_mcp"] is True
    assert kwargs["active_package_keys"] == frozenset({"acme/p"})
    assert kwargs["user_scope"] is (scope is InstallScope.USER)
    assert kwargs["project_root"] == Path("/project")
    assert kwargs["executable_context"] == ("<allow>", True)
    assert allow_for == [ctx]
    assert kwargs["no_policy"] is False
    assert kwargs["claude_native_keys"] == frozenset({"acme/n"})
    assert kwargs["prospective_lock"] is prospective
    silent = kwargs["logger"]
    assert silent.verbose is False and silent.anything("x", y=1) is None
    assert ctx.preview_mcp_dependencies == (server,)
    assert ctx.mcp_policy_enforced is True

    (servers,), enforce_kwargs = enforce.call_args
    assert servers == [server]
    assert enforce_kwargs["brought_in_by"] == ({"x": "acme/p"} if view else None)
    assert (enforce_kwargs["runtime"], enforce_kwargs["target_decision"]) == (
        "copilot",
        "<decision>",
    )
    assert enforce_kwargs["user_scope"] is (scope is InstallScope.USER)
    assert (enforce_kwargs["project_root"], enforce_kwargs["no_policy"]) == (
        Path("/project"),
        False,
    )
    assert enforce_kwargs["dry_run"] is True
    announced = enforce_kwargs["logger"]
    assert announced.policy_resolved(source="s") is None
    assert announced.policy_disabled("r") is None
    assert announced.policy_discovery_miss("o", source="s") is None
    announced.refusal("blocked")
    logger.refusal.assert_called_once_with("blocked")
    logger.policy_resolved.assert_not_called()


def test_policy_subjects_follow_the_target_decision_for_the_scope(
    preflight: Mock, tmp_path: Path
) -> None:
    native = _server("n", {"claude"})
    plain = _server("x")
    decision = Mock()
    decision.runtime_targets_for_scope.return_value = ("claude",)
    enforce_mcp_policy(
        [native, plain],
        brought_in_by=None,
        runtime=None,
        target_decision=decision,
        user_scope=True,
        project_root=tmp_path,
        no_policy=False,
        logger=None,
    )
    decision.runtime_targets_for_scope.assert_called_once_with(user_scope=True)
    assert preflight.call_args.kwargs["mcp_deps"] == [plain]


def test_a_context_without_a_scope_is_project_scope(
    hashes: dict[str, Mock], tmp_path: Path
) -> None:
    ctx = _ctx(frozen=True, project_root=tmp_path / "p", apm_dir=tmp_path)
    del ctx.scope
    check_before_resolve(ctx, LockFile())
    assert hashes["trust"].call_args.args == (tmp_path / "p",)
