"""File scanning for content integrity checks.

Extracted from ``commands/audit.py`` so the policy module can call these
without importing from the command layer.

Two scopes, because the two signals need different ones:

* :func:`scan_lockfile_packages` -- lockfile-driven. Required for anything
  compared against a recorded baseline (per-file hashes) and for per-package
  queries.
* :func:`scan_deployed_trees` -- deploy-tree-driven. Hidden-Unicode detection
  needs no baseline, so restricting it to recorded files would exempt every
  deployed file the lockfile omits (issue #2379).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..security.content_scanner import ContentScanner, ScanFinding
from ..utils.path_security import PathTraversalError, ensure_path_within


@dataclass(frozen=True)
class _FileScanResult:
    """Findings plus the exact repository-relative files examined."""

    findings_by_file: dict[str, list[ScanFinding]]
    scanned_files: frozenset[str]

    def merged(self, other: _FileScanResult) -> _FileScanResult:
        """Union scopes, treating ``self`` as the authoritative first scope."""
        findings = dict(self.findings_by_file)
        for rel_path, file_findings in other.findings_by_file.items():
            findings.setdefault(rel_path, file_findings)
        return _FileScanResult(
            findings_by_file=findings,
            scanned_files=self.scanned_files | other.scanned_files,
        )


def _empty_scan() -> _FileScanResult:
    """Return an empty independent scan result."""
    return _FileScanResult({}, frozenset())


def _is_safe_lockfile_path(rel_path: str, project_root: Path) -> bool:
    """Return True if a relative path from the lockfile is safe to read.

    Reuses the same logic as ``BaseIntegrator.validate_deploy_path``
    (no ``..``, allowed prefix, resolves within root).
    """
    return BaseIntegrator.validate_deploy_path(rel_path, project_root)


def _scan_directory_result(dir_path: Path, base_label: str) -> _FileScanResult:
    """Recursively scan a directory and label exact project-relative paths."""
    from ..security.gate import REPORT_POLICY, SecurityGate

    verdict = SecurityGate.scan_files(dir_path, policy=REPORT_POLICY)
    return _FileScanResult(
        findings_by_file={
            f"{base_label}/{rel_path}": file_findings
            for rel_path, file_findings in verdict.findings_by_file.items()
        },
        scanned_files=frozenset(f"{base_label}/{rel_path}" for rel_path in verdict.scanned_files),
    )


def _scan_files_in_dir(
    dir_path: Path,
    base_label: str,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Compatibility wrapper returning findings and unique file count."""
    result = _scan_directory_result(dir_path, base_label)
    return result.findings_by_file, len(result.scanned_files)


def _minimal_governed_prefixes(prefixes: set[str]) -> tuple[str, ...]:
    """Return non-empty prefixes with nested roots removed."""
    normalized = {prefix.rstrip("/") for prefix in prefixes if prefix.rstrip("/")}
    accepted: list[PurePosixPath] = []
    for prefix in sorted(normalized, key=lambda value: (len(PurePosixPath(value).parts), value)):
        candidate = PurePosixPath(prefix)
        if any(candidate == parent or candidate.is_relative_to(parent) for parent in accepted):
            continue
        accepted.append(candidate)
    return tuple(path.as_posix() for path in accepted)


def _scan_deployed_trees(project_root: Path) -> _FileScanResult:
    """Scan every file under the deploy trees this project's targets govern.

    Hash verification has to be manifest-driven: comparing a hash needs a
    recorded baseline to compare against. Hidden-Unicode detection does not --
    a bidi override is dangerous on sight, with no baseline required. Scoping
    both signals to ``deployed_files`` therefore leaves any deployed file the
    lockfile omits unscanned, permanently and with no indication that the
    scope narrowed (issue #2379). For this signal the deploy tree is the
    boundary, not manifest membership.

    Targets are resolved from the project, which is sufficient: a deploy tree
    absent from disk has nothing to scan, and one present on disk is exactly
    what ``detect_by_dir`` resolution keys on. Sources under ``.apm/`` are not
    in scope here -- the install-time pre-deployment gate owns those.
    """
    from ..install.manifest_reconcile import install_governance
    from ..integration.targets import resolve_targets

    file_prefixes, _uri_schemes = install_governance(resolve_targets(project_root))
    result = _empty_scan()

    for rel_path in _minimal_governed_prefixes(file_prefixes):
        # These prefixes come from the target registry, not from untrusted
        # input, so the deploy-path allow-list would be circular here (the
        # prefixes ARE that list). Containment is the property worth asserting,
        # via the sanctioned guard. Symlinked roots resolve outward and are
        # skipped; SecurityGate.scan_files likewise never follows links.
        candidate = project_root / rel_path
        if candidate.is_symlink():
            continue
        try:
            deploy_path = ensure_path_within(candidate, project_root)
        except PathTraversalError:
            continue
        if deploy_path == project_root.resolve():
            continue

        if deploy_path.is_dir():
            result = result.merged(_scan_directory_result(deploy_path, rel_path))
        elif deploy_path.is_file():
            findings = ContentScanner.scan_file(deploy_path)
            result = result.merged(
                _FileScanResult(
                    findings_by_file={rel_path: findings} if findings else {},
                    scanned_files=frozenset({rel_path}),
                )
            )

    return result


def scan_deployed_trees(project_root: Path) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan governed deploy trees and return findings plus unique file count."""
    result = _scan_deployed_trees(project_root)
    return result.findings_by_file, len(result.scanned_files)


def _scan_claimed_files(
    project_root: Path,
    claims: dict[str, str],
    package_filter: str | None,
) -> _FileScanResult:
    """Scan one canonical deployment-claim projection."""
    result = _empty_scan()
    for rel_path, owner in claims.items():
        if package_filter and owner != package_filter:
            continue

        safe_path = rel_path.rstrip("/")
        if not _is_safe_lockfile_path(safe_path, project_root):
            continue

        abs_path = project_root / rel_path
        if not abs_path.exists():
            continue

        if abs_path.is_dir():
            result = result.merged(_scan_directory_result(abs_path, safe_path))
            continue

        findings = ContentScanner.scan_file(abs_path)
        result = result.merged(
            _FileScanResult(
                findings_by_file={rel_path: findings} if findings else {},
                scanned_files=frozenset({rel_path}),
            )
        )

    return result


def _scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
) -> _FileScanResult:
    """Collect the exact lockfile-driven scan result."""
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return _empty_scan()

    from ..core.deployment_ledger import DeploymentLedgerCodec

    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock)
    return _scan_claimed_files(project_root, claims, package_filter)


def scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan deployed files tracked in apm.lock.yaml.

    Args:
        lockfile: An already-parsed lockfile for ``project_root``. When
            provided, the on-disk lockfile is not re-read.

    Returns:
        Findings grouped by path and the exact unique number of files scanned.
    """
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return {}, 0

    from ..core.deployment_ledger import DeploymentLedgerCodec

    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock)
    result = _scan_claimed_files(project_root, claims, package_filter)
    return result.findings_by_file, len(result.scanned_files)


def scan_project_files(
    project_root: Path,
    *,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
    include_deployed_trees: bool,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Union lockfile and deploy-tree scopes with exact unique accounting."""
    result = _scan_lockfile_packages(project_root, package_filter, lockfile)
    if include_deployed_trees:
        result = result.merged(_scan_deployed_trees(project_root))
    return result.findings_by_file, len(result.scanned_files)
