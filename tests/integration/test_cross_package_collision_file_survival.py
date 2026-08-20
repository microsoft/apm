"""Cross-package deployed-file ownership regression."""

from pathlib import Path
from types import SimpleNamespace

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.utils.content_hash import compute_file_hash


def test_collision_loser_does_not_delete_winner_file(tmp_path: Path) -> None:
    loser = "orga/loser-skill"
    winner = "orgb/winner-skill"
    shared = ".claude/skills/shared-topic/SKILL.md"
    shared_path = tmp_path / shared
    shared_path.parent.mkdir(parents=True)
    shared_path.write_text("# Shared skill\n", encoding="utf-8")

    prior = LockFile()
    prior.add_dependency(
        LockedDependency(
            repo_url=loser,
            deployed_files=[shared],
            deployed_file_hashes={shared: compute_file_hash(shared_path)},
        )
    )
    current = LockFile()
    current.add_dependency(LockedDependency(repo_url=loser))
    current.add_dependency(LockedDependency(repo_url=winner))
    context = SimpleNamespace(
        package_deployed_files={loser: [shared], winner: [shared]},
        existing_lockfile=prior,
        targets=[SimpleNamespace(name="claude", root_dir=".claude", primitives={})],
        project_root=tmp_path,
        diagnostics=SimpleNamespace(
            count_for_package=lambda *args, **kwargs: 0,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        logger=None,
        verbose=False,
    )

    LockfileBuilder(context)._attach_deployed_files(current)

    assert shared_path.exists()
    assert current.get_dependency(winner).deployed_files == [shared]
    assert current.get_dependency(loser).deployed_files == []
