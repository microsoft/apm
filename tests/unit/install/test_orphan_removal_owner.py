"""Owner-level contract for every command's orphan removal decision (#4011)."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.orphans import DeploymentClaims, OrphanOperation, decide_orphan_removal


def _decide(operation: OrphanOperation, lock: LockFile, **inputs):
    arguments = {
        "roots": (),
        "candidate_universe": lock,
        "installed_paths": None,
        "resolved_keys": None,
        "intended": None,
        "materialized": None,
        "reachability": None,
        "deployment_claims": None,
    }
    arguments.update(inputs)
    return decide_orphan_removal(operation=operation, **arguments)


def _lock() -> LockFile:
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="owner/gone", deployed_files=["shared.md", "gone.md"])
    )
    lock.add_dependency(LockedDependency(repo_url="owner/kept", deployed_files=["shared.md"]))
    lock.add_dependency(LockedDependency(repo_url="owner/leaf", resolved_by="owner/gone", depth=2))
    return lock


def test_install_keeps_files_a_retained_package_still_claims() -> None:
    """Preview and real run both keep a file another package still deploys."""
    decision = _decide(
        OrphanOperation.INSTALL,
        _lock(),
        intended={"owner/kept"},
        deployment_claims=DeploymentClaims(live_files=None, retained={}),
    )
    assert decision.removed_files["owner/gone"] == ("gone.md",)
    assert decision.claimed_files["owner/gone"] == ("shared.md",)
    assert "owner/kept" not in decision.candidate_keys


def test_undeletable_files_keep_their_package_locked() -> None:
    decision = _decide(
        OrphanOperation.INSTALL,
        _lock(),
        intended={"owner/kept"},
        deployment_claims=DeploymentClaims(
            live_files=frozenset(), retained={"owner/gone": {"gone.md": None}}
        ),
    )
    assert decision.retained_keys == {"owner/gone"}
    assert "owner/gone" not in decision.removed_keys


def test_audit_reports_only_the_direct_entry_projection() -> None:
    decision = _decide(OrphanOperation.AUDIT, _lock(), intended=())
    assert decision.removed_keys == {"owner/gone", "owner/kept"}
    assert "owner/leaf" in decision.candidate_keys


@pytest.mark.parametrize(
    ("operation", "missing"),
    [
        (OrphanOperation.INSTALL, "intended"),
        (OrphanOperation.UNINSTALL, "reachability"),
        (OrphanOperation.PRUNE, "installed_paths"),
    ],
)
def test_missing_evidence_fails_closed(operation: OrphanOperation, missing: str) -> None:
    with pytest.raises(ValueError, match=missing):
        _decide(operation, _lock())
