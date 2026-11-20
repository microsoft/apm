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


def seed_receipts(
    project_root: Path, files: list[Path], *, target: str, owner: str, user_scope: bool = False
) -> None:
    """Record local deployment receipts for *files*, as the installing machine holds them.

    Fixtures that forge a lockfile and its deployed files model a real install;
    the receipt is the deployment evidence that install would have written.
    """
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.targets import KNOWN_TARGETS

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    session = DeploymentMaterializationSession(project_root, scope, None)
    session.record(
        {path: compute_file_hash(path) for path in files}, [KNOWN_TARGETS[target]], owner
    )
    session.flush()


def no_evidence(deploy_root: Path) -> DeploymentEvidence:
    """Return evidence that authorizes nothing (a clone without receipts or render)."""
    return DeploymentEvidence.none(deploy_root)


class _ProvenOverwrites:
    """An overwrite gate whose every managed file carries deployment evidence."""

    owner = "test"
    user_scope = False

    def authorizes(self, path: Path, claim: str) -> bool:
        return True

    def keep(self, path: Path) -> bool:
        return False


def prove_overwrites(monkeypatch) -> None:
    """Give direct integrator calls the evidence a live install binds (#4360).

    Outside ``recorded_writes`` an integrator replaces no differing managed
    file; tests of the overwrite itself model a receipted reinstall.
    """
    from apm_cli.integration.base_integrator import BaseIntegrator

    monkeypatch.setattr(BaseIntegrator, "_overwrite_gate", _ProvenOverwrites())
