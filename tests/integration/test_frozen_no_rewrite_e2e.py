"""Real-CLI contract for the --frozen under-recording failure (req-lk-006).

``apm install --frozen`` never writes the lockfile it freezes. When the
install deploys a path that ``apm.lock.yaml`` does not record, it exits 1,
names the path, and leaves the lockfile bytes untouched. Deployment has
already happened by then: only the lockfile write is withheld.

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
    assert (project.root / deployed_path / "SKILL.md").is_file()

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
