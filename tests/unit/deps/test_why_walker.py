"""Unit tests for :mod:`apm_cli.deps.why_walker`."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.why_walker import (
    AmbiguousPackageError,
    PackageNotInstalledError,
    compute_why,
    resolve_package_query,
)


def _direct(repo_url: str, **kwargs) -> LockedDependency:
    return LockedDependency(repo_url=repo_url, depth=1, resolved_by=None, **kwargs)


def _transitive(repo_url: str, resolved_by: str, depth: int = 2, **kwargs) -> LockedDependency:
    return LockedDependency(repo_url=repo_url, depth=depth, resolved_by=resolved_by, **kwargs)


def _build(deps: list[LockedDependency]) -> LockFile:
    lf = LockFile()
    for d in deps:
        lf.add_dependency(d)
    return lf


# ---------------------------------------------------------------------------
# compute_why
# ---------------------------------------------------------------------------


def test_compute_why_direct_dep_returns_single_path_with_no_intermediates():
    direct = _direct("acme/foo")
    lf = _build([direct])

    result = compute_why(lf, direct)

    assert result.is_direct is True
    assert len(result.paths) == 1
    chain = result.paths[0].chain
    assert len(chain) == 1
    assert chain[0].parent_key is None
    assert chain[0].child_key == "acme/foo"


def test_compute_why_transitive_dep_with_one_path():
    parent = _direct("acme/big")
    child = _transitive("acme/util", resolved_by="acme/big")
    lf = _build([parent, child])

    result = compute_why(lf, child)

    assert result.is_direct is False
    assert len(result.paths) == 1
    chain = result.paths[0].chain
    assert [e.child_key for e in chain] == ["acme/big", "acme/util"]
    assert chain[0].parent_key is None  # root
    assert chain[1].parent_key == "acme/big"


def test_compute_why_target_with_duplicate_repo_url_walks_recorded_parent():
    # Two records share the same repo_url under different virtual_path keys.
    # compute_why() operates on a single LockedDependency target and walks
    # that record's lone recorded resolved_by parent -- it does NOT fan out
    # to alternative parents that other duplicate records reference, since
    # the lockfile schema records exactly one resolved_by per record.
    p1 = _direct("acme/big")
    p2 = _direct("acme/other")
    util1 = _transitive("acme/util", resolved_by="acme/big")
    util2 = _transitive("acme/util", resolved_by="acme/other", depth=2)
    # Two different unique keys via virtual_path so both survive add_dependency
    util2.virtual_path = "alt"
    util2.is_virtual = True
    lf = _build([p1, p2, util1, util2])

    # Picking util1 only finds the path via acme/big.
    result = compute_why(lf, util1)
    assert result.is_direct is False
    assert len(result.paths) == 1


def test_compute_why_diamond_records_single_resolved_by_path():
    # A -> B -> D, A -> C -> D. Walker keys parents by repo_url; the lockfile
    # records D once with a single resolved_by. compute_why returns one
    # canonical path, not multiple, because we walk from a single
    # LockedDependency target upward through its one recorded parent.
    # (Multi-parent fan-in would require a lockfile schema change -- the
    # iterative worklist is ready for it without an API change.)
    a = _direct("o/a")
    b = _transitive("o/b", resolved_by="o/a")
    c = _transitive("o/c", resolved_by="o/a")
    d = _transitive("o/d", resolved_by="o/b")
    lf = _build([a, b, c, d])

    result = compute_why(lf, d)
    # Single path because d only records one parent (o/b).
    assert len(result.paths) == 1
    chain = result.paths[0].chain
    assert [e.child_key for e in chain] == ["o/a", "o/b", "o/d"]


def test_compute_why_target_in_cycle_does_not_infinite_loop():
    # Construct a malformed lockfile where two deps reference each other.
    a = LockedDependency(repo_url="o/a", depth=1, resolved_by="o/b")
    b = LockedDependency(repo_url="o/b", depth=1, resolved_by="o/a")
    lf = _build([a, b])

    result = compute_why(lf, a)

    # Walker terminates and returns at least one path; no infinite loop.
    assert len(result.paths) >= 1
    for path in result.paths:
        keys = [e.child_key for e in path.chain]
        assert keys.count("o/a") <= 1
        assert keys.count("o/b") <= 1


# ---------------------------------------------------------------------------
# resolve_package_query
# ---------------------------------------------------------------------------


def test_resolve_package_query_by_basename_unique_match():
    lf = _build([_direct("acme/shared-utils"), _direct("acme/other")])
    dep = resolve_package_query(lf, "shared-utils")
    assert dep.repo_url == "acme/shared-utils"


def test_resolve_package_query_by_basename_ambiguous_raises():
    lf = _build(
        [
            _direct("acme/shared-utils"),
            _direct("other-org/shared-utils"),
        ]
    )
    with pytest.raises(AmbiguousPackageError) as exc_info:
        resolve_package_query(lf, "shared-utils")
    assert "acme/shared-utils" in exc_info.value.matches
    assert "other-org/shared-utils" in exc_info.value.matches


def test_resolve_package_query_by_owner_repo():
    lf = _build([_direct("acme/foo"), _direct("acme/bar")])
    dep = resolve_package_query(lf, "acme/bar")
    assert dep.repo_url == "acme/bar"


def test_resolve_package_query_by_alias_falls_back_to_basename():
    # Aliases are not stored on LockedDependency today; resolution by alias
    # gracefully degrades to basename match.
    lf = _build([_direct("acme/special")])
    dep = resolve_package_query(lf, "special")
    assert dep.repo_url == "acme/special"


def test_resolve_package_query_by_full_repo_url():
    lf = _build([_direct("github.com/acme/foo")])
    dep = resolve_package_query(lf, "github.com/acme/foo")
    assert dep.repo_url == "github.com/acme/foo"


def test_resolve_package_query_not_installed_raises():
    lf = _build([_direct("acme/foo")])
    with pytest.raises(PackageNotInstalledError):
        resolve_package_query(lf, "nope")


def test_walker_handles_lockfile_without_constraint_field():
    # Graceful-degrade trap: LockedDependency on main today has no
    # ``constraint`` attribute. The walker must still produce edges with
    # ``constraint=None`` rather than crash on AttributeError.
    parent = _direct("acme/big")
    child = _transitive("acme/util", resolved_by="acme/big")
    lf = _build([parent, child])

    result = compute_why(lf, child)
    for path in result.paths:
        for edge in path.chain:
            # constraint reads gracefully via getattr; either None or str.
            assert edge.constraint is None or isinstance(edge.constraint, str)


def test_walker_handles_basename_with_trailing_dot_git():
    lf = _build([_direct("acme/foo.git")])
    dep = resolve_package_query(lf, "foo")
    assert dep.repo_url == "acme/foo.git"
