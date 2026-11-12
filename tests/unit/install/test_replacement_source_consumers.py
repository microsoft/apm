"""Filesystem-backed consumers distinguish ownership from the selected source."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
from apm_cli.commands.deps.cli import _build_dep_tree
from apm_cli.commands.deps.why import _render_human, _render_json
from apm_cli.commands.outdated import _check_one_dep
from apm_cli.commands.uninstall.engine import _compute_actual_orphans
from apm_cli.core.command_logger import InstallLogger
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.path_anchoring import LocalResolutionError
from apm_cli.deps.reachability import ReachabilityResult
from apm_cli.deps.why_walker import compute_why
from apm_cli.install.context import InstallContext
from apm_cli.install.drift import _materialize_install_path
from apm_cli.install.exec_gate import check_executable_approval, resolve_package_key
from apm_cli.install.helpers.plan_inputs import build_resolved_update_plan
from apm_cli.install.helpers.ref_reuse import maybe_resolve_git_semver
from apm_cli.install.helpers.ref_seed import seed_ref_resolver_from_lockfile
from apm_cli.install.native_plugin_admission import verify_native_plugin_cache
from apm_cli.install.pipeline import _preflight_auth_check
from apm_cli.install.plan import build_update_plan
from apm_cli.install.presentation.resolution import report_resolved_dependencies
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.materialization import (
    build_materialization_path,
    prepare_materialization_path,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import (
    build_replacement_index,
    parse_replace_block,
    replacement_rule_to_dict,
)
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef
from apm_cli.policy.policy_checks import _check_dependency_allowlist, _check_dependency_denylist
from apm_cli.policy.schema import DependencyPolicy
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = pytest.mark.component


def _locked(tmp_path: Path, source: dict, *, parent: str | None = None):
    original = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo#upstream")
    rule = parse_replace_block(
        [{"for": {"git": "https://github.com/MixedOrg/MixedRepo"}, "with": source}]
    )[0]
    selected = build_replacement_index([rule]).bind(
        original,
        consumer_root=tmp_path,
        modules_dir=tmp_path / "apm_modules",
        parent=parent,
        is_dev=False,
    )
    locked = LockedDependency.from_dependency_ref(
        selected, None if selected.is_local else "a" * 40, 1, parent
    )
    lock = LockFile(replacements=[replacement_rule_to_dict(rule)])
    lock.add_dependency(locked)
    return selected, locked, lock


@pytest.mark.parametrize("scoped", [False, True])
def test_plan_inputs_hash_staged_local_replacement_before_canonical_plan(
    tmp_path: Path, scoped: bool
) -> None:
    selected, locked, lock = _locked(tmp_path, {"path": "../source-not-read"})
    modules = tmp_path / "staged-modules"
    staged = selected.get_install_path(modules)
    staged.mkdir(parents=True)
    payload = staged / "payload.txt"
    payload.write_text("before", encoding="utf-8")
    locked.content_hash = compute_package_hash(staged)
    payload.write_text("after", encoding="utf-8")
    expected_hash = compute_package_hash(staged)
    remote = DependencyReference.parse("example/unmatched")
    ordinary_local = DependencyReference.parse("./ordinary-not-read")
    context = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        apm_modules_dir=modules,
        deps_to_install=[selected, remote, ordinary_local],
        only_packages=[selected.get_unique_key()] if scoped else None,
        update_plan_complete_dep_keys={selected.get_unique_key(), "example/retained"},
        package_hashes={remote.get_unique_key(): "sha256:retained"},
    )

    with (
        patch(
            "apm_cli.utils.content_hash.compute_package_hash", wraps=compute_package_hash
        ) as hashing,
        patch("apm_cli.install.plan.build_update_plan", wraps=build_update_plan) as planning,
    ):
        plan = build_resolved_update_plan(context, lock)

    hashing.assert_called_once_with(staged)
    planning.assert_called_once_with(
        lock,
        context.deps_to_install,
        complete_resolved_dep_keys=context.update_plan_complete_dep_keys if scoped else None,
        resolved_content_hashes={
            remote.get_unique_key(): "sha256:retained",
            selected.get_unique_key(): expected_hash,
        },
    )
    entry = next(entry for entry in plan.entries if entry.dep_key == selected.get_unique_key())
    assert entry.action == "update"
    assert entry.old_content_hash == locked.content_hash
    assert entry.new_content_hash == expected_hash


@pytest.mark.parametrize("verbose", [False, True])
def test_resolution_presenter_defers_unused_rules_and_preserves_event_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verbose: bool
) -> None:
    rules = parse_replace_block(
        [
            {"for": {"git": "https://github.com/example/used"}, "with": {"path": "../used"}},
            {
                "for": {"git": "https://github.com/example/dormant"},
                "with": {"path": "../never-accessed"},
            },
        ]
    )
    resolver = APMDependencyResolver()
    resolver.replacement_index = build_replacement_index(rules)
    selected = resolver.replacement_index.bind(
        DependencyReference.parse("example/used#upstream"),
        consumer_root=tmp_path,
        modules_dir=tmp_path / "apm_modules",
        parent=None,
        is_dev=False,
    )
    root = APMPackage(name="consumer", version="1.0.0", replace_rules=rules)
    tree = DependencyTree(root_package=root)
    direct = DependencyNode(
        package=APMPackage(name="used", version="1.0.0"), dependency_ref=selected, depth=1
    )
    child = DependencyNode(
        package=APMPackage(name="child", version="1.0.0"),
        dependency_ref=DependencyReference.parse("example/child"),
        depth=2,
        parent=direct,
    )
    direct.children.append(child)
    tree.add_node(direct)
    tree.add_node(child)
    logger = InstallLogger(verbose=verbose)
    context = InstallContext(
        project_root=tmp_path, apm_dir=tmp_path, apm_package=root, logger=logger
    )
    events = Mock()
    with (
        patch.object(logger, "verbose_detail", wraps=logger.verbose_detail) as detail,
        patch.object(logger.diagnostics, "warn", wraps=logger.diagnostics.warn) as warning,
        patch.object(logger, "progress", wraps=logger.progress) as progress,
    ):
        events.attach_mock(detail, "detail")
        events.attach_mock(warning, "warning")
        events.attach_mock(progress, "progress")
        report_resolved_dependencies(context, resolver, [selected, child.dependency_ref], tree)

    assert [call[0] for call in events.mock_calls] == ["detail", "detail", "warning", "progress"]
    detail.assert_any_call("Resolved dependency tree: 1 direct + 1 transitive deps (max depth 2)")
    warning.assert_called_once_with(
        "Unused replacement rule for example/dormant. "
        "Check the exact host and package path, or remove the rule from apm.yml."
    )
    progress.assert_called_once_with(
        f"Replacement: {selected.ownership_reference.to_display_reference()} "
        f"-> {selected.source_reference.to_display_reference()}"
    )
    output = capsys.readouterr().out
    assert "Replacement:" in output
    assert ("Resolved dependency tree" in output) is verbose
    assert "Unused replacement rule" not in output
    logger.render_summary()
    assert "Unused replacement rule" in capsys.readouterr().out
    assert not (tmp_path.parent / "never-accessed").exists()


@pytest.mark.parametrize(
    "source", [{"path": "../local"}, {"git": "https://gitlab.com/team/fork", "ref": "patched"}]
)
def test_materialization_owner_preserves_original_slot_for_bound_references(
    tmp_path: Path, source: dict
) -> None:
    selected, _, _ = _locked(tmp_path, source)
    modules = tmp_path / "apm_modules"
    assert build_materialization_path(selected, modules) == modules / "MixedOrg" / "MixedRepo"


def test_local_replacement_outdated_is_not_remotely_comparable(tmp_path: Path) -> None:
    _, locked, _ = _locked(tmp_path, {"path": "../local"})
    assert locked.is_local_source is True
    downloader = Mock()
    row = _check_one_dep(locked, downloader, False)
    assert row.package == "mixedorg/mixedrepo"
    assert row.status == "not-comparable"
    assert "../local" in row.source
    assert downloader.mock_calls == []


@pytest.mark.parametrize("count", [3, 4])
def test_replacement_uninstall_bounds_unique_unverifiable_manifest_names(
    tmp_path: Path, count: int
) -> None:
    keys = [f"owner/package-{i}" for i in range(count)]
    failures = tuple((key, "private diagnostic detail") for key in reversed(keys))
    reachability = ReachabilityResult(
        reachable=frozenset(), complete=False, unverifiable=failures + failures[:1]
    )
    lock = LockFile(replacements=[])
    with (
        patch("apm_cli.commands.uninstall.engine._read_survivor_direct_refs", return_value=[]),
        patch(
            "apm_cli.deps.reachability.compute_forward_reachable_keys",
            return_value=reachability,
        ),
        pytest.raises(ValueError) as refused,
    ):
        _compute_actual_orphans(
            lock,
            {"owner/orphan"},
            set(),
            tmp_path / "apm.yml",
            [],
            tmp_path / "apm_modules",
            Mock(),
        )

    names = ", ".join(keys[:3]) + (", ..." if count > 3 else "")
    assert str(refused.value) == (
        f"Cannot verify replacement dependency reachability ({count} manifest(s): {names}). "
        "Restore missing or invalid package manifests before uninstalling."
    )
    assert lock.dependencies == {}


def test_fork_outdated_queries_clean_selected_reference(tmp_path: Path) -> None:
    _, locked, _ = _locked(tmp_path, {"git": "https://gitlab.com/team/fork", "ref": "patched"})
    downloader = Mock()
    downloader.list_remote_refs.return_value = [
        RemoteRef(name="patched", ref_type=GitReferenceType.BRANCH, commit_sha="b" * 40)
    ]
    row = _check_one_dep(locked, downloader, False)
    queried = downloader.list_remote_refs.call_args.args[0]
    assert queried.repo_url == "team/fork"
    assert queried.host == "gitlab.com"
    assert queried.replacement is None
    assert row.package == "mixedorg/mixedrepo"
    assert row.status == "outdated"
    source = row.source.partition(": ")[2].split()[0]
    parsed = urlparse(source if "://" in source else "//" + source)
    assert (parsed.hostname, parsed.path, parsed.fragment) == (
        "gitlab.com",
        "/team/fork",
        "patched",
    )


def test_replacement_why_keeps_exact_diamond_parents_and_original_refs(tmp_path: Path) -> None:
    left = LockedDependency(repo_url="owner/parents", virtual_path="packages/left", is_virtual=True)
    right = LockedDependency(
        repo_url="owner/parents", virtual_path="packages/right", is_virtual=True
    )
    _, locked, lock = _locked(tmp_path, {"path": "../local"}, parent=left.get_unique_key())
    second = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo#other-request")
    locked.replacement.record(second, right.get_unique_key(), False)
    lock.add_dependency(left)
    lock.add_dependency(right)
    result = compute_why(lock, locked)
    assert not result.is_direct
    assert len(result.paths) == 2
    assert {
        (path.chain[0].child_key, path.chain[-1].child_key, path.chain[-1].constraint)
        for path in result.paths
    } == {
        (left.get_unique_key(), "mixedorg/mixedrepo", "upstream"),
        (right.get_unique_key(), "mixedorg/mixedrepo", "other-request"),
    }


def test_replacement_why_exposes_direct_and_transitive_requests(tmp_path: Path) -> None:
    _, locked, lock = _locked(tmp_path, {"path": "../local"})
    parent = LockedDependency(repo_url="owner/parent")
    indirect = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo#indirect")
    locked.replacement.record(indirect, parent.get_unique_key(), False)
    lock.add_dependency(parent)
    result = compute_why(lock, locked)
    assert result.is_direct
    assert len(result.paths) == 2
    human = _render_human(result)
    assert "-> ../local" in human
    assert "owner/parent" in human
    assert "constraint: indirect" in human
    structured = json.loads(_render_json(result))
    assert structured["package"]["replacement"] == locked.replacement.to_dict()
    assert len(structured["paths"]) == 2


def test_replacement_tree_keeps_direct_and_shared_incoming_edges(tmp_path: Path) -> None:
    _, locked, lock = _locked(tmp_path, {"path": "../local"})
    parent = LockedDependency(repo_url="owner/parent")
    indirect = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo#indirect")
    locked.replacement.record(indirect, parent.get_unique_key(), False)
    lock.add_dependency(parent)
    lock.write(tmp_path / "apm.lock.yaml")
    tree = _build_dep_tree(tmp_path)
    assert {dep.get_unique_key() for dep in tree["direct"]} == {
        "owner/parent",
        "mixedorg/mixedrepo",
    }
    assert [dep.get_unique_key() for dep in tree["children_map"]["owner/parent"]] == [
        "mixedorg/mixedrepo"
    ]
    assert tree["unresolved"] == []


def test_retained_request_alias_does_not_reassign_the_owned_slot(tmp_path: Path) -> None:
    selected, locked, _ = _locked(tmp_path, {"path": "../local"}, parent="owner/left")
    right = DependencyReference.parse_from_dict(
        {"git": "https://github.com/MixedOrg/MixedRepo", "alias": "different-slot", "ref": "right"}
    )
    locked.replacement.record(right, "owner/right", False)
    assert locked.replacement.retain_parents({"owner/right"})
    restored = LockedDependency.from_dict(locked.to_dict())
    assert [request.alias for request in restored.replacement.requests] == ["different-slot"]
    assert restored.to_dependency_ref().get_install_path(tmp_path) == selected.get_install_path(
        tmp_path
    )


def test_local_replacement_audit_reads_source_not_upstream_cache(tmp_path: Path) -> None:
    source = tmp_path.parent / f"{tmp_path.name}-local"
    source.mkdir()
    _, locked, lock = _locked(tmp_path, {"path": str(source)})
    assert (
        _materialize_install_path(locked, tmp_path, tmp_path / "scratch", True, lockfile=lock)
        == source
    )


def test_native_local_replacement_audit_uses_current_selected_source_bytes(tmp_path: Path) -> None:
    selected, locked, _ = _locked(tmp_path, {"path": "./selected"})
    source = tmp_path / "selected"
    source.mkdir()
    (source / "SKILL.md").write_text("new selected content\n", encoding="ascii")
    modules = tmp_path / "apm_modules"
    candidate = selected.get_install_path(modules)
    candidate.mkdir(parents=True)
    (candidate / "SKILL.md").write_text("old selected content\n", encoding="ascii")
    locked.content_hash = compute_package_hash(candidate)
    scratch = tmp_path / "scratch"
    replay = selected.get_install_path(scratch / "apm_modules")
    with pytest.raises(AgentPluginDeploymentBoundaryError, match="cache differs"):
        verify_native_plugin_cache(locked, source, replay, modules, scratch)
    (candidate / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes())
    verify_native_plugin_cache(locked, source, replay, modules, scratch)
    assert (replay / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


def test_missing_replacement_audit_source_is_not_a_soft_cache_miss(tmp_path: Path) -> None:
    _, locked, lock = _locked(tmp_path, {"path": "../missing"})
    with pytest.raises(LocalResolutionError, match=r"replacement.*missing"):
        _materialize_install_path(locked, tmp_path, tmp_path / "scratch", True, lockfile=lock)


def test_replacement_casing_migration_uses_original_owner(tmp_path: Path) -> None:
    selected, _, _ = _locked(tmp_path, {"path": "../local"})
    modules = tmp_path / "apm_modules"
    stale = modules / "mixedorg" / "mixedrepo"
    stale.mkdir(parents=True)
    (stale / "marker").write_text("owned", encoding="utf-8")
    staging = ResolutionStagingSession(modules)
    migrations = []
    try:
        destination = prepare_materialization_path(
            selected,
            modules,
            staging,
            on_migrate=lambda old, new: migrations.append((old, new)),
        )
        assert migrations == [(stale, modules / "MixedOrg" / "MixedRepo")]
        assert (destination / "marker").read_text(encoding="utf-8") == "owned"
    finally:
        staging.rollback()
    assert (stale / "marker").is_file()


@pytest.mark.parametrize(
    "source", [{"path": "../local"}, {"git": "https://github.com/team/fork", "ref": "patched"}]
)
def test_replacement_does_not_inherit_upstream_executable_approval(
    tmp_path: Path, source: dict
) -> None:
    selected, _, _ = _locked(tmp_path, source)
    package = SimpleNamespace(dependency_ref=selected)
    upstream = SimpleNamespace(dependency_ref=selected.ownership_reference)
    approval = {
        resolve_package_key(upstream, "upstream"): {"hooks": True, "bin": True, "mcp": True}
    }
    assert check_executable_approval("upstream", upstream, approval)[:3] == (True,) * 3
    assert check_executable_approval("mixedorg/mixedrepo", package, approval) == (False,) * 5


@pytest.mark.parametrize(
    "source", [{"path": "../local"}, {"git": "https://github.com/team/fork", "ref": "patched"}]
)
def test_dependency_policy_evaluates_the_selected_source(tmp_path: Path, source: dict) -> None:
    selected, _, _ = _locked(tmp_path, source)
    original_name = selected.ownership_reference.get_canonical_dependency_string()
    selected_name = selected.get_canonical_dependency_string()
    assert not _check_dependency_allowlist(
        [selected], DependencyPolicy(allow=[original_name])
    ).passed
    assert not _check_dependency_denylist([selected], DependencyPolicy(deny=[selected_name])).passed


def test_semver_source_change_discards_old_resolution_and_routes_effective_auth(
    tmp_path: Path,
) -> None:
    _, old, lock = _locked(tmp_path, {"git": "https://github.com/old/fork", "ref": "^1.0.0"})
    old.constraint = "^1.0.0"
    old.version = "1.0.0"
    old.resolved_ref = old.resolved_tag = "v1.0.0"
    selected, _, _ = _locked(tmp_path, {"git": "https://gitlab.com/new/fork", "ref": "^1.0.0"})
    auth = Mock()
    auth.uses_public_github_anonymous_first.return_value = False
    auth.resolve_for_dep.return_value = SimpleNamespace(
        token=None, auth_scheme="basic", host_info=SimpleNamespace(kind="gitlab")
    )
    auth.git_env_for_remote.return_value = {}
    fresh_resolution = object()
    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver"),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver") as resolver,
    ):
        resolver.return_value.resolve.return_value = fresh_resolution
        assert (
            maybe_resolve_git_semver(
                dep_ref=selected, existing_lockfile=lock, update_refs=False, auth_resolver=auth
            )
            is fresh_resolution
        )
    assert auth.resolve_for_dep.call_args_list
    for call in auth.resolve_for_dep.call_args_list:
        reference = call.args[0]
        assert (reference.host, reference.repo_url) == ("gitlab.com", "new/fork")
        assert reference.replacement is None


def test_update_auth_preflight_never_uses_original_source_coordinates(tmp_path: Path) -> None:
    selected, _, _ = _locked(tmp_path, {"git": "https://gitlab.com/new/fork", "ref": "patched"})
    auth = Mock()
    auth.resolve_for_dep.return_value = SimpleNamespace(
        token=None,
        auth_scheme="basic",
        source="anonymous",
        host_info=SimpleNamespace(display_name="gitlab.com"),
    )
    auth.git_env_for_remote.return_value = {}
    with (
        patch("apm_cli.deps.github_downloader.GitHubPackageDownloader") as downloader,
        patch("apm_cli.utils.git_env.git_remote_refs") as probe,
    ):
        downloader.return_value._build_repo_url.return_value = "https://gitlab.com/new/fork.git"
        probe.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        _preflight_auth_check(SimpleNamespace(deps_to_install=[selected]), auth, False)
        auth.resolve_for_dep.assert_called()
        reference = auth.resolve_for_dep.call_args.args[0]
        assert reference.replacement is None
        assert (reference.host, reference.repo_url) == ("gitlab.com", "new/fork")
        assert urlparse(probe.call_args.args[0]).hostname == "gitlab.com"
    local, _, _ = _locked(tmp_path, {"path": "../local"})
    unused_auth = Mock()
    _preflight_auth_check(SimpleNamespace(deps_to_install=[local]), unused_auth, False)
    assert unused_auth.mock_calls == []


@pytest.mark.parametrize("state", ["matching", "changed", "removed"])
def test_replacement_lock_seed_uses_only_matching_clean_selected_source(
    tmp_path: Path, state: str
) -> None:
    selected, _, lock = _locked(tmp_path, {"git": "https://gitlab.com/team/fork", "ref": "patched"})
    rules = [selected.replacement.rule]
    if state == "changed":
        other, _, _ = _locked(tmp_path, {"git": "https://gitlab.com/team/other", "ref": "patched"})
        rules = [other.replacement.rule]
    elif state == "removed":
        rules = []
    seed = Mock(return_value=True)
    context = SimpleNamespace(
        apm_package=SimpleNamespace(replace_rules=rules),
        existing_lockfile=lock,
        ref_resolver=SimpleNamespace(seed=seed),
        ref_freshness_policy=None,
        update_refs=False,
        refresh=False,
        logger=None,
    )
    seed_ref_resolver_from_lockfile(context)
    if state == "matching":
        seed.assert_called_once()
        reference, ref, sha = seed.call_args.args
        assert reference.replacement is None
        assert (reference.host, reference.repo_url, ref, sha) == (
            "gitlab.com",
            "team/fork",
            "patched",
            "a" * 40,
        )
    else:
        seed.assert_not_called()


@pytest.mark.parametrize("source_kind", ["local", "fork"])
def test_install_policy_gate_blocks_selected_source_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    import os

    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.models.manifest_contract import SOURCE_REPLACEMENTS_V1_SCHEMA_URI
    from apm_cli.policy.discovery import PolicyFetchResult
    from apm_cli.policy.schema import ApmPolicy
    from apm_cli.utils.yaml_io import dump_yaml
    from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
    from tests.utils.local_git_repository import LocalGitRepositoryFactory
    from tests.utils.local_package import LocalPackageFactory

    isolated = IsolatedApmEnvironment.create(tmp_path / "isolation", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_POLICY_DISABLE"] = "0"
    factory = LocalPackageFactory(tmp_path / "sources")
    package = factory.create("selected")
    factory.add_instruction(package, "selected", "---\napplyTo: '**'\n---\n# SELECTED\n")
    source = {"path": "./sources/selected"}
    if source_kind == "fork":
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        repository = repositories.create("fork", source_tree=package.root)
        sha = repositories.commit(repository, message="Selected fixture").sha
        environment = repositories.url_rewrite_subprocess_env_many(
            ((repository, "https://github.com/team/fork"),)
        )
        source = {"git": "https://github.com/team/fork", "ref": sha}
    selected, _, _ = _locked(tmp_path, source)
    manifest = tmp_path / "apm.yml"
    dump_yaml(
        {
            "name": "policy-consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "$schema": SOURCE_REPLACEMENTS_V1_SCHEMA_URI,
            "dependencies": {"apm": [{"git": "https://github.com/MixedOrg/MixedRepo"}]},
            "replace": [replacement_rule_to_dict(selected.replacement.rule)],
        },
        manifest,
    )
    before = manifest.read_bytes()
    monkeypatch.chdir(tmp_path)
    policy = ApmPolicy(
        enforcement="block",
        dependencies=DependencyPolicy(
            allow=[selected.ownership_reference.get_canonical_dependency_string()]
        ),
    )
    fetch = PolicyFetchResult(
        policy=policy,
        source="org:fixture/.github",
        cached=False,
        error=None,
        cache_age_seconds=None,
        cache_stale=False,
        fetch_error=None,
        outcome="found",
    )
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=fetch),
        patch("apm_cli.policy.install_preflight.discover_policy_with_chain", return_value=fetch),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
    ):
        result = CliRunner().invoke(cli, ["install", "--target", "copilot"])
    assert result.exit_code != 0, result.output
    assert "blocked by org policy" in result.output.lower()
    assert manifest.read_bytes() == before
    assert not (tmp_path / "apm.lock.yaml").exists()
    assert not list((tmp_path / "apm_modules").rglob("apm.yml"))
    assert not (tmp_path / ".github/instructions/selected.instructions.md").exists()
