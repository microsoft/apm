"""Dependency identity and materialization casing contracts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.dependency_graph import FlatDependencyMap
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.dependency.materialization import (
    CachedMaterializationPathReader,
    MaterializationPathCollisionError,
    find_case_equivalent_materialization_path,
    prepare_materialization_path,
)
from apm_cli.models.dependency.reference import DependencyReference


@pytest.mark.parametrize(
    "raw",
    (
        "https://github.com/MixedOrg/MixedRepo.git",
        "git@github.com:MixedOrg/MixedRepo.git",
        "ssh://git@github.com/MixedOrg/MixedRepo.git",
    ),
)
def test_github_url_spellings_separate_identity_from_materialization(
    raw: str,
    tmp_path: Path,
) -> None:
    """HTTPS and SSH spellings share identity without lowering disk paths."""
    reference = DependencyReference.parse(raw)

    assert reference.repo_url == "MixedOrg/MixedRepo"
    assert reference.canonical_repo_url == "mixedorg/mixedrepo"
    assert reference.get_unique_key() == "mixedorg/mixedrepo"
    assert reference.get_identity() == "mixedorg/mixedrepo"
    assert reference.get_install_path(tmp_path / "apm_modules") == (
        tmp_path / "apm_modules" / "MixedOrg" / "MixedRepo"
    )


def test_nested_virtual_path_preserves_every_source_path_segment(tmp_path: Path) -> None:
    """Repository and in-repository path casing survive materialization."""
    reference = DependencyReference.parse_from_dict(
        {
            "git": "https://github.com/MixedOrg/MixedRepo.git",
            "path": "Nested/Rules/Review",
        }
    )

    assert reference.get_unique_key() == "mixedorg/mixedrepo/Nested/Rules/Review"
    assert reference.get_install_path(tmp_path / "apm_modules") == (
        tmp_path / "apm_modules" / "MixedOrg" / "MixedRepo" / "Nested" / "Rules" / "Review"
    )


def test_host_case_and_repo_case_dedupe_without_replacing_first_display_spelling() -> None:
    """Host-case variants have one key while first source casing remains visible."""
    first = DependencyReference(
        repo_url="MixedOrg/MixedRepo",
        host="GitHub.COM",
    )
    duplicate = DependencyReference(
        repo_url="mixedorg/mixedrepo",
        host="github.com",
    )

    assert first.get_unique_key() == duplicate.get_unique_key() == "mixedorg/mixedrepo"
    assert first.repo_url == "MixedOrg/MixedRepo"
    assert duplicate.repo_url == "mixedorg/mixedrepo"


def test_flat_dependency_map_dedupes_case_variants_by_canonical_key() -> None:
    """Graph flattening cannot create two packages from presentation casing."""
    dependencies = FlatDependencyMap()
    first = DependencyReference(repo_url="MixedOrg/MixedRepo", host="GitHub.COM")
    duplicate = DependencyReference(repo_url="mixedorg/mixedrepo", host="github.com")

    dependencies.add_dependency(first)
    dependencies.add_dependency(duplicate)

    assert dependencies.total_dependencies() == 1
    assert dependencies.get_installation_list() == [first]


def test_dependency_tree_lookup_accepts_source_and_canonical_spelling() -> None:
    """Legacy O(1) lookup remains useful after repo_url becomes source-cased."""
    from unittest.mock import Mock

    from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree

    reference = DependencyReference(repo_url="MixedOrg/MixedRepo", host="github.com")
    tree = DependencyTree(root_package=Mock())
    tree.add_node(DependencyNode(package=Mock(), dependency_ref=reference))

    assert tree.has_dependency("MixedOrg/MixedRepo")
    assert tree.has_dependency("mixedorg/mixedrepo")


def test_lockfile_keeps_canonical_identity_and_materialization_spelling() -> None:
    """Lock serialization can replay both identity and the actual disk path."""
    reference = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo.git")
    locked = LockedDependency.from_dependency_ref(
        reference,
        resolved_commit="a" * 40,
        depth=1,
        resolved_by=None,
    )
    payload = locked.to_dict()
    restored = LockedDependency.from_dict(payload)

    assert payload["repo_url"] == "mixedorg/mixedrepo"
    assert payload["materialization_repo_url"] == "MixedOrg/MixedRepo"
    assert restored.get_unique_key() == "mixedorg/mixedrepo"
    assert restored.to_dependency_ref().repo_url == "MixedOrg/MixedRepo"


def test_legacy_mixed_case_lock_entry_migrates_without_losing_display_spelling() -> None:
    """Pre-0.25 mixed-case lock rows gain a separate materialization spelling."""
    restored = LockedDependency.from_dict(
        {
            "repo_url": "MixedOrg/MixedRepo",
            "host": "github.com",
            "resolved_commit": "b" * 40,
        }
    )

    assert restored.repo_url == "mixedorg/mixedrepo"
    assert restored.materialization_repo_url == "MixedOrg/MixedRepo"
    assert restored.get_unique_key() == "mixedorg/mixedrepo"


def test_lock_rejects_materialization_spelling_for_a_different_identity() -> None:
    """A tampered display path cannot redirect a canonical lock identity."""
    with pytest.raises(ValueError, match="materialization_repo_url"):
        LockedDependency(
            repo_url="mixedorg/mixedrepo",
            host="github.com",
            materialization_repo_url="OtherOrg/OtherRepo",
        )


@pytest.mark.windows_compat
def test_stale_lowercase_materialization_moves_transactionally(tmp_path: Path) -> None:
    """A stale generated path is reused once and rolls back with the install."""
    modules = tmp_path / "apm_modules"
    stale = modules / "mixedorg" / "mixedrepo"
    stale.mkdir(parents=True)
    (stale / "marker.txt").write_text("owned", encoding="utf-8")
    reference = DependencyReference.parse("MixedOrg/MixedRepo")
    staging = ResolutionStagingSession(modules)
    migrations: list[tuple[Path, Path]] = []

    selected = prepare_materialization_path(
        reference,
        modules,
        staging,
        on_migrate=lambda source, destination: migrations.append((source, destination)),
    )

    assert selected == modules / "MixedOrg" / "MixedRepo"
    assert migrations == [(stale, selected)]
    assert (selected / "marker.txt").read_text(encoding="utf-8") == "owned"
    owner_entries = [path for path in modules.iterdir() if path.name.casefold() == "mixedorg"]
    assert [path.name for path in owner_entries] == ["MixedOrg"]
    package_entries = [
        path for path in owner_entries[0].iterdir() if path.name.casefold() == "mixedrepo"
    ]
    assert [path.name for path in package_entries] == ["MixedRepo"]
    matches = [
        path
        for owner in modules.iterdir()
        if owner.is_dir() and owner.name.casefold() == "mixedorg"
        for path in owner.iterdir()
        if path.is_dir() and path.name.casefold() == "mixedrepo"
    ]
    assert len(matches) == 1

    staging.rollback()
    assert (stale / "marker.txt").read_text(encoding="utf-8") == "owned"
    assert [path.name for path in modules.iterdir()] == ["mixedorg"]


def test_cached_reader_reuses_directory_listings(tmp_path: Path) -> None:
    """One resolution attempt does not rescan the same owner for every dep."""

    class CountingReader:
        def __init__(self) -> None:
            self.scans: dict[Path, int] = {}

        @staticmethod
        def exists(path: Path) -> bool:
            return path.exists()

        @staticmethod
        def is_dir(path: Path) -> bool:
            return path.is_dir()

        def iterdir(self, path: Path):
            key = path.absolute()
            self.scans[key] = self.scans.get(key, 0) + 1
            return path.iterdir()

        @staticmethod
        def samefile(left: Path, right: Path) -> bool:
            return left.samefile(right)

        def invalidate(self, *paths: Path) -> None:
            return None

    modules = tmp_path / "apm_modules"
    (modules / "MixedOrg" / "RepoA").mkdir(parents=True)
    (modules / "MixedOrg" / "RepoB").mkdir()
    delegate = CountingReader()
    reader = CachedMaterializationPathReader(delegate)

    for repo in ("RepoA", "RepoB"):
        dependency = DependencyReference.parse(f"MixedOrg/{repo}")
        desired = modules / "MixedOrg" / repo
        assert (
            find_case_equivalent_materialization_path(
                desired,
                modules,
                dependency=dependency,
                reader=reader,
            )
            == desired
        )

    assert delegate.scans == {
        modules.absolute(): 1,
        (modules / "MixedOrg").absolute(): 1,
    }


def test_case_variant_lookup_fails_closed_on_distinct_collisions(tmp_path: Path) -> None:
    """Case-sensitive filesystems reject two physical paths for one identity."""
    modules = tmp_path / "apm_modules"
    desired = modules / "MixedOrg" / "MixedRepo"
    candidates = (
        PurePosixPath("MixedOrg/MixedRepo"),
        PurePosixPath("mixedorg/mixedrepo"),
    )
    dependency = DependencyReference.parse("MixedOrg/MixedRepo")

    with pytest.raises(MaterializationPathCollisionError, match="multiple"):
        find_case_equivalent_materialization_path(
            desired,
            modules,
            dependency=dependency,
            candidate_relatives=candidates,
        )


def test_virtual_subpath_casing_remains_identity_significant(tmp_path: Path) -> None:
    """Casefolding stops at the repository boundary for nested packages."""
    modules = tmp_path / "apm_modules"
    dependency = DependencyReference.parse_from_dict(
        {
            "git": "https://github.com/MixedOrg/MixedRepo.git",
            "path": "Nested/Rules",
        }
    )
    desired = dependency.get_install_path(modules)

    assert (
        find_case_equivalent_materialization_path(
            desired,
            modules,
            dependency=dependency,
            candidate_relatives=(PurePosixPath("mixedorg/mixedrepo/Nested/rules"),),
        )
        is None
    )


def test_virtual_file_leaf_casing_remains_identity_significant(tmp_path: Path) -> None:
    """Flattened virtual-file names fold only their repository-name prefix."""
    modules = tmp_path / "apm_modules"
    dependency = DependencyReference.parse_from_dict(
        {
            "git": "https://github.com/MixedOrg/MixedRepo.git",
            "path": "Prompts/Review.prompt.md",
        }
    )
    desired = dependency.get_install_path(modules)
    correct = PurePosixPath("mixedorg/mixedrepo-Review")

    assert find_case_equivalent_materialization_path(
        desired,
        modules,
        dependency=dependency,
        candidate_relatives=(
            PurePosixPath("mixedorg/mixedrepo-review"),
            correct,
        ),
    ) == modules.joinpath(*correct.parts)
