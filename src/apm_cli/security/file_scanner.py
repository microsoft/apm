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

from pathlib import Path

from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..security.content_scanner import ContentScanner, ScanFinding
from ..utils.path_security import PathTraversalError, ensure_path_within


def _is_safe_lockfile_path(rel_path: str, project_root: Path) -> bool:
    """Return True if a relative path from the lockfile is safe to read.

    Reuses the same logic as ``BaseIntegrator.validate_deploy_path``
    (no ``..``, allowed prefix, resolves within root).
    """
    return BaseIntegrator.validate_deploy_path(rel_path, project_root)


def _scan_files_in_dir(
    dir_path: Path,
    base_label: str,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Recursively scan all files under a directory via SecurityGate.

    Returns (findings_by_file, files_scanned).
    """
    from ..security.gate import REPORT_POLICY, SecurityGate

    verdict = SecurityGate.scan_files(dir_path, policy=REPORT_POLICY)
    findings: dict[str, list[ScanFinding]] = {}
    for rel_path, file_findings in verdict.findings_by_file.items():
        label = f"{base_label}/{rel_path}"
        findings[label] = file_findings
    return findings, verdict.files_scanned


def scan_deployed_trees(
    project_root: Path,
) -> tuple[dict[str, list[ScanFinding]], int]:
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

    all_findings: dict[str, list[ScanFinding]] = {}
    files_scanned = 0

    for prefix in sorted(file_prefixes):
        rel_path = prefix.rstrip("/")
        # These prefixes come from the target registry, not from untrusted
        # input, so the deploy-path allow-list would be circular here (the
        # prefixes ARE that list). Containment is the property worth asserting,
        # via the sanctioned guard. Symlinked roots resolve outward and are
        # skipped; `SecurityGate.scan_files` likewise never follows links.
        try:
            deploy_path = ensure_path_within(project_root / rel_path, project_root)
        except PathTraversalError:
            continue
        if deploy_path == project_root.resolve():
            continue  # empty/degenerate prefix: never scan the whole project

        if deploy_path.is_dir():
            dir_findings, dir_count = _scan_files_in_dir(deploy_path, rel_path)
            files_scanned += dir_count
            all_findings.update(dir_findings)
        elif deploy_path.is_file():
            # A governance entry may name a single generated file (for
            # example an `.agents/` root context file) rather than a subtree.
            files_scanned += 1
            findings = ContentScanner.scan_file(deploy_path)
            if findings:
                all_findings[rel_path] = findings

    return all_findings, files_scanned


def scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan deployed files tracked in apm.lock.yaml.

    Args:
        lockfile: An already-parsed lockfile for ``project_root``. When
            provided, the on-disk lockfile is not re-read (callers such as
            ``apm audit`` that already loaded it avoid a duplicate parse).

    Returns:
        (findings_by_file, files_scanned) -- findings grouped by file path
        and total number of files scanned.
    """
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return {}, 0

    all_findings: dict[str, list[ScanFinding]] = {}
    files_scanned = 0

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock)
    for rel_path, owner in claims.items():
        if package_filter and owner != package_filter:
            continue

        if not _is_safe_lockfile_path(rel_path.rstrip("/"), project_root):
            continue

        abs_path = project_root / rel_path
        if not abs_path.exists():
            continue

        if abs_path.is_dir():
            dir_findings, dir_count = _scan_files_in_dir(abs_path, rel_path.rstrip("/"))
            files_scanned += dir_count
            all_findings.update(dir_findings)
            continue

        files_scanned += 1
        findings = ContentScanner.scan_file(abs_path)
        if findings:
            all_findings[rel_path] = findings

    return all_findings, files_scanned
