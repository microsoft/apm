"""Test helpers for cleanup deployment evidence (#3701)."""

from __future__ import annotations

from pathlib import Path

from apm_cli.integration.cleanup import DeploymentEvidence
from apm_cli.utils.content_hash import compute_file_hash


def receipted_evidence(*roots: Path, deploy_root: Path | None = None) -> DeploymentEvidence:
    """Return evidence as on the deploying machine.

    Every regular file currently under *roots* carries a local deployment
    receipt of its current bytes, as if APM had just written it. Files
    edited or created after this call are not covered.
    """
    receipts: dict[str, str] = {}
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if path.is_file() and not path.is_symlink():
                receipts[path.resolve().as_posix()] = compute_file_hash(path)
    base = deploy_root if deploy_root is not None else (roots[0] if roots else Path.cwd())
    return DeploymentEvidence(deploy_root=base, receipts=receipts, render=None)


def no_evidence(deploy_root: Path) -> DeploymentEvidence:
    """Return evidence that authorizes nothing (a clone without receipts or render)."""
    return DeploymentEvidence.none(deploy_root)
