"""Withdraw what a refused ``--frozen`` install deployed outside the lock (#4309).

``--frozen`` never rewrites ``apm.lock.yaml``, so an install that deploys a
file the lock does not record must fail. A failed command preserves durable
state (I7): before refusing, the frozen owner removes exactly the unrecorded
files this install wrote, proven by this installation's deployment receipts,
and the directories that removal emptied. A file without that proof (a user
file the install adopted, or one edited since) is kept and named.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from apm_cli.deps.lockfile import LockFile


def _unrecorded_by_owner(
    candidates: Iterable[LockFile], unclaimed: set[str]
) -> Iterator[tuple[str, list[str], dict[str, str]]]:
    """Yield ``(owner, unrecorded claims, recorded hashes)`` per candidate owner."""
    seen: set[str] = set()
    for candidate in candidates:
        rows = [
            (key, list(dep.deployed_files), dict(dep.deployed_file_hashes))
            for key, dep in candidate.dependencies.items()
            if key != "."
        ]
        rows.append(
            (".", list(candidate.local_deployed_files), dict(candidate.local_deployed_file_hashes))
        )
        for owner, claims, hashes in rows:
            paths = [path for path in claims if path in unclaimed and path not in seen]
            seen.update(paths)
            if paths:
                yield owner, paths, hashes


def withdraw_unrecorded_deployments(
    candidates: Iterable[LockFile],
    unclaimed: set[str],
    *,
    committed: LockFile,
    lockfile_dir: Path,
) -> list[str]:
    """Remove the unrecorded files this install wrote; return the claims kept on disk."""
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
    from apm_cli.policy.ci_checks import resolve_audit_scope
    from apm_cli.utils.diagnostics import DiagnosticCollector

    deploy_root, user_scope, _targets = resolve_audit_scope(lockfile_dir, committed)
    diagnostics = DiagnosticCollector()
    # Receipts only: the committed lock never rendered these files, so only
    # this installation's own write receipts can prove it deployed them.
    evidence = DeploymentEvidence.collect(
        deploy_root,
        user_scope=user_scope,
        lockfile=None,
        lockfile_root=lockfile_dir,
        diagnostics=diagnostics,
    )
    kept: list[str] = []
    deleted: list[Path] = []
    # The cleanup owner retires the receipts of what it deletes.
    for owner, paths, hashes in _unrecorded_by_owner(candidates, unclaimed):
        result = remove_stale_deployed_files(
            paths,
            deploy_root,
            dep_key=owner,
            targets=None,
            diagnostics=diagnostics,
            evidence=evidence,
            recorded_hashes=hashes,
            failed_path_retained=False,
            user_scope=user_scope,
        )
        deleted.extend(result.deleted_targets)
        kept.extend(result.retained)
    if deleted:
        BaseIntegrator.cleanup_empty_parents(deleted, deploy_root)
    return sorted(kept)


def refuse_unrecorded_deployments(
    withheld: Iterable[str], unclaimed: set[str], committed: LockFile, lockfile_dir: Path
) -> Exception:
    """Withdraw this install's unrecorded files and return the ``--frozen`` refusal."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.errors import FrozenInstallError
    from apm_cli.install.service import _outermost

    kept = withdraw_unrecorded_deployments(
        [LockFile.from_yaml(payload) for payload in withheld],
        unclaimed,
        committed=committed,
        lockfile_dir=lockfile_dir,
    )
    reasons = [
        f"  - {path} is deployed by this install but not recorded in apm.lock.yaml"
        for path in _outermost(unclaimed)
    ]
    reasons += [
        f"  - kept {path}: no receipt proves this install wrote it"
        for path in _outermost(set(kept))
    ]
    removed = "the other unrecorded files were" if kept else "the unrecorded files were"
    return FrozenInstallError(
        "--frozen: apm.lock.yaml does not record everything this install deploys.",
        reasons=reasons,
        tip=(
            f"Tip: {removed} removed again and apm.lock.yaml is unchanged. Run "
            "'apm install' without --frozen, retaining --root DIR or --global if "
            "specified, then commit the deployed files and apm.lock.yaml."
        ),
    )
