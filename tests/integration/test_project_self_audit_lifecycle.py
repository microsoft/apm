"""Required APMLifecycle transitions for a project that audits its own deployed content."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _assert_same_state,
    _audit,
    _instruction,
    _new_scenario,
    _run_success,
    _skill,
)
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_INSTRUCTION_PATHS = {
    "claude": ".claude/rules/project.md",
    "copilot": ".github/instructions/project.instructions.md",
}


def _project_skill_path(target: str, skill_name: str) -> str:
    """Where install would deploy a skill of this name in the project, for one target."""
    profile = KNOWN_TARGETS[target]
    mapping = profile.primitives["skills"]
    root = mapping.deploy_root or profile.root_dir
    return (PurePosixPath(root) / mapping.subdir / skill_name / "SKILL.md").as_posix()


def _failed_checks(payload: dict[str, object]) -> set[str]:
    return {check["name"] for check in payload["checks"] if not check["passed"]}


@pytest.mark.parametrize(
    "includes", ([".apm/instructions/**/*.md"], "auto"), ids=("explicit-includes", "auto-includes")
)
@pytest.mark.parametrize(
    "targets",
    (("claude",), ("claude", "copilot")),
    ids=("single-target", "multiple-targets"),
)
def test_required_project_self_audit_replays_install_without_sibling_skills(
    tmp_path: Path,
    apm_binary_path: Path,
    includes: str | list[str],
    targets: tuple[str, ...],
) -> None:
    scenario = _new_scenario(tmp_path / "project-self-audit", apm_binary_path)
    project = scenario.consumers.create("self-audit-project", targets=targets)
    scenario.consumers.add_instruction(project, "project", _instruction("project"))
    scenario.consumers.add_skill(project, "hello", _skill("hello"))
    manifest = load_yaml(project.manifest_path)
    manifest["includes"] = includes
    dump_yaml(manifest, project.manifest_path)

    instruction_paths = tuple(_INSTRUCTION_PATHS[target] for target in targets)
    sibling_skill_paths = tuple(_project_skill_path(target, "hello") for target in targets)
    capture_args = {
        "targets": targets,
        "config_paths": tuple(PurePosixPath(path) for path in instruction_paths),
    }

    def run(args: tuple[str, ...], scenario_id: str) -> None:
        _run_success(
            scenario, project, args, environment=scenario.environment, scenario_id=scenario_id
        )

    def audit(scenario_id: str, *, expected_returncode: int = 0) -> dict[str, object]:
        _, payload = _audit(
            scenario,
            project,
            environment=scenario.environment,
            expected_returncode=expected_returncode,
            scenario_id=scenario_id,
        )
        return payload

    run(_INSTALL_ARGS, "project-self-install")
    installed = LifecycleStateSnapshot.capture(project.root, **capture_args)
    lock = load_yaml(project.root / "apm.lock.yaml")

    # Install deploys only the project's own .apm content, never the sibling product skill.
    for target, path in zip(targets, instruction_paths, strict=True):
        deployed = (project.root / path).read_bytes()
        if target == "copilot":
            assert deployed == _instruction("project").encode()
        else:
            assert deployed.endswith(b"# project\n")
    for path in sibling_skill_paths:
        assert not (project.root / path).exists(), path
    assert sorted(lock["local_deployed_files"]) == sorted(instruction_paths)

    # A clean install audits clean, and the audit does not mutate the project.
    clean = audit("project-self-audit-after-install")
    assert clean["passed"] is True
    assert clean["summary"]["failed"] == 0
    _assert_same_state(installed, LifecycleStateSnapshot.capture(project.root, **capture_args))

    # Reinstalling keeps the same state and the audit stays clean.
    run(_INSTALL_ARGS, "project-self-reinstall")
    _assert_same_state(installed, LifecycleStateSnapshot.capture(project.root, **capture_args))
    assert audit("project-self-audit-after-reinstall")["passed"] is True

    # Genuine drift still fails: a missing managed file, then a changed one.
    managed = project.root / instruction_paths[0]
    managed_bytes = managed.read_bytes()
    managed.unlink()
    missing = audit("project-self-audit-missing", expected_returncode=1)
    assert "deployed-files-present" in _failed_checks(missing)
    run(_INSTALL_ARGS, "project-self-repair-missing")
    assert managed.read_bytes() == managed_bytes
    assert audit("project-self-audit-repaired-missing")["passed"] is True

    managed.write_text("# tampered\n", encoding="utf-8")
    changed = audit("project-self-audit-changed", expected_returncode=1)
    assert {"content-integrity", "drift"} <= _failed_checks(changed)
    run(_INSTALL_ARGS, "project-self-repair-changed")
    assert audit("project-self-audit-repaired-changed")["passed"] is True
    _assert_same_state(installed, LifecycleStateSnapshot.capture(project.root, **capture_args))
