"""Owner tests: the commit a same-repository ``path:`` child is fetched at (#5525)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.same_repo_pin import same_repo_child_reference
from apm_cli.models.apm_package import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-source.ref_cache_coherent")]

FETCHED = "a" * 40
LOCKED = "b" * 40
REFRESHED = "c" * 40


def _parent(reference: str | None = None, commit: str | None = None) -> DependencyReference:
    dep = DependencyReference(
        repo_url="acme/agents-marketplace",
        host="gitlab.com",
        reference=reference,
        virtual_path="plugins/guild",
        is_virtual=True,
    )
    if commit is not None:
        dep.resolved_reference = ResolvedReference(
            original_ref=reference or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name=reference or "default",
        )
    return dep


def _lock(parent: DependencyReference, *, resolved_ref=None, commit=LOCKED) -> LockFile:
    lock = LockFile()
    row = LockedDependency.from_dependency_ref(parent, commit, 1, None)
    row.resolved_ref = resolved_ref
    lock.add_dependency(row)
    return lock


def test_the_commit_the_parent_was_fetched_at_wins() -> None:
    parent = _parent(commit=FETCHED.upper())
    assert same_repo_child_reference(parent, _lock(parent)) == FETCHED


def test_a_cached_parent_resolves_at_its_lock_row_commit() -> None:
    parent = _parent()
    assert same_repo_child_reference(parent, _lock(parent)) == LOCKED


@pytest.mark.parametrize(
    ("declared", "locked_ref", "locked_commit"),
    [
        ("v2", "v1", LOCKED),  # the selector changed since the lock
        (None, None, None),  # the parent row has no commit
        (None, None, "cached"),  # a sentinel is not a commit
    ],
)
def test_without_a_known_parent_commit_the_child_keeps_the_selector(
    declared, locked_ref, locked_commit
) -> None:
    parent = _parent(reference=declared, commit="unknown")
    lock = _lock(parent, resolved_ref=locked_ref, commit=locked_commit)
    assert same_repo_child_reference(parent, lock) == declared
    assert same_repo_child_reference(parent, None) == declared


def test_an_update_resolves_the_parent_now_and_records_it_on_the_parent() -> None:
    parent = _parent(reference="main")
    calls: list[DependencyReference] = []

    def refresh(dep: DependencyReference) -> ResolvedReference:
        calls.append(dep)
        return ResolvedReference("main", GitReferenceType.BRANCH, REFRESHED, "main")

    lock = _lock(parent, resolved_ref="main")
    assert same_repo_child_reference(parent, lock, refresh=refresh) == REFRESHED
    # The next child of the same parent reuses the recorded commit.
    assert same_repo_child_reference(parent, lock, refresh=refresh) == REFRESHED
    assert calls == [parent]


def test_an_update_never_falls_back_to_the_stale_lock_commit() -> None:
    parent = _parent()

    def unreachable(dep: DependencyReference) -> ResolvedReference:
        raise RuntimeError("origin unreachable")

    assert same_repo_child_reference(parent, _lock(parent), refresh=unreachable) is None
    assert getattr(parent, "resolved_reference", None) is None


def test_an_update_keeps_a_commit_pinned_parent_without_asking_the_origin() -> None:
    parent = _parent(reference=FETCHED)

    def refresh(dep: DependencyReference) -> ResolvedReference:
        raise AssertionError("a pinned parent needs no ref resolution")

    assert same_repo_child_reference(parent, None, refresh=refresh) == FETCHED


def test_a_cached_semver_parent_resolves_at_its_lock_row_commit() -> None:
    parent = _parent(reference="^1.0.0")
    lock = _lock(parent, resolved_ref="v1.2.0")
    lock.get_dependency(parent.get_unique_key()).constraint = "^1.0.0"
    assert same_repo_child_reference(parent, lock) == LOCKED


def test_a_parent_that_dropped_its_ref_ignores_the_locked_branch_commit() -> None:
    parent = _parent()
    assert same_repo_child_reference(parent, _lock(parent, resolved_ref="main")) is None


COMMIT = "0123456789abcdef0123456789abcdef01234567"
PREFIX = "gitlab.com/acme/agents-marketplace/plugins/"


def _resolve(tmp_path: Path, manifests: dict[str, str]):
    """Resolve a consumer of ``plugins/guild``; the plugin is fetched at COMMIT."""
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    (project_root / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - git: https://gitlab.com/acme/agents-marketplace.git\n"
        "      path: plugins/guild\n"
    )
    fetched: list[str | None] = []

    def download_callback(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
        fetched.append(dep_ref.reference)
        install_path = dep_ref.get_install_path(apm_modules_dir)
        install_path.mkdir(parents=True, exist_ok=True)
        if dep_ref.virtual_path == "plugins/guild":
            dep_ref.resolved_reference = ResolvedReference(
                "default", GitReferenceType.BRANCH, COMMIT, "default"
            )
        body = manifests.get(dep_ref.virtual_path, "")
        (install_path / "apm.yml").write_text(f"name: p\nversion: 1.0.0\n{body}")
        return install_path

    resolver = APMDependencyResolver(
        apm_modules_dir=project_root / "apm_modules",
        download_callback=download_callback,
        max_parallel=1,
    )
    return resolver.resolve_dependencies(project_root), fetched


def test_a_self_listed_file_is_fetched_at_the_plugin_commit(tmp_path: Path) -> None:
    manifests = {"plugins/guild": "dependencies:\n  apm:\n    - ./prompts/review.prompt.md\n"}
    graph, fetched = _resolve(tmp_path, manifests)

    child = graph.dependency_tree.get_node(PREFIX + "guild/prompts/review.prompt.md")
    assert child is not None
    assert child.dependency_ref.reference == COMMIT
    # An inherited commit is not a pin the user declared (immutable checks skip it).
    assert child.dependency_ref.pinned_by_parent
    assert fetched == [None, COMMIT]


def test_the_inherited_marker_survives_nested_same_repo_children(tmp_path: Path) -> None:
    manifests = {
        "plugins/guild": "dependencies:\n  apm:\n    - ./sub\n",
        "plugins/guild/sub": (
            "dependencies:\n  apm:\n    - ./x.prompt.md\n"
            "    - git: parent\n      path: plugins/shared\n"
        ),
    }
    graph, _ = _resolve(tmp_path, manifests)

    for path in ("guild/sub", "guild/sub/x.prompt.md", "shared"):
        node = graph.dependency_tree.get_node(PREFIX + path)
        assert node is not None, path
        assert node.dependency_ref.reference == COMMIT, path
        assert node.dependency_ref.pinned_by_parent, path
