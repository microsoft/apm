"""Lockfile-driven file scanning for content integrity checks.

Extracted from ``commands/audit.py`` so the policy module can call
``scan_lockfile_packages`` without importing from the command layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple  # noqa: F401, UP035

from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..security.content_scanner import ContentScanner, ScanFinding


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


def scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan deployed files tracked in apm.lock.yaml.

    Returns:
        (findings_by_file, files_scanned) -- findings grouped by file path
        and total number of files scanned.
    """
    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path)
    if lock is None:
        return {}, 0

    all_findings: dict[str, list[ScanFinding]] = {}
    files_scanned = 0

    for dep_key, dep in lock.dependencies.items():
        if package_filter and dep_key != package_filter:
            continue

        for rel_path in dep.deployed_files:
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
