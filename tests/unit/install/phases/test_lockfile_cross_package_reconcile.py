"""Regression traps for cross-package deployed-file ownership reconciliation.

``ctx.package_deployed_files`` is populated once per dep_key, independently,
by that dep's own integration call (see ``install/template.py``). When two
different packages' primitives resolve to the same on-disk path -- a name
collision, e.g. two repos both shipping a skill called ``shared-topic`` --
each package's own integration call correctly and independently reports "I
wrote this path" at the moment it ran. Without reconciliation, BOTH entries
end up claiming ``deployed_files`` for a path only one of them actually
owns on disk -- a lockfile integrity bug: a future ``apm uninstall`` or
``apm audit`` on the "losing" package would act on a file it does not
control. The claim decision belongs to ``DeploymentReconciler``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apm_cli.core.deployment_state import DeploymentReconciler
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


@pytest.mark.parametrize("obligation", ["hash", "projection", "owner-work"])
def test_aggregate_attachment_work_is_linear(tmp_path, monkeypatch, obligation) -> None:
    """Count attachment work, not latency or the separate concatenation algorithm."""
    from apm_cli.core import deployment_state
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.phases import lockfile as attachment
    from apm_cli.integration.targets import resolve_targets

    original_hash = attachment.compute_file_hash
    original_record = deployment_state.DeploymentRecord
    original_apply = DeploymentLedgerCodec.apply_to_lockfile
    counts = {}

    def hash_file(path):
        counts["hash"] += 1
        counts["bytes"] += path.stat().st_size
        return original_hash(path)

    def record(**kwargs):
        counts["owners"] += len(kwargs["owners"])
        return original_record(**kwargs)

    def project(ledger, lockfile):
        counts["projection"] += 1
        return original_apply(ledger, lockfile)

    monkeypatch.setattr(attachment, "compute_file_hash", hash_file)
    monkeypatch.setattr(deployment_state, "DeploymentRecord", record)
    monkeypatch.setattr(DeploymentLedgerCodec, "apply_to_lockfile", project)
    observations = []
    for size in (50, 500):
        counts.update(hash=0, bytes=0, owners=0, projection=0)
        root = tmp_path / str(size)
        path = root / ".copilot/copilot-instructions.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixed-width-contribution\n" * size)
        lock = LockFile()
        claims = {}
        for i in range(size):
            key = f"fixture/package-{i}"
            lock.add_dependency(LockedDependency(repo_url=key))
            claims[key] = [".copilot/copilot-instructions.md"]
        ctx = _ctx(
            package_deployed_files=claims,
            targets=resolve_targets(root, user_scope=True, explicit_target=["copilot"]),
            project_root=root,
        )
        ctx.scope = InstallScope.USER
        LockfileBuilder(ctx)._attach_deployed_files(lock)
        observations.append(dict(counts))
        records = list(lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert records[0].owners == tuple(claims)
        assert records[0].active_owner == list(claims)[-1]
        assert records[0].content_hash == original_hash(path)
        assert all(
            dep.deployed_files == [".copilot/copilot-instructions.md"]
            for dep in lock.dependencies.values()
        )
    small, large = observations
    print("ATTACHMENT WORK", observations)
    if obligation == "hash":
        assert small["hash"] == large["hash"] == 1
        assert large["bytes"] == 10 * small["bytes"]
    elif obligation == "projection":
        assert small["projection"] == large["projection"] == 1
    else:
        assert large["owners"] < 15 * small["owners"]


def test_aggregate_attachment_invalidates_digest_after_cleanup(tmp_path, monkeypatch):
    """A reconciliation cleanup handoff invalidates this phase's digest, not future commands."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.install import manifest_reconcile
    from apm_cli.install.phases.lockfile import compute_deployed_hashes
    from apm_cli.integration.cleanup import CleanupResult
    from apm_cli.integration.targets import resolve_targets

    relative = ".copilot/copilot-instructions.md"
    target = tmp_path / relative
    target.parent.mkdir()
    target.write_text("before cleanup\n")
    original = manifest_reconcile.reconcile_deployed_block
    calls = []

    def reconcile(**kwargs):
        result = original(**kwargs)
        calls.append(kwargs)
        if len(calls) == 1:
            # Simulate the owner's cleanup callback, then a replacement.
            # This fixture does not claim a native cleanup execution.
            target.write_text("replacement following cleanup\n")
            if kwargs.get("on_cleanup") is not None:
                kwargs["on_cleanup"](CleanupResult(deleted=[relative]))
        return result

    monkeypatch.setattr(manifest_reconcile, "reconcile_deployed_block", reconcile)
    lock = LockFile()
    for name in ("first", "second"):
        lock.add_dependency(LockedDependency(repo_url=f"fixture/{name}"))
    ctx = _ctx(
        package_deployed_files={key: [relative] for key in lock.dependencies},
        targets=resolve_targets(tmp_path, user_scope=True, explicit_target=["copilot"]),
        project_root=tmp_path,
    )
    ctx.scope = InstallScope.USER
    LockfileBuilder(ctx)._attach_deployed_files(lock)
    assert (
        next(iter(lock.deployment_ledger.records.values())).content_hash
        == compute_deployed_hashes([relative], tmp_path)[relative]
    )


@pytest.mark.parametrize("has_root", [False, True], ids=["no-root", "root"])
def test_warm_aggregate_root_carry_work_is_linear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_root: bool
) -> None:
    """Count actual codec owner freezing through the existing-local-state chain."""
    from apm_cli.core import deployment_ledger as codec
    from apm_cli.core.deployment_state import DeploymentLedger, DeploymentRecord
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.phases.lockfile import compute_deployed_hashes
    from apm_cli.integration.targets import resolve_targets

    observations = []
    relative = ".copilot/copilot-instructions.md"
    original_record = codec.DeploymentRecord
    counts = {"records": 0, "owners": 0}

    def record(**kwargs: Any) -> DeploymentRecord:
        counts["records"] += 1
        counts["owners"] += len(kwargs["owners"])
        return original_record(**kwargs)

    for size in (50, 500):
        root = tmp_path / str(size)
        aggregate = root / relative
        aggregate.parent.mkdir(parents=True)
        aggregate.write_bytes(b"current contribution\n" * size)
        current_hash = compute_deployed_hashes([relative], root)[relative]
        current, previous = LockFile(), LockFile()
        claims = {}
        for index in range(size):
            owner = f"fixture/package-{index}"
            current.add_dependency(LockedDependency(repo_url=owner))
            previous.add_dependency(LockedDependency(repo_url=owner))
            claims[owner] = [relative]
        owners = (*claims, ".") if has_root else tuple(claims)
        locator = codec.DeploymentLedgerCodec._legacy_locator(relative)
        prior_record = DeploymentRecord(
            locator=locator,
            owners=owners,
            active_owner=owners[-1],
            content_hash="sha256:" + "a" * 64,
        )
        codec.DeploymentLedgerCodec.apply_to_lockfile(
            DeploymentLedger(records={locator.key: prior_record}), previous
        )
        ctx = _ctx(
            package_deployed_files=claims,
            existing_lockfile=previous,
            targets=resolve_targets(root, user_scope=True, explicit_target=["copilot"]),
            project_root=root,
        )
        ctx.scope = InstallScope.USER
        ctx.local_deployed_files = [relative] if has_root else []
        ctx.logger = None
        builder = LockfileBuilder(ctx)
        builder._attach_deployed_files(current)
        counts.update(records=0, owners=0)
        with monkeypatch.context() as patch:
            patch.setattr(codec, "DeploymentRecord", record)
            builder._preserve_existing_local_state(current)
        observations.append(dict(counts))
        assert counts["records"] > 0, "The warm codec route was not exercised"
        assert len(current.deployment_ledger.records) == 1
        final = next(iter(current.deployment_ledger.records.values()))
        assert final.owners == owners
        assert final.active_owner == owners[-1]
        assert final.content_hash == current_hash
        assert all(dep.deployed_files == [relative] for dep in current.dependencies.values())
        assert all(
            dep.deployed_file_hashes == {relative: current_hash}
            for dep in current.dependencies.values()
        )
        assert current.local_deployed_files == ([relative] if has_root else [])
        assert current.local_deployed_file_hashes == ({relative: current_hash} if has_root else {})
        restored = LockFile.from_yaml(current.to_yaml())
        assert restored.deployment_ledger == current.deployment_ledger
    small, large = observations
    print("WARM CODEC OWNER WORK", observations)
    assert large["owners"] < 15 * small["owners"]
    assert large["records"] == small["records"]


def _target(name, root_dir=".claude"):
    return SimpleNamespace(name=name, root_dir=root_dir, primitives={})


def _ctx(*, package_deployed_files, existing_lockfile=None, targets=None, project_root):
    return SimpleNamespace(
        package_deployed_files=package_deployed_files,
        existing_lockfile=existing_lockfile,
        targets=targets or [_target("claude")],
        project_root=project_root,
    )


def _reconciled_current(package_deployed_files: dict[str, list[str]]) -> dict[str, list[str]]:
    claims = DeploymentReconciler.reconcile_package_claims(
        package_keys=package_deployed_files,
        current_claims=package_deployed_files,
        prior_files={},
        prior_hashes={},
    )
    return {owner: list(claim.current_files) for owner, claim in claims.items()}


class TestReconcileCrossPackageDeployedFiles:
    def test_colliding_path_kept_only_on_last_writer(self) -> None:
        """Two dep_keys both report the same path; only the last (the actual
        on-disk owner, under sequential integration order) keeps it."""
        package_deployed_files = {
            "orga/shared-skill": [".claude/skills/shared-topic/SKILL.md"],
            "orgb/shared-skill": [".claude/skills/shared-topic/SKILL.md"],
        }
        reconciled = _reconciled_current(package_deployed_files)

        assert reconciled["orga/shared-skill"] == []
        assert reconciled["orgb/shared-skill"] == [".claude/skills/shared-topic/SKILL.md"]

    def test_non_colliding_paths_are_untouched(self) -> None:
        """Normal case: no two dep_keys share a path -- nothing is stripped."""
        package_deployed_files = {
            "orga/repo-a": [".claude/skills/topic-a/SKILL.md"],
            "orgb/repo-b": [".claude/skills/topic-b/SKILL.md"],
        }
        reconciled = _reconciled_current(package_deployed_files)

        assert reconciled["orga/repo-a"] == [".claude/skills/topic-a/SKILL.md"]
        assert reconciled["orgb/repo-b"] == [".claude/skills/topic-b/SKILL.md"]

    def test_partial_collision_only_strips_the_shared_path(self) -> None:
        """A dep_key with multiple deployed files only loses the ONE path
        another dep_key also claims -- its other files are untouched."""
        package_deployed_files = {
            "orga/shared-skill": [
                ".claude/skills/shared-topic/SKILL.md",
                ".claude/skills/unique-to-a/SKILL.md",
            ],
            "orgb/shared-skill": [".claude/skills/shared-topic/SKILL.md"],
        }
        reconciled = _reconciled_current(package_deployed_files)

        assert reconciled["orga/shared-skill"] == [".claude/skills/unique-to-a/SKILL.md"]
        assert reconciled["orgb/shared-skill"] == [".claude/skills/shared-topic/SKILL.md"]

    def test_attach_deployed_files_end_to_end_only_winner_recorded(self, tmp_path) -> None:
        """End-to-end through _attach_deployed_files: the lockfile entry for
        the losing package must not claim deployed_files for the collided
        path, and must not resurrect it from a prior lockfile either."""
        key_a = "orga/shared-skill"
        key_b = "orgb/shared-skill"
        collided_path = ".claude/skills/shared-topic/SKILL.md"

        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key_a,
                deployed_files=[collided_path],
                deployed_file_hashes={collided_path: "sha256:aaa"},
            )
        )

        new = LockFile()
        new.add_dependency(LockedDependency(repo_url=key_a))
        new.add_dependency(LockedDependency(repo_url=key_b))

        ctx = _ctx(
            package_deployed_files={key_a: [collided_path], key_b: [collided_path]},
            existing_lockfile=prior,
            targets=[_target("claude")],
            project_root=tmp_path,
        )
        LockfileBuilder(ctx)._attach_deployed_files(new)

        dep_a = new.get_dependency(key_a)
        dep_b = new.get_dependency(key_b)
        assert collided_path not in (dep_a.deployed_files or [])
        assert collided_path in dep_b.deployed_files
