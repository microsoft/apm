"""Owner tests for deployment ledger projections (deployment-provenance-state).

Exact projections at ``DeploymentLedgerCodec`` (see tests/mutation/floors.json).
"""

from __future__ import annotations

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec, DeploymentOwnerViolation
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.unit


def _record(value: str, *, owners: tuple[str, ...], active: str, digest: str | None):
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="claude",
        value=value,
        runtime=None,
        scope="project",
    )
    return DeploymentRecord(
        locator=locator, owners=owners, active_owner=active, content_hash=digest
    )


def _lockfile(*records: DeploymentRecord) -> LockFile:
    lockfile = LockFile(
        dependencies={
            "acme/a": LockedDependency(repo_url="acme/a"),
            "acme/b": LockedDependency(repo_url="acme/b"),
        }
    )
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(records={record.locator.key: record for record in records}),
        lockfile,
    )
    return lockfile


def test_hash_and_owner_projections_are_keyed_by_path() -> None:
    lockfile = _lockfile(
        _record(".claude/rules/a.md", owners=("acme/a",), active="acme/a", digest="sha256:a"),
        _record(
            ".claude/rules/b.md", owners=("acme/a", "acme/b"), active="acme/b", digest=None
        ),
    )

    assert DeploymentLedgerCodec.deployed_file_hashes(lockfile) == {
        ".claude/rules/a.md": "sha256:a"
    }
    assert DeploymentLedgerCodec.active_file_owners(lockfile) == {
        ".claude/rules/a.md": "acme/a",
        ".claude/rules/b.md": "acme/b",
    }


def test_owner_violations_honor_exclusions_and_report_any_invalid_reference() -> None:
    shared = _record(
        ".claude/rules/shared.md", owners=("acme/a", "acme/b"), active="acme/a", digest="sha256:s"
    )
    lockfile = _lockfile(shared)

    assert DeploymentLedgerCodec.owner_reference_violations(lockfile) == ()
    assert DeploymentLedgerCodec.owner_reference_violations(
        lockfile, excluded_dependency_keys=("acme/b",)
    ) == (
        DeploymentOwnerViolation(
            locator=shared.locator,
            owners=("acme/a", "acme/b"),
            active_owner="acme/a",
            invalid_owners=("acme/b",),
            invalid_active_owner=None,
        ),
    )
