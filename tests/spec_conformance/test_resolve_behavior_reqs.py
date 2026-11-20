"""Behavioral receipts for OpenAPM resolution requirements (req-rs-*).

Not testable against this implementation (no receipt here):
req-rg-001 -- registry server immutability; APM ships no registry server.
req-pr-004 -- tag/version alignment is a producer rule; APM has no consumer check.
req-pr-005 -- advisory tag-signing SHOULD; v0.1 consumers verify nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.resolver import RegistryPackageResolver
from apm_cli.deps.why_walker import compute_why
from apm_cli.drift import detect_ref_change
from apm_cli.models.dependency.reference import DependencyReference
from tests.integration.test_replacement_source_consumers import _locked
from tests.unit.deps.test_apm_resolver_edge_cases import _write_pkg
from tests.unit.registry.test_resolver_e2e_http import (
    _build_apm_tarball,
    _make_dep,
    _running_server,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("req-rg-001", "req-rs-004", "req-rs-005", "req-rs-006", "req-rs-009"),
]

# --- req-rs-004: semver range drift is character equality -----------------


def _range_dep(ref: str) -> DependencyReference:
    return DependencyReference.parse(f"acme/widget#{ref}")


def _locked_range(constraint: str) -> LockedDependency:
    """The lock row install records after resolving *constraint* to v1.5.3."""
    locked = LockedDependency.from_dependency_ref(_range_dep(constraint), "a" * 40, 1, None)
    locked.constraint = constraint
    locked.resolved_ref = locked.resolved_tag = "v1.5.3"
    locked.version = "1.5.3"
    return locked


@pytest.mark.req("req-rs-004")
@pytest.mark.parametrize(
    ("manifest", "locked", "drift"),
    [
        ("^1.2.0", "^1.2.0", False),
        (">=1.2.0 <2.0.0", ">=1.2.0 <2.0.0", False),
        (">=1.2.0  <2.0.0", ">=1.2.0 <2.0.0", True),
        (">=1.2.0 <2.0.0", ">=1.2.0  <2.0.0", True),
        ("~1.2.0", "^1.2.0", True),
        ("^1.2.0", "^1.2", True),
    ],
    ids=["equal", "equal-compound", "extra-space", "lock-extra-space", "operator", "same-set"],
)
def test_semver_range_drifts_unless_character_equal(
    manifest: str, locked: str, drift: bool
) -> None:
    """Only a character-equal recorded constraint replays the lock; any edit re-resolves."""
    assert (
        detect_ref_change(_range_dep(manifest), _locked_range(locked), update_refs=False) is drift
    )


# --- req-rs-005: deps why chains --------------------------------------------


def _chains(result) -> list[tuple[str, ...]]:
    return [tuple(edge.child_key for edge in path.chain) for path in result.paths]


def _diamond_lock(tmp_path: Path, *, reverse: bool):
    """A replaced target requested by owner/zeta (direct) and owner/alpha (under owner/root)."""
    _, target, lock = _locked(tmp_path, {"path": "../local"}, parent="owner/zeta")
    second = DependencyReference.parse("https://github.com/MixedOrg/MixedRepo#v2")
    target.replacement.record(second, "owner/alpha", False)
    parents = [
        LockedDependency(repo_url="owner/zeta", depth=1),
        LockedDependency(repo_url="owner/root", depth=1),
        LockedDependency(repo_url="owner/alpha", depth=2, resolved_by="owner/root"),
    ]
    for parent in reversed(parents) if reverse else parents:
        lock.add_dependency(parent)
    return lock, target


@pytest.mark.req("req-rs-005")
def test_why_returns_every_root_chain_in_lexicographic_order(tmp_path: Path) -> None:
    """Both root-to-target chains come back, sorted, independent of lock order."""
    lock, target = _diamond_lock(tmp_path / "a", reverse=False)
    result = compute_why(lock, target)
    leaf = result.paths[0].chain[-1].child_key

    assert _chains(result) == [
        ("owner/root", "owner/alpha", leaf),
        ("owner/zeta", leaf),
    ]
    assert [path.chain[0].parent_key for path in result.paths] == [None, None]
    reordered_lock, reordered_target = _diamond_lock(tmp_path / "b", reverse=True)
    assert _chains(compute_why(reordered_lock, reordered_target)) == _chains(result)


@pytest.mark.req("req-rs-005")
def test_why_terminates_deterministically_on_a_cyclic_lock() -> None:
    """A malformed lock whose parents form a cycle yields the same finite answer twice."""
    a = LockedDependency(repo_url="o/a", depth=2, resolved_by="o/b")
    b = LockedDependency(repo_url="o/b", depth=2, resolved_by="o/c")
    c = LockedDependency(repo_url="o/c", depth=2, resolved_by="o/a")
    lock = LockFile()
    for dep in (a, b, c):
        lock.add_dependency(dep)

    first = compute_why(lock, a)
    assert first.paths
    assert all(len(set(chain)) == len(chain) for chain in _chains(first))
    assert compute_why(lock, a) == first


# --- req-rs-006: transitive depth cap ----------------------------------------


def _chain_project(tmp_path: Path) -> Path:
    modules = tmp_path / "apm_modules"
    _write_pkg(modules / "org", "a", deps=["org/b"])
    _write_pkg(modules / "org", "b")
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump({"name": "root", "version": "1.0.0", "dependencies": {"apm": ["org/a"]}}),
        encoding="utf-8",
    )
    return tmp_path / "apm.yml"


def test_default_transitive_depth_cap_is_fifty(tmp_path: Path) -> None:
    """Passing default clause of req-rs-006 (the id reports the xfail below)."""
    assert APMDependencyResolver(apm_modules_dir=tmp_path).max_depth == 50


@pytest.mark.req("req-rs-006")
@pytest.mark.xfail(
    strict=True,
    reason="#4277: exceeding the depth cap drops the dependency without failing",
)
def test_exceeding_depth_cap_fails_naming_the_chain(tmp_path: Path) -> None:
    manifest = _chain_project(tmp_path)
    resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules", max_depth=1)
    resolver._project_root = tmp_path
    tree = resolver.build_dependency_tree(manifest)

    assert any("org/a" in error and "org/b" in error for error in tree.resolution_errors)


# --- req-rs-009: any declared registry may serve hash-matching bytes ----------


@pytest.mark.req("req-rs-009")
@pytest.mark.xfail(
    strict=True,
    reason="#4278: a resolved_url on another declared registry is refused before hashing",
)
def test_lock_replay_accepts_mirror_bytes_matching_the_locked_hash(tmp_path: Path) -> None:
    archive, digest = _build_apm_tarball()
    download = "/v1/packages/acme/web/versions/1.0.0/download"
    target = tmp_path / "apm_modules" / "acme" / "web"

    with (
        _running_server({}) as (primary_host, primary_port),
        _running_server({download: lambda _h: (200, "application/gzip", archive)}) as (h, p),
    ):
        mirror = f"http://{h}:{p}"
        resolver = RegistryPackageResolver(
            {"corp": f"http://{primary_host}:{primary_port}", "mirror": mirror}
        )
        resolver.download_from_lockfile(
            _make_dep(),
            target,
            resolved_url=f"{mirror}{download}",
            resolved_hash=f"sha256:{digest}",
            version="1.0.0",
        )

    assert (target / "apm.yml").is_file()
