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


def test_fully_refreshed_survivor_replaces_complete_deployed_file_view(
    tmp_path,
) -> None:
    """A shared-slot refresh records added files and drops stale survivor files."""
    old_path = ".github/instructions/old.instructions.md"
    new_path = ".github/instructions/new.instructions.md"
    new_file = tmp_path / new_path
    new_file.parent.mkdir(parents=True)
    new_file.write_text("# refreshed\n", encoding="ascii")
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=old_path,
        runtime=None,
        scope="project",
    )
    dependency = LockedDependency(
        repo_url="survivor",
        deployed_files=[old_path],
        deployed_file_hashes={old_path: "sha256:old"},
    )
    lockfile = LockFile(
        dependencies={"survivor": dependency},
        deployment_ledger=DeploymentLedger(
            records={
                locator.key: DeploymentRecord(
                    locator=locator,
                    owners=("survivor",),
                    active_owner="survivor",
                    content_hash="sha256:old",
                )
            }
        ),
        _deployments_present=True,
    )

    changed = reconcile_uninstall_deployment_state(
        lockfile,
        deploy_root=tmp_path,
        all_deployed_files={old_path},
        surviving_deployed_files={"survivor": {new_path}},
        fully_refreshed_dependency_keys={"survivor"},
    )

    assert changed is True
    assert dependency.deployed_files == [new_path]
    assert set(dependency.deployed_file_hashes) == {new_path}
    assert dependency.deployed_file_hashes[new_path].startswith("sha256:")
