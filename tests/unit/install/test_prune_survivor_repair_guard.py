"""Prune repairs a survivor's metadata only under a parent it can record (#5126).

PROMISE: P-OWN-orphan-package-selection -- prune gives a surviving local row
the depth and dev status a fresh resolve records only when the walk's nearest
parent is the recorded one and could be recorded unambiguously; otherwise the
row's whole record stays as it was (source: #5126 scope record).
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.reachability import ReachabilityResult
from apm_cli.install.orphans import _repaired_survivors

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-orphan-package-selection")]


def _lock(f_declaring_parent: str) -> LockFile:
    lock = LockFile()
    for path in ("left/z", "right/z"):
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/z",
                source="local",
                local_path=f"../{path}",
                anchored_local_path=f"vendor/{path}",
                resolved_by=f"_local/{path[0]}",
                declaring_parent=f"./vendor/{path[0]}",
                depth=2,
            )
        )
    lock.add_dependency(
        LockedDependency(
            repo_url="_local/f",
            source="local",
            local_path="../../f",
            anchored_local_path="vendor/f",
            resolved_by="_local/z",
            declaring_parent=f_declaring_parent,
            depth=3,
        )
    )
    return lock


def _walk(lock: LockFile) -> ReachabilityResult:
    keys = {row.anchored_local_path: key for key, row in lock.dependencies.items()}
    left, right, f = keys["vendor/left/z"], keys["vendor/right/z"], keys["vendor/f"]
    return ReachabilityResult(
        reachable=frozenset({left, right, f}),
        complete=True,
        unverifiable=(),
        dev_only=frozenset({right, f}),
        parent_keys={f: right},
        depths={left: 2, right: 2, f: 3},
        declaring_edges={f: {right: ("_local/z", "../../f")}},
        local_edge_repos={(f, right): ("_local/f", None)},
    )


def test_no_repair_when_the_nearest_parent_shares_its_identity_with_another() -> None:
    """f was declared by left/z; the walk reaches it from right/z, also named z."""
    lock = _lock("local:vendor/left/z")
    walk = _walk(lock)
    (f,) = [key for key, row in lock.dependencies.items() if row.repo_url == "_local/f"]

    assert _repaired_survivors(lock, set(lock.dependencies), walk, []) == []
    assert lock.dependencies[f].declaring_parent == "local:vendor/left/z"
