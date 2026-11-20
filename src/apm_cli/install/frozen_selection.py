"""Frozen-install selection comparison (req-rs-018).

Which locked dependencies a ``--frozen`` install would deploy with a skill or
target selection that differs from ``apm.lock.yaml``, and the outermost
unrecorded paths a failing frozen install reports. Consumed by
:class:`~apm_cli.install.service.InstallService`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apm_cli.core import remediation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apm_cli.deps.lockfile import LockFile


def frozen_selection_changes(deps_to_install: Iterable[Any], committed: LockFile) -> list[str]:
    """Name each locked dependency whose resolved selection differs (req-rs-018).

    Runs before any deployment so ``--frozen`` never deploys either selection.
    A recorded ``subset_pairings`` value that is malformed or disagrees with
    the flat fields is reported as invalid lock state.
    """
    from apm_cli.models.dependency.subsets import dependency_pairing

    marketplace_index = committed.marketplace_index()
    reasons: dict[str, str] = {}
    for dep_ref in deps_to_install:
        key = dep_ref.get_unique_key()
        locked = committed.get_dependency(key, marketplace_index=marketplace_index)
        if locked is None:
            continue
        try:
            recorded = locked.selection()
        except ValueError as exc:
            reasons[key] = f"selection for {key} is invalid in apm.lock.yaml ({exc})"
            continue
        if recorded != dependency_pairing(dep_ref):
            reasons[key] = (
                f"selection for {key} changed: the skills and targets its declaring "
                "manifests request differ from apm.lock.yaml; "
                f"{remediation.DEPENDENCY_SELECTION_CHANGED.clause()}"
            )
    return [reasons[key] for key in sorted(reasons)]


def _changed_selections(committed: LockFile, candidate: LockFile) -> dict[str, str]:
    """Name each locked dependency whose resolved skill or target selection changed."""
    changed: dict[str, str] = {}
    for dependency in candidate.get_package_dependencies():
        key = dependency.get_unique_key()
        locked = committed.get_dependency(key)
        if locked is None:
            continue
        for field_name, label in (("skill_subset", "skills"), ("target_subset", "targets")):
            before = sorted(getattr(locked, field_name) or ())
            after = sorted(getattr(dependency, field_name) or ())
            if before != after:
                changed[key] = (
                    f"selection for {key} changed: locked {label} {before or 'all'}, "
                    f"resolved {label} {after or 'all'}; "
                    f"{remediation.DEPENDENCY_SELECTION_CHANGED.clause()}"
                )
                break
        else:
            # Same unions, but the per-target pairing can still differ (req-rs-018).
            try:
                same = locked.selection() == dependency.selection()
            except ValueError:
                same = False
            if not same:
                changed[key] = (
                    f"selection for {key} changed: the skills paired with each target "
                    f"differ from apm.lock.yaml; {remediation.DEPENDENCY_SELECTION_CHANGED.clause()}"
                )
    return changed


def _outermost(paths: set[str]) -> list[str]:
    """Sort *paths*, dropping any already covered by a listed ancestor.

    ``deployed_files`` records a primitive's directory as well as each file
    beneath it, so an unrecorded skill otherwise reports twice.  Sorted
    order puts an ancestor before its descendants, so a single pass over
    the accumulated output is enough.

    The prefix scan is O(n^2) in the number of unclaimed paths.  It runs
    only on the error path of a failing frozen install, where *n* is what
    one install left unrecorded and the process is about to exit.
    """
    outermost: list[str] = []
    for path in sorted(paths):
        if not any(path.startswith(f"{parent}/") for parent in outermost):
            outermost.append(path)
    return outermost
