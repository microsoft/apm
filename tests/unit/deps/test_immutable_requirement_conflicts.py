"""Regression traps for immutable requirements lost by single-version hoisting."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef, ResolvedReference

pytestmark = pytest.mark.component

OLD = "a" * 40
NEW = "b" * 40


def _manifest(path: Path, name: str, dependencies: list[str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "apm.yml").write_text(
        yaml.safe_dump({"name": name, "version": "1.0.0", "dependencies": {"apm": dependencies}}),
        encoding="utf-8",
    )


def _project(path: Path, first: str, second: str, *, transitive: bool = False) -> None:
    modules = path / "apm_modules" / "org"
    _manifest(modules / "shared", "shared", [])
    _manifest(modules / "parent", "parent", [f"org/shared#{second}"])
    _manifest(modules / "other", "other", [f"org/shared#{first}"])
    _manifest(
        path,
        "consumer",
        ["org/other" if transitive else f"org/shared#{first}", "org/parent"],
    )


@pytest.mark.parametrize("transitive", [False, True], ids=["direct-transitive", "two-transitive"])
@pytest.mark.parametrize("parallel", [1, 4])
def test_conflicting_commit_requirements_fail_with_both_chains(
    tmp_path: Path, transitive: bool, parallel: int
) -> None:
    _project(tmp_path, OLD, NEW, transitive=transitive)

    graph = APMDependencyResolver(max_parallel=parallel).resolve_dependencies(tmp_path)

    assert not graph.is_valid()
    diagnostic = "\n".join(graph.resolution_errors)
    first_chain = "consumer -> org/other@HEAD -> " if transitive else "consumer -> "
    assert f"{first_chain}org/shared@{OLD}" in diagnostic
    assert f"consumer -> org/parent@HEAD -> org/shared@{NEW}" in diagnostic
    assert "incompatible immutable requirements" in diagnostic
    assert "Align" in diagnostic
    assert "--frozen" not in diagnostic


@pytest.mark.parametrize("same_commit", [False, True], ids=["conflict", "equivalent-tags"])
def test_tag_spelling_is_not_conflict_evidence(tmp_path: Path, same_commit: bool) -> None:
    _project(tmp_path, "v1", "shared-release")
    refs = Mock()
    refs.list_remote_refs.return_value = [
        RemoteRef("v1", GitReferenceType.TAG, OLD),
        RemoteRef("shared-release", GitReferenceType.TAG, OLD if same_commit else NEW),
    ]

    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)

    assert graph.is_valid() is same_commit
    assert refs.list_remote_refs.call_count == 1


def test_identical_requirements_need_no_remote_check(tmp_path: Path) -> None:
    _project(tmp_path, "v1", "v1")
    refs = Mock()
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert graph.is_valid()
    refs.list_remote_refs.assert_not_called()
    refs.resolve_git_reference.assert_not_called()


@pytest.mark.parametrize("locked_commit", [OLD, NEW], ids=["collapsed-lock", "valid-lock"])
def test_frozen_transitive_requirement_must_match_locked_commit(
    tmp_path: Path, locked_commit: str
) -> None:
    _project(tmp_path, OLD, NEW)
    _manifest(tmp_path, "consumer", ["org/parent"])
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="org/parent", resolved_commit="c" * 40))
    lock.add_dependency(
        LockedDependency(repo_url="org/shared", resolved_ref=OLD, resolved_commit=locked_commit)
    )

    graph = APMDependencyResolver(existing_lockfile=lock, frozen=True).resolve_dependencies(
        tmp_path
    )

    assert graph.is_valid() is (locked_commit == NEW)
    if locked_commit != NEW:
        assert "apm.lock.yaml" in graph.resolution_errors[0]
        assert f"consumer -> org/parent@HEAD -> org/shared@{NEW}" in graph.resolution_errors[0]


@pytest.mark.parametrize("second", ["release", OLD[:8]], ids=["tag-sha", "short-full-sha"])
def test_equivalent_tag_or_short_sha_is_accepted(tmp_path: Path, second: str) -> None:
    _project(tmp_path, OLD, second)
    refs = Mock()
    refs.list_remote_refs.return_value = [RemoteRef("release", GitReferenceType.TAG, OLD)]
    refs.resolve_git_reference.return_value = ResolvedReference(
        second, GitReferenceType.COMMIT, OLD, second
    )
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert graph.is_valid()


def test_unverifiable_names_fail_without_claiming_a_proven_conflict(tmp_path: Path) -> None:
    _project(tmp_path, "release-one", "release-two")
    refs = Mock()
    refs.list_remote_refs.side_effect = RuntimeError("remote unavailable")
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert not graph.is_valid()
    assert "Cannot verify immutable requirements" in graph.resolution_errors[0]
    assert "incompatible immutable requirements" not in graph.resolution_errors[0]


def test_branch_collision_does_not_hide_two_incompatible_pins(tmp_path: Path) -> None:
    _project(tmp_path, OLD, NEW, transitive=True)
    _manifest(tmp_path, "consumer", ["org/shared#main", "org/other", "org/parent"])
    refs = Mock()
    refs.list_remote_refs.return_value = [RemoteRef("main", GitReferenceType.BRANCH, OLD)]
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert not graph.is_valid()
    assert "incompatible immutable requirements" in graph.resolution_errors[0]


def test_compatible_semver_requirement_keeps_existing_resolution_policy(tmp_path: Path) -> None:
    _project(tmp_path, "^1.0.0", "v1.2.0")
    refs = Mock()
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert graph.is_valid()
    refs.list_remote_refs.assert_not_called()


def test_different_virtual_packages_are_not_one_identity(tmp_path: Path) -> None:
    _manifest(tmp_path, "consumer", [f"org/mono/one#{OLD}", f"org/mono/two#{NEW}"])
    graph = APMDependencyResolver().resolve_dependencies(tmp_path)
    assert graph.is_valid()
    assert graph.flattened_dependencies.total_dependencies() == 2


def test_frozen_conflict_preserves_both_root_chains(tmp_path: Path) -> None:
    _project(tmp_path, OLD, NEW)
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="org/shared", resolved_ref=OLD, resolved_commit=OLD)
    )
    graph = APMDependencyResolver(existing_lockfile=lock, frozen=True).resolve_dependencies(
        tmp_path
    )
    diagnostic = "\n".join(graph.resolution_errors)
    assert not graph.is_valid()
    assert f"consumer -> org/shared@{OLD}" in diagnostic
    assert f"consumer -> org/parent@HEAD -> org/shared@{NEW}" in diagnostic


@pytest.mark.parametrize("compatible", [False, True], ids=["wrong-pin", "valid-pin"])
@pytest.mark.parametrize("length", [7, 8, 39])
def test_frozen_short_sha_validates_locked_prefix_without_discovery(
    tmp_path: Path, compatible: bool, length: int
) -> None:
    short = OLD[:length]
    _project(tmp_path, short, short)
    _manifest(tmp_path, "consumer", ["org/parent"])
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="org/parent", resolved_commit="c" * 40))
    lock.add_dependency(
        LockedDependency(
            repo_url="org/shared", resolved_ref=short, resolved_commit=OLD if compatible else NEW
        )
    )
    refs = Mock()
    refs.list_remote_refs.side_effect = AssertionError("Locked replay must not discover refs")
    refs.resolve_git_reference.side_effect = AssertionError("Do not trust a lock-seeded lookup")

    graph = APMDependencyResolver(
        existing_lockfile=lock, frozen=True, reference_resolver=refs
    ).resolve_dependencies(tmp_path)

    assert graph.is_valid() is compatible
    if not compatible:
        diagnostic = "\n".join(graph.resolution_errors)
        assert "incompatible immutable requirements" in diagnostic
        assert f"consumer -> org/parent@HEAD -> org/shared@{short}" in diagnostic
        assert "apm.lock.yaml" in diagnostic
    refs.list_remote_refs.assert_not_called()
    refs.resolve_git_reference.assert_not_called()


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("name", ["release", "main"])
def test_unchanged_locked_literal_replays_offline_with_equivalent_sha(
    tmp_path: Path, frozen: bool, name: str
) -> None:
    _project(tmp_path, name, OLD)
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="org/parent", resolved_commit="c" * 40))
    lock.add_dependency(
        LockedDependency(repo_url="org/shared", resolved_ref=name, resolved_commit=OLD)
    )
    refs = Mock()
    refs.list_remote_refs.side_effect = RuntimeError("Remote ref no longer exists")

    graph = APMDependencyResolver(
        existing_lockfile=lock, frozen=frozen, reference_resolver=refs
    ).resolve_dependencies(tmp_path)

    assert graph.is_valid(), graph.resolution_errors
    refs.list_remote_refs.assert_not_called()


@pytest.mark.parametrize("update_refs", [False, True], ids=["replay", "refresh"])
def test_moved_tag_uses_locked_content_until_explicit_refresh(
    tmp_path: Path, update_refs: bool
) -> None:
    _project(tmp_path, "release", OLD)
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="org/shared", resolved_ref="release", resolved_commit=OLD)
    )
    refs = Mock()
    refs.list_remote_refs.return_value = [RemoteRef("release", GitReferenceType.TAG, NEW)]

    graph = APMDependencyResolver(
        existing_lockfile=lock, update_refs=update_refs, reference_resolver=refs
    ).resolve_dependencies(tmp_path)

    assert graph.is_valid() is (not update_refs)
    assert refs.list_remote_refs.call_count == int(update_refs)


@pytest.mark.parametrize("explicit_tag", [False, True])
def test_branch_tag_collision_preserves_existing_reference_policy(
    tmp_path: Path, explicit_tag: bool
) -> None:
    _project(tmp_path, "refs/tags/v1.0" if explicit_tag else "v1.0", NEW)
    refs = Mock()
    refs.list_remote_refs.return_value = [
        RemoteRef("v1.0", GitReferenceType.BRANCH, NEW),
        RemoteRef("v1.0", GitReferenceType.TAG, OLD),
    ]
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    assert graph.is_valid() is (not explicit_tag)


def test_verification_failure_names_both_chains_without_transport_secrets(tmp_path: Path) -> None:
    _project(tmp_path, "release-one", "release-two")
    refs = Mock()
    refs.list_remote_refs.side_effect = RuntimeError("Authorization: Bearer secret-value")
    graph = APMDependencyResolver(reference_resolver=refs).resolve_dependencies(tmp_path)
    diagnostic = "\n".join(graph.resolution_errors)
    assert not graph.is_valid()
    assert "consumer -> org/shared@release-one" in diagnostic
    assert "consumer -> org/parent@HEAD -> org/shared@release-two" in diagnostic
    assert "Cannot verify immutable requirements" in diagnostic
    assert "Authorization" not in diagnostic
    assert "secret-value" not in diagnostic
