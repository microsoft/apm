"""Bundle admission: the one owner every bundle deploy entry point calls.

``apm install <bundle>`` and ``apm unpack`` admit a bundle here before any
file is written: org policy is discovered and enforced through the shared
install preflight (outcome routing, dependency allow/deny, MCP and target
checks), then the bundle's integrity is checked under the policy's
``security.integrity.require_hashes`` setting.

Each bundle format brings its own integrity check; the policy gate and the
``require_hashes`` decision stay here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from apm_cli.bundle.unpacker import UnpackCandidate
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.policy.schema import ApmPolicy

IntegrityCheck = Callable[[bool], None]
"""Format-specific integrity check; receives whether policy requires hashes."""


def admit_bundle(
    *,
    policy_root: Path,
    apm_deps: Iterable[DependencyReference],
    mcp_deps: Sequence[MCPDependency],
    target_decision: EffectiveTargetDecision,
    integrity_check: IntegrityCheck,
    no_policy: bool,
    logger: Any,
    dry_run: bool,
    fetch_result: Any = None,
) -> ApmPolicy | None:
    """Admit a bundle for deployment, or raise before anything is written.

    Raises :class:`~apm_cli.install.errors.PolicyViolationError` when org
    policy blocks the bundle or cannot be verified under
    ``fetch_failure_default: block``, and the integrity check's own error
    when the bundle fails it.
    """
    from ..policy.install_preflight import run_policy_preflight
    from .integrity import require_hashes_enabled

    policy_fetch, _enforcement_active = run_policy_preflight(
        project_root=policy_root,
        apm_deps=list(apm_deps),
        mcp_deps=mcp_deps,
        no_policy=no_policy,
        logger=logger,
        dry_run=dry_run,
        effective_target=target_decision.canonical_targets,
        effective_target_source=target_decision.source,
        fetch_result=fetch_result,
    )
    policy = policy_fetch.policy if policy_fetch is not None else None
    integrity_check(require_hashes_enabled(policy.security.integrity if policy else None))
    return policy


def apm_bundle_admission(
    output_dir: Path, logger: Any, *, dry_run: bool
) -> Callable[[UnpackCandidate], None]:
    """Admission for ``apm unpack`` of a ``--format apm`` bundle.

    Org policy resolves from *output_dir*, the project the files deploy
    into, and checks the bundle's locked dependencies and pack target.
    Executable content never reaches here: the unpacker refuses it first.
    """

    def admit(candidate: UnpackCandidate) -> None:
        from ..bundle.local_bundle import _extract_pack_targets
        from ..core.target_detection import EffectiveTargetDecision

        source = "bundle pack target"
        decision = EffectiveTargetDecision(
            _extract_pack_targets({"pack": candidate.pack_meta}) or None, source
        )
        try:
            decision.canonical_targets  # noqa: B018 -- validate the projection
        except KeyError:
            # A target this APM version does not know deploys nowhere it manages.
            decision = EffectiveTargetDecision(None, source)
        admit_bundle(
            policy_root=output_dir,
            apm_deps=(
                dep.to_dependency_ref() for dep in candidate.lockfile.get_package_dependencies()
            ),
            mcp_deps=(),
            target_decision=decision,
            integrity_check=apm_bundle_integrity(
                candidate.source_dir, candidate.lockfile, candidate.files, logger
            ),
            no_policy=False,
            logger=logger,
            dry_run=dry_run,
        )

    return admit


def unpack_record_root(output_dir: Path) -> Path | None:
    """The project whose lockfile records an unpack into *output_dir*, or ``None``.

    An APM project root (``apm.yml`` present) records the files, exactly as
    ``apm install <bundle>`` does; any other directory stays untracked.
    """
    from ..constants import APM_YML_FILENAME

    return output_dir if (output_dir / APM_YML_FILENAME).is_file() else None


def record_unpacked_files(output_dir: Path, files: Sequence[str], logger: Any) -> None:
    """Record the files ``apm unpack`` wrote, or announce that they are untracked.

    The record goes through the shared local-bundle lockfile owner, so audit
    checks the files and a later install reconciles them.
    """
    root = unpack_record_root(output_dir)
    if root is None:
        logger.info(
            "unpack output is not tracked: the output directory has no apm.yml. "
            "Unpack into an APM project to record the deployed files."
        )
        return
    if not files:
        return
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import InstallScope
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ..utils.content_hash import compute_file_hash

    migrate_lockfile_if_needed(root)
    lockfile_path = get_lockfile_path(root)
    lockfile = LockFile.read(lockfile_path) or LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile,
        list(files),
        {rel: compute_file_hash(root / rel) for rel in files},
        scope=InstallScope.PROJECT,
    )
    lockfile.write(lockfile_path)


def plugin_bundle_integrity(bundle_info: Any, logger: Any) -> IntegrityCheck:
    """Integrity check for a plugin bundle: its ``pack.bundle_files`` manifest."""

    def check(require_hashes: bool) -> None:
        from ..bundle.local_bundle import verify_bundle_integrity

        if bundle_info.lockfile is None:
            if require_hashes:
                from .errors import InstallFailureAlreadyRendered

                logger.error(
                    "Bundle has no apm.lock.yaml, but org policy requires "
                    "integrity hashes. Repack it with the current APM version "
                    "and retry."
                )
                raise InstallFailureAlreadyRendered(
                    "Org policy requires integrity hashes for this bundle"
                )
            logger.warning(
                "Bundle has no apm.lock.yaml -- skipping integrity check. "
                "It was produced by an older APM version or by another tool; "
                "repack it with 'apm pack' to embed an integrity manifest."
            )
            return
        errors = verify_bundle_integrity(bundle_info.source_dir, bundle_info.lockfile)
        if errors:
            logger.error("Bundle integrity check failed:")
            for err in errors:
                logger.error_detail(err)
            raise click.Abort()
        logger.verbose_detail("Bundle integrity verified")

    return check


def apm_bundle_integrity(
    source_dir: Path, lockfile: LockFile, files: Sequence[str], logger: Any
) -> IntegrityCheck:
    """Integrity check for a ``--format apm`` bundle: its per-file lock hashes.

    *files* are the regular files the bundle would write. A file whose bytes
    differ from its locked hash, or that has no locked hash, is refused when
    policy requires hashes; otherwise an altered file is a warning.
    """

    def check(require_hashes: bool) -> None:
        locked: dict[str, str] = {}
        for dep in lockfile.get_all_dependencies():
            locked.update(dep.deployed_file_hashes)
        locked.update(lockfile.local_deployed_file_hashes)
        altered, unhashed = _unverified_files(source_dir, files, locked)
        if require_hashes and (altered or unhashed):
            details = [f"Hash mismatch: {rel}" for rel in altered]
            details += [f"No locked hash: {rel}" for rel in unhashed]
            raise ValueError(
                "Org policy requires integrity hashes, and the bundle failed its "
                "integrity check:\n" + "\n".join(f"  - {line}" for line in details)
            )
        for rel in altered:
            logger.warning(f"Bundle file differs from its locked hash: {rel}")

    return check


def _unverified_files(
    source_dir: Path, files: Sequence[str], locked: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    from ..utils.content_hash import compute_file_hash

    altered: list[str] = []
    unhashed: list[str] = []
    for rel in files:
        path = source_dir / rel
        if not path.is_file() or path.is_symlink():
            continue
        expected = locked.get(rel)
        if not expected:
            unhashed.append(rel)
        elif compute_file_hash(path) != expected:
            altered.append(rel)
    return altered, unhashed
