"""Real-CLI contract for the --frozen under-recording failure (req-lk-006).

``apm install --frozen`` never writes the lockfile it freezes. When the
install deploys a path that ``apm.lock.yaml`` does not record, it exits 1,
names the path, and leaves the lockfile bytes untouched. A failed command
preserves durable state (I7, #4309): the files it deployed there are removed
again, so the project is byte-identical to before the command.

The project has no remote dependencies, so the scenario needs no network
access to resolve them (CI also sets APM_E2E_TESTS=1, which skips the
update check).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _result_evidence,
    _run_success,
    _Scenario,
)
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged
from tests.utils.local_package import LocalPackage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-INV-I7", "P-LAW-lockfile.frozen_preserves_bytes", "req-lk-006"),
]


def _add_skill(project: Path, name: str) -> Path:
    """Add local source content without refreshing the installed lockfile."""
    skill_dir = project / ".apm" / "skills" / name
    skill_dir.mkdir(parents=True)
    source = skill_dir / "SKILL.md"
    source.write_text(
        f"---\nname: {name}\ndescription: Frozen fixture\n---\n# {name}\n",
        encoding="ascii",
    )
    return source


def _assert_frozen_refusal_and_recovery(
    scenario: _Scenario,
    project: LocalPackage,
    environment: dict[str, str],
    deployed_path: PurePosixPath,
) -> None:
    """Refuse new claims, repair normally, then prove repeated frozen convergence."""
    lockfile = project.root / "apm.lock.yaml"
    locked = lockfile.read_bytes()
    manifest = project.manifest_path.read_bytes()
    before = ArtifactSnapshotSet.capture({"project": project.root})
    frozen = scenario.runner.run(
        (*_INSTALL_ARGS, "--frozen"),
        scenario_id="frozen-unrecorded-skill",
        cwd=project.root,
        env=environment,
    )
    assert frozen.returncode == 1, _result_evidence(frozen)
    # Rich wraps long lines; the diagnostic still has to name the missing claim.
    flat = " ".join((frozen.stdout + frozen.stderr).split())
    assert f"{deployed_path} is deployed by this install but not recorded in apm.lock.yaml" in flat
    assert lockfile.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert "the unrecorded files were removed again" in flat
    assert not (project.root / deployed_path).exists()
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture({"project": project.root}))

    _run_success(
        scenario,
        project,
        _INSTALL_ARGS,
        environment=environment,
        scenario_id="repair-unrecorded-skill",
    )
    assert lockfile.read_bytes() != locked
    _assert_frozen_replays(scenario, project, environment)


def _assert_frozen_replays(
    scenario: _Scenario, project: LocalPackage, environment: dict[str, str]
) -> None:
    """Require converged frozen installs to preserve project and user bytes."""
    roots = {"project": project.root, "user": scenario.isolated.home}
    repaired = ArtifactSnapshotSet.capture(roots)
    for replay in range(2):
        _run_success(
            scenario,
            project,
            (*_INSTALL_ARGS, "--frozen"),
            environment=environment,
            scenario_id=f"repaired-frozen-replay-{replay}",
        )
        assert_snapshot_set_unchanged(repaired, ArtifactSnapshotSet.capture(roots))


def _frozen_refusal(
    scenario: _Scenario,
    project: LocalPackage,
    environment: dict[str, str],
    scenario_id: str,
) -> str:
    """Run a refused ``--frozen`` install and return its flattened output."""
    result = scenario.runner.run(
        (*_INSTALL_ARGS, "--frozen"), scenario_id=scenario_id, cwd=project.root, env=environment
    )
    assert result.returncode == 1, _result_evidence(result)
    return " ".join((result.stdout + result.stderr).split())


def test_frozen_install_kept_file_remedy_runs_to_a_clean_refusal(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Execute the printed remedy for a kept (adopted) file, then rerun as told.

    A file an earlier install deployed is not this invocation's to remove, so
    the refusal keeps it and names the next step (#4439). Following that step
    must leave nothing kept: the rerun withdraws its own files and leaves the
    project and lockfile byte-identical. The edited and could-not-remove
    reasons need a change inside one invocation; their wording and next step
    are pinned in tests/unit/install/test_frozen_withdrawal.py.
    """
    scenario = _new_scenario(tmp_path / "frozen-kept", apm_binary_path)
    project = scenario.consumers.create("frozen-kept-project", targets=("claude",))
    _add_skill(project.root, "alpha")
    _run_success(
        scenario,
        project,
        _INSTALL_ARGS,
        environment=scenario.environment,
        scenario_id="kept-initial-install",
    )
    lockfile = project.root / "apm.lock.yaml"
    locked = lockfile.read_bytes()
    _add_skill(project.root, "beta")
    _run_success(
        scenario,
        project,
        _INSTALL_ARGS,
        environment=scenario.environment,
        scenario_id="kept-install-beta",
    )
    # A lockfile that predates beta (e.g. checked out from main) meets a disk
    # where an earlier install already deployed it.
    lockfile.write_bytes(locked)
    kept_dir = project.root / ".claude" / "skills" / "beta"
    assert (kept_dir / "SKILL.md").is_file()

    flat = _frozen_refusal(scenario, project, scenario.environment, "kept-frozen-refusal")
    assert (
        "kept .claude/skills/beta/SKILL.md: already on disk before this install "
        "(not written by it); delete or move it aside if it is not yours to keep, "
        "then rerun 'apm install --frozen'"
    ) in flat
    assert (kept_dir / "SKILL.md").is_file()
    assert lockfile.read_bytes() == locked

    aside = tmp_path / "moved-aside-beta"
    kept_dir.rename(aside)
    assert (aside / "SKILL.md").is_file()
    before_rerun = ArtifactSnapshotSet.capture({"project": project.root})

    flat = _frozen_refusal(scenario, project, scenario.environment, "kept-remedy-rerun")
    assert ".claude/skills/beta is deployed by this install but not recorded" in flat
    assert " kept " not in flat
    assert "the unrecorded files were removed again" in flat
    assert not kept_dir.exists()
    assert lockfile.read_bytes() == locked
    assert_snapshot_set_unchanged(
        before_rerun, ArtifactSnapshotSet.capture({"project": project.root})
    )


def test_frozen_install_fails_on_unrecorded_skill_without_rewriting_lockfile(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "frozen", apm_binary_path)
    project = scenario.consumers.create("frozen-project", targets=("claude",))
    _add_skill(project.root, "alpha")
    _run_success(
        scenario,
        project,
        _INSTALL_ARGS,
        environment=scenario.environment,
        scenario_id="initial-install",
    )
    _assert_frozen_replays(scenario, project, scenario.environment)
    _add_skill(project.root, "beta")
    _assert_frozen_refusal_and_recovery(
        scenario,
        project,
        scenario.environment,
        PurePosixPath(".claude/skills/beta"),
    )
