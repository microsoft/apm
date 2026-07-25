"""Direct unit tests for the forward-reachability canonical owner.

Covers :func:`apm_cli.deps.reachability.compute_forward_reachable_keys` in
isolation from the uninstall engine: empty lockfile, no-candidate fast
path, local diamond, remote diamond, deeper chain, cycle safety, each of
the fail-closed causes (missing manifest, malformed manifest, remote
path-escape via symlink, corrupt local anchor chain), and the
``reachable_via`` repair-data-source contract (which parent/local_path a
rescue records, and that it is empty when nothing was rescued).
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.reachability import compute_forward_reachable_keys
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.models.dependency import DependencyReference


@pytest.fixture(autouse=True)
def _isolated_apm_yml_cache():
    """``APMPackage.from_apm_yml`` caches by resolved path; each test uses
    a fresh ``tmp_path`` so cross-test collisions can't happen, but a
    single test that mutates a manifest mid-test must clear explicitly.
    """
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _shared_key(lockfile: LockFile, repo_url: str) -> str:
    for key, dep in lockfile.dependencies.items():
        if dep.repo_url == repo_url:
            return key
    raise AssertionError(f"no lockfile entry for {repo_url!r}")


def test_empty_lockfile_returns_complete_empty_result(tmp_path):
    lockfile = LockFile()
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, tmp_path / "apm_modules", [], frozenset()
    )
    assert result.complete is True
    assert result.reachable == frozenset()
    assert result.unverifiable == ()


def test_no_candidate_orphans_returns_empty_reachable_set(tmp_path):
    """With no candidate orphans to rescue, the walk still visits the
    survivor (matching production, where the engine only *calls*
    ``compute_forward_reachable_keys`` when candidates are non-empty --
    that gating lives in ``_compute_actual_orphans``, not here) but
    ``reachable`` is trivially empty since nothing was being looked for.
    """
    apm_modules_dir = tmp_path / "apm_modules"
    pkg_a_dir = apm_modules_dir / "acme" / "pkg-a"
    pkg_a_dir.mkdir(parents=True)
    (pkg_a_dir / "apm.yml").write_text("name: pkg-a\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/pkg-a", depth=1))
    pkg_a_ref = DependencyReference.parse("acme/pkg-a")
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, apm_modules_dir, [pkg_a_ref], frozenset()
    )
    assert result.complete is True
    # Nothing was a candidate orphan, so nothing here matters to a caller
    # (candidate_orphans & reachable is always empty regardless of what
    # incidental nodes the walk happens to mark, e.g. the survivor itself).
    assert result.reachable & frozenset() == frozenset()
    assert result.unverifiable == ()


def test_local_diamond_shared_dep_is_reachable_via_surviving_parent(tmp_path):
    """root-a and root-b both declare ../shared; root-b survives -> shared
    remains forward-reachable even though its lockfile ``resolved_by``
    still points at root-a.

    ``root-a``/``root-b``/``shared`` are siblings of the project
    directory (as real local deps are, e.g. ``../shared``); the project
    directory itself is the anchor passed as ``project_root``.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (tmp_path / "root-b").mkdir()
    (tmp_path / "root-b" / "apm.yml").write_text(
        "name: root-b\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../shared\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "apm.yml").write_text("name: shared\nversion: 1.0.0\n")

    lockfile = LockFile()
    # root-a's own lockfile entry is still present at this point in a real
    # uninstall (dict deletion happens later than the rescue check -- see
    # the module docstring's "call-order safety" note); ``shared``'s
    # ``resolved_by`` still points at it.
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-b", source="local", local_path="../root-b", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",  # first-wins parent, no longer a survivor
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    root_b_ref = DependencyReference.parse_from_dict({"path": "../root-b"})
    result = compute_forward_reachable_keys(
        lockfile, project_root, project_root / "apm_modules", [root_b_ref], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key in result.reachable
    assert result.unverifiable == ()


def test_local_diamond_reachable_via_records_rescuing_parent_and_local_path(tmp_path):
    """``reachable_via`` must record root-b (the surviving parent whose
    manifest edge led to shared) and its declared ``local_path`` -- the
    exact data ``engine._cleanup_transitive_orphans`` writes back into
    ``resolved_by``/``local_path`` to repair the stale first-wins anchor.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (tmp_path / "root-b").mkdir()
    (tmp_path / "root-b" / "apm.yml").write_text(
        "name: root-b\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../shared\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "apm.yml").write_text("name: shared\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-b", source="local", local_path="../root-b", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    root_b_ref = DependencyReference.parse_from_dict({"path": "../root-b"})
    result = compute_forward_reachable_keys(
        lockfile, project_root, project_root / "apm_modules", [root_b_ref], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key in result.reachable
    assert result.reachable_via[shared_key] == ("_local/root-b", "../shared")


def test_remote_diamond_reachable_via_records_rescuing_parent_with_no_local_path(tmp_path):
    """Remote matches have no anchored ``local_path`` concept -- the
    second tuple element must be ``None`` (only ``resolved_by`` needs
    repairing for these, never ``local_path``).
    """
    apm_modules_dir = tmp_path / "apm_modules"
    pkg_c_dir = apm_modules_dir / "acme" / "pkg-c"
    pkg_c_dir.mkdir(parents=True)
    (pkg_c_dir / "apm.yml").write_text(
        "name: pkg-c\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/shared-lib\n"
    )
    shared_dir = apm_modules_dir / "acme" / "shared-lib"
    shared_dir.mkdir(parents=True)
    (shared_dir / "apm.yml").write_text("name: shared-lib\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"))
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/shared-lib", depth=2, resolved_by="acme/pkg-a", resolved_commit="sss"
        )
    )
    shared_key = _shared_key(lockfile, "acme/shared-lib")

    pkg_c_ref = DependencyReference.parse("acme/pkg-c")
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, apm_modules_dir, [pkg_c_ref], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key in result.reachable
    assert result.reachable_via[shared_key] == ("acme/pkg-c", None)


def test_reachable_via_is_empty_when_no_candidates_are_rescued(tmp_path):
    """A survivor that does not declare the candidate at all -- the
    negative twin's shape -- must leave ``reachable_via`` empty for it,
    not populate it with a spurious/incidental entry.
    """
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    result = compute_forward_reachable_keys(
        lockfile, tmp_path, tmp_path / "apm_modules", [], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key not in result.reachable
    assert shared_key not in result.reachable_via


def test_local_negative_twin_no_survivor_declares_shared(tmp_path):
    """Same lockfile shape, but no surviving direct ref declares ../shared
    -- must NOT be marked reachable (proves the walk doesn't over-rescue).
    """
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    result = compute_forward_reachable_keys(
        lockfile, tmp_path, tmp_path / "apm_modules", [], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key not in result.reachable


def test_remote_diamond_shared_dep_is_reachable_via_surviving_parent(tmp_path):
    apm_modules_dir = tmp_path / "apm_modules"
    pkg_c_dir = apm_modules_dir / "acme" / "pkg-c"
    pkg_c_dir.mkdir(parents=True)
    (pkg_c_dir / "apm.yml").write_text(
        "name: pkg-c\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/shared-lib\n"
    )
    shared_dir = apm_modules_dir / "acme" / "shared-lib"
    shared_dir.mkdir(parents=True)
    (shared_dir / "apm.yml").write_text("name: shared-lib\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/pkg-c", depth=1, resolved_commit="ccc"))
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/shared-lib", depth=2, resolved_by="acme/pkg-a", resolved_commit="sss"
        )
    )
    shared_key = _shared_key(lockfile, "acme/shared-lib")

    pkg_c_ref = DependencyReference.parse("acme/pkg-c")
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, apm_modules_dir, [pkg_c_ref], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key in result.reachable


def test_deeper_chain_three_levels_is_reachable(tmp_path):
    apm_modules_dir = tmp_path / "apm_modules"
    top_dir = apm_modules_dir / "acme" / "top"
    top_dir.mkdir(parents=True)
    (top_dir / "apm.yml").write_text(
        "name: top\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/mid\n"
    )
    mid_dir = apm_modules_dir / "acme" / "mid"
    mid_dir.mkdir(parents=True)
    (mid_dir / "apm.yml").write_text(
        "name: mid\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/leaf\n"
    )
    leaf_dir = apm_modules_dir / "acme" / "leaf"
    leaf_dir.mkdir(parents=True)
    (leaf_dir / "apm.yml").write_text("name: leaf\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/top", depth=1, resolved_commit="t"))
    lockfile.add_dependency(
        LockedDependency(repo_url="acme/mid", depth=2, resolved_by="acme/top", resolved_commit="m")
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="acme/leaf", depth=3, resolved_by="acme/mid", resolved_commit="l")
    )
    leaf_key = _shared_key(lockfile, "acme/leaf")
    mid_key = _shared_key(lockfile, "acme/mid")

    top_ref = DependencyReference.parse("acme/top")
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, apm_modules_dir, [top_ref], frozenset({leaf_key, mid_key})
    )

    assert result.complete is True
    assert leaf_key in result.reachable
    assert mid_key in result.reachable


def test_cycle_safety_does_not_hang(tmp_path):
    """A local manifest that (incorrectly) declares a dependency cycle
    back on itself must not cause an infinite walk.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (tmp_path / "root-b").mkdir()
    (tmp_path / "root-b" / "apm.yml").write_text(
        "name: root-b\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../shared\n"
    )
    (tmp_path / "shared").mkdir()
    # shared points back at root-b -- a cycle.
    (tmp_path / "shared" / "apm.yml").write_text(
        "name: shared\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../root-b\n"
    )

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-b", source="local", local_path="../root-b", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    root_b_ref = DependencyReference.parse_from_dict({"path": "../root-b"})
    result = compute_forward_reachable_keys(
        lockfile, project_root, project_root / "apm_modules", [root_b_ref], frozenset({shared_key})
    )

    assert result.complete is True
    assert shared_key in result.reachable


def test_fails_closed_on_missing_survivor_manifest(tmp_path):
    """root-b's real apm.yml is missing (deleted out from under it) --
    reachability cannot be verified, so the result must be incomplete.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "apm.yml").write_text("name: shared\nversion: 1.0.0\n")
    # Deliberately do NOT create tmp_path/root-b/apm.yml.

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-b", source="local", local_path="../root-b", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    root_b_ref = DependencyReference.parse_from_dict({"path": "../root-b"})
    result = compute_forward_reachable_keys(
        lockfile, project_root, project_root / "apm_modules", [root_b_ref], frozenset({shared_key})
    )

    assert result.complete is False
    assert len(result.unverifiable) == 1
    pkg_id, reason = result.unverifiable[0]
    assert "root-b" in pkg_id
    assert reason  # non-empty diagnostic message


def test_fails_closed_on_malformed_survivor_manifest(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (tmp_path / "root-b").mkdir()
    (tmp_path / "root-b" / "apm.yml").write_text("name: [unterminated\n")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "apm.yml").write_text("name: shared\nversion: 1.0.0\n")

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-a", source="local", local_path="../root-a", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(repo_url="_local/root-b", source="local", local_path="../root-b", depth=1)
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/root-a",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    root_b_ref = DependencyReference.parse_from_dict({"path": "../root-b"})
    result = compute_forward_reachable_keys(
        lockfile, project_root, project_root / "apm_modules", [root_b_ref], frozenset({shared_key})
    )

    assert result.complete is False
    assert len(result.unverifiable) == 1


def test_fails_closed_on_remote_install_path_symlink_escape(tmp_path):
    """A survivor legitimately declares a remote child dep, but that
    child's real install location is a symlink escaping apm_modules/ --
    ``get_install_path`` must raise, and the walk must fail closed.
    """
    apm_modules_dir = tmp_path / "apm_modules"
    pkg_a_dir = apm_modules_dir / "acme" / "pkg-a"
    pkg_a_dir.mkdir(parents=True)
    (pkg_a_dir / "apm.yml").write_text(
        "name: pkg-a\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/shared-lib\n"
    )
    outside = tmp_path / "outside_evil"
    outside.mkdir()
    (outside / "apm.yml").write_text("name: evil\nversion: 1.0.0\n")
    (apm_modules_dir / "acme" / "shared-lib").symlink_to(outside)

    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="acme/pkg-a", depth=1, resolved_commit="aaa"))
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/shared-lib", depth=2, resolved_by="acme/pkg-a", resolved_commit="sss"
        )
    )
    shared_key = _shared_key(lockfile, "acme/shared-lib")

    pkg_a_ref = DependencyReference.parse("acme/pkg-a")
    result = compute_forward_reachable_keys(
        lockfile, tmp_path, apm_modules_dir, [pkg_a_ref], frozenset({shared_key})
    )

    assert result.complete is False
    assert any("shared-lib" in pkg_id for pkg_id, _ in result.unverifiable)


def test_fails_closed_on_corrupt_local_anchor_chain(tmp_path):
    """A candidate orphan's own ``resolved_by`` chain is ambiguous/corrupt
    (points at a parent that isn't itself in the lockfile as local) --
    ``resolve_local_dep_dir`` raises ``LocalResolutionError``, which must
    also fail closed.
    """
    lockfile = LockFile()
    # resolved_by points at a parent key that does not exist at all.
    lockfile.add_dependency(
        LockedDependency(
            repo_url="_local/shared",
            source="local",
            local_path="../shared",
            resolved_by="_local/does-not-exist",
            depth=2,
        )
    )
    shared_key = _shared_key(lockfile, "_local/shared")

    result = compute_forward_reachable_keys(
        lockfile, tmp_path, tmp_path / "apm_modules", [], frozenset({shared_key})
    )

    assert result.complete is False
    assert any(shared_key == pkg_id for pkg_id, _ in result.unverifiable)
