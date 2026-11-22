"""Lockfile-hash evidence for files an APM before 0.37 deployed (#4398).

APM before 0.37 never refused an overwrite for lack of a deployment receipt
(#4360), so the hash its lockfile recorded is the only record of what it
deployed. The first 0.37 install migrates such files once; afterwards the
receipt model applies. The deployment ledger is the only consumer.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.deployment_state import LocatorKind

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

# Locks written before 0.37, whose APM never refused an overwrite for lack of a
# receipt (#4360), are the only record of what that APM deployed.
_LOCK_HASH_EVIDENCE_BEFORE = (0, 37)


def legacy_lock_hashes(lockfile: LockFile | None) -> dict[str, str]:
    """Map each claim to its recorded hash when *lockfile* predates 0.37 (#4398).

    A lock written before 0.37 (or one recording no version) comes from an APM
    that did not require receipt evidence, so its hash is the record of what
    that APM deployed. A claim two owners record with different hashes is dropped.
    """
    if lockfile is None or not _predates_receipt_evidence(lockfile.apm_version):
        return {}
    hashes: dict[str, str] = {}
    conflicts: set[str] = set()
    recorded = [dep.deployed_file_hashes for dep in lockfile.dependencies.values()]
    recorded.append(lockfile.local_deployed_file_hashes)
    for mapping in recorded:
        for claim, digest in (mapping or {}).items():
            key = claim.replace("\\", "/")
            if hashes.setdefault(key, digest) != digest:
                conflicts.add(key)
    for key in conflicts:
        del hashes[key]
    return hashes


def _predates_receipt_evidence(version: str | None) -> bool:
    if not version:
        return True
    parts: list[int] = []
    for piece in version.split(".")[:2]:
        digits = "".join(itertools.takewhile(str.isdigit, piece))
        if not digits:
            return False
        parts.append(int(digits))
    return tuple(parts) < _LOCK_HASH_EVIDENCE_BEFORE


def git_permits_legacy_adoption(path: Path) -> bool:
    """Whether git lets a pre-0.37 lock hash vouch for *path* (#4398).

    A forger can only know committed bytes, and those stay recoverable, so a
    file git tracks unchanged qualifies, as does one git does not track at all
    (untracked, ignored, or no repository). A tracked file with a staged or
    unstaged change that exactly matches the lock is suspicious and does not.
    Any other git error fails closed.
    """
    import subprocess

    env = {**os.environ, "LC_ALL": "C"}

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args], cwd=path.parent, env=env, capture_output=True, check=False, timeout=30
        )

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            return b"not a git repository" in inside.stderr.lower()
        tracked = git("ls-files", "--error-unmatch", "--", path.name)
        if tracked.returncode != 0:
            return b"did not match any file" in tracked.stderr.lower()
        status = git("status", "--porcelain", "--", path.name)
    except FileNotFoundError:
        return True  # no git installed: nothing can track the file
    except (OSError, subprocess.SubprocessError):
        return False
    return status.returncode == 0 and not status.stdout.strip()


def legacy_bytes_owned(path: Path, recorded: str | None) -> bool:
    """Whether *path* still holds the bytes a pre-0.37 lock recorded for it.

    The hash counts only when the file still holds those bytes and git holds
    no uncommitted change to it (:func:`git_permits_legacy_adoption`), so a
    forged lock cannot vouch for a staged or unstaged edit.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    try:
        return (
            recorded is not None
            and compute_file_hash(path) == recorded
            and git_permits_legacy_adoption(path)
        )
    except OSError:
        return False


class RowProjection:
    """``DeploymentLedgerCodec`` half that decides when a lockfile load replays rows."""

    @classmethod
    def load_observes_per_row_projection(cls, dependencies: Iterable[LockedDependency]) -> bool:
        """Whether loading *dependencies* row by row can leave a non-empty ledger.

        Per-row legacy invalidation only rebuilds rows when a URI receipt or a
        reserved owner (local-bundle provenance) appears, and such a rebuild
        keeps the first claimant as active owner. Otherwise every step leaves
        the empty default that the load replaces once at its end, so a load may
        skip the per-row projection (O(N^2) over a lockfile's rows).
        """
        from apm_cli.deps.lockfile import LockFile

        reserved_owners = cls.valid_owner_keys(LockFile())
        return any(
            dep.get_unique_key() in reserved_owners
            or any(cls._legacy_locator(path).kind is LocatorKind.URI for path in dep.deployed_files)
            for dep in dependencies
        )
