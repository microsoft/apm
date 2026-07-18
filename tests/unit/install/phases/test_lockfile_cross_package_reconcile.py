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

from types import SimpleNamespace

from apm_cli.core.deployment_state import DeploymentReconciler
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


def _target(name, root_dir=".claude"):
    return SimpleNamespace(name=name, root_dir=root_dir, primitives={})


def _ctx(*, package_deployed_files, existing_lockfile=None, targets=None, project_root):
    return SimpleNamespace(
        package_deployed_files=package_deployed_files,
        existing_lockfile=existing_lockfile,
        targets=targets or [_target("claude")],
        project_root=project_root,
        lockfile_only=False,
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
