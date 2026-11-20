"""Lockfile claim membership for the drift replay diff.

Single owner of which replayed paths a lockfile entry claims. A differing
path no entry claims is a user-authored file APM skipped, so drift reports it
as ``unmanaged-conflict`` rather than a managed local edit (#3682); a matching
unclaimed path is ``unrecorded`` (#2379).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def claimed_prefixes(
    tracked: Mapping[str, str],
    hashed_files: Collection[str],
    project_files: Collection[str],
) -> tuple[str, ...]:
    """Return ``dir/`` prefixes for the tracked entries that name directories.

    A manifest entry may name a directory rather than each file beneath it
    (``scan_lockfile_packages`` and ``_check_deployed_files_present`` both
    accept that shape), so a file is claimed when a tracked *directory*
    covers it.

    Entries with a recorded hash are files even if the current project path was
    replaced by a directory. Entries present in *project_files* are also files,
    which preserves the same rule for legacy hashless lockfiles. A file claim
    must never act as a prefix: otherwise replacing tracked ``a.md`` with an
    ``a.md/`` directory would let every descendant dodge the membership check.
    """
    return tuple(
        normalized + "/"
        for path in tracked
        if (normalized := path.rstrip("/"))
        and normalized not in hashed_files
        and normalized not in project_files
    )


@dataclass(frozen=True)
class ReplayClaims:
    """Lock claims rebased onto one comparison root."""

    tracked: dict[str, str]
    prefixes: tuple[str, ...]
    shared: frozenset[str]

    @classmethod
    def build(
        cls,
        tracked_claims: dict[str, str],
        hashed_claims: Collection[str],
        project_root: Path,
        project_files: Collection[str],
        *,
        targets: tuple[Any, ...],
        absolute_only: bool,
        shared: Collection[str],
    ) -> ReplayClaims:
        """Rebase lock claims and file-shaped hash paths onto *project_root*."""
        from apm_cli.install.audit_target_roots import claims_for_root

        def rebase(claims: dict[str, str]) -> dict[str, str]:
            return claims_for_root(
                claims, project_root, absolute_only=absolute_only, targets=targets
            )

        tracked = rebase(tracked_claims)
        hashed = set(rebase(dict.fromkeys(hashed_claims, "")))
        return cls(tracked, claimed_prefixes(tracked, hashed, project_files), frozenset(shared))

    def unclaimed(self, rel: str) -> bool:
        """Return True when no lock entry, directory claim or shared config covers *rel*."""
        return not (rel in self.tracked or rel.startswith(self.prefixes) or rel in self.shared)
