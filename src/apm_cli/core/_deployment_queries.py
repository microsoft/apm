"""Read-only projections of recorded deployment evidence for the ledger codec."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.utils.path_security import resolve_for_containment, resolved_relative_within

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence, ValuesView

    from apm_cli.core.deployment_state import DeploymentRecord
    from apm_cli.deps.lockfile import LockFile


def recorded_rows(lockfile: LockFile) -> ValuesView[DeploymentRecord] | None:
    """Return recorded rows, or None if the only possible evidence is legacy inference."""
    ledger = lockfile.deployment_ledger
    if not lockfile._deployments_present or ledger is None:
        return None
    return ledger.records.values()


def group_owner_records(
    records: Iterable[DeploymentRecord],
) -> dict[str, tuple[DeploymentRecord, ...]]:
    """Index already scope-filtered deployment rows by recorded owner."""
    grouped: dict[str, list[DeploymentRecord]] = {}
    for record in records:
        for owner in dict.fromkeys(record.owners):
            grouped.setdefault(owner, []).append(record)
    return {owner: tuple(rows) for owner, rows in grouped.items()}


class CallRoots:
    """Roots resolved at most once within ONE ledger call, then dropped with it.

    Lazy, so a root is observed exactly when it is first needed, as before;
    never stored on the ledger or reused by a later call.
    """

    def __init__(self) -> None:
        self._resolved: dict[str, Path] = {}

    def __call__(self, root: Path) -> Path:
        key = os.fspath(root)
        if key not in self._resolved:
            self._resolved[key] = resolve_for_containment(root)
        return self._resolved[key]


def skills_locator(
    ledger: Any,
    path: Path,
    targets: Sequence[Any],
    roots: Callable[[Path], Path] | None = None,
    candidate: Path | None = None,
) -> Any:
    """Locate *path* under a target's skills root (*candidate*: *path* resolved now)."""
    resolve_root = roots or resolve_for_containment
    if candidate is None:
        candidate = resolve_for_containment(path)
    for target in targets:
        if not target.supports("skills"):
            continue
        skills_root = resolve_root(target.skills_deploy_path(ledger.root))
        if resolved_relative_within(candidate, skills_root) is not None:
            return ledger._encode(candidate, target, roots(ledger.root) if roots else None)
    return None


def write_locator(
    ledger: Any,
    path: Path,
    targets: Sequence[Any],
    roots: Callable[[Path], Path] | None = None,
    candidate: Path | None = None,
) -> Any:
    """Locate a final-rendered write inside a target's roots, resolving *path* once."""
    resolve_root = roots or resolve_for_containment
    if candidate is None:
        candidate = resolve_for_containment(path)
    locator = skills_locator(ledger, path, targets, roots, candidate)
    if locator is not None:
        return locator
    for target in targets:
        target_roots = [target.deploy_path(ledger.root)]
        if target.resolved_deploy_root is None:
            target_roots.extend(
                ledger.root / mapping.deploy_root
                for mapping in target.primitives.values()
                if mapping.deploy_root
            )
        for root in target_roots:
            if resolved_relative_within(candidate, resolve_root(root)) is not None:
                return ledger._encode(candidate, target, roots(ledger.root) if roots else None)
    return None
