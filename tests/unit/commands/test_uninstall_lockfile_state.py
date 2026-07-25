"""Tests for atomic uninstall lockfile reconciliation."""

from apm_cli.commands.uninstall.lockfile_state import reconcile_uninstall_deployment_state
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile


def test_duplicate_locator_values_update_each_target_record_hash(tmp_path) -> None:
    """Shared paths keep distinct target records without index collisions."""
    deployed_path = ".agents/skills/shared/SKILL.md"
    target_file = tmp_path / deployed_path
    target_file.parent.mkdir(parents=True)
    target_file.write_text("# current\n", encoding="ascii")
    records = {}
    for target in ("codex", "cursor"):
        locator = DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target=target,
            value=deployed_path,
            runtime=None,
            scope="project",
        )
        records[locator.key] = DeploymentRecord(
            locator=locator,
            owners=("removed", "survivor"),
            active_owner="removed",
            content_hash="sha256:old",
        )
    lockfile = LockFile(
        dependencies={
            "survivor": LockedDependency(repo_url="survivor"),
        },
        deployment_ledger=DeploymentLedger(records=records),
        _deployments_present=True,
    )

    changed = reconcile_uninstall_deployment_state(
        lockfile,
        deploy_root=tmp_path,
        all_deployed_files={deployed_path},
        surviving_deployed_files={"survivor": {deployed_path}},
    )

    assert changed is True
    hashes = {record.content_hash for record in lockfile.deployment_ledger.records.values()}
    assert len(hashes) == 1
    assert next(iter(hashes)).startswith("sha256:")
    assert hashes != {"sha256:old"}
