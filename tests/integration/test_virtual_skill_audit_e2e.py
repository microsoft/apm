"""Real lifecycle evidence for manifestless virtual-skill audit consistency."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_GIT_SOURCE = "git@gitlab.example.invalid:group/virtual-skills.git"
_HTTPS_GIT_SOURCE = "https://gitlab.example.invalid/group/virtual-skills"
_VIRTUAL_PATH = "skills/productivity/grilling"
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--no-fail-fast",
    "--format",
    "json",
)


@dataclass(frozen=True)
class _VirtualSkillLifecycle:
    """Installed virtual-skill project and its isolated command boundary."""

    project_root: Path
    module_root: Path
    environment: dict[str, str]
    runner: ApmLifecycleRunner


def _config_check(result: CommandResult) -> dict[str, object]:
    """Return the public audit payload's config-consistency check."""
    payload = json.loads(result.stdout)
    return next(check for check in payload["checks"] if check["name"] == "config-consistency")


def _run_audit(
    lifecycle: _VirtualSkillLifecycle,
    *,
    expected_returncode: int,
    scenario_id: str,
) -> CommandResult:
    """Run the public audit command for one lifecycle state."""
    return lifecycle.runner.run_sequence(
        (_AUDIT_ARGS,),
        expected_returncodes=(expected_returncode,),
        scenario_id=scenario_id,
        cwd=lifecycle.project_root,
        env=lifecycle.environment,
    )[0]


def _install_virtual_skill_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> _VirtualSkillLifecycle:
    """Install and frozen-replay a real manifestless virtual Claude skill."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "virtual-skill-audit",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()

    source_root = isolated.package_root / "virtual-skills"
    skill_root = source_root / _VIRTUAL_PATH
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            "name: grilling\n"
            "description: Hermetic virtual skill audit fixture\n"
            "---\n"
            "# Grilling\n"
        ),
        encoding="utf-8",
    )
    assert not (source_root / "apm.yml").exists()
    assert not (skill_root / "apm.yml").exists()

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create("virtual-skills", source_tree=source_root)
    commit = repositories.commit(repository, message="seed virtual skill")
    user_git_environment = dict(environment)
    user_git_environment.pop("GIT_CONFIG_GLOBAL")
    for git_environment in (environment, user_git_environment):
        for source in (_GIT_SOURCE, _HTTPS_GIT_SOURCE):
            subprocess.run(
                (
                    "git",
                    "config",
                    "--global",
                    "--add",
                    f"url.{repository.file_url}.insteadOf",
                    source,
                ),
                env=git_environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )

    project = LocalPackageFactory(isolated.work_root).create(
        "virtual-skill-consumer",
        dependencies=(
            {
                "git": _GIT_SOURCE,
                "type": "gitlab",
                "path": _VIRTUAL_PATH,
                "ref": commit.sha,
            },
        ),
        targets=("copilot",),
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    first_install = runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="virtual-skill-first-install",
        cwd=project.root,
        env=environment,
    )[0]
    assert first_install.stdout

    lock = load_yaml(project.root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    locked_data = dependencies[0]
    assert locked_data["is_virtual"] is True
    assert locked_data["virtual_path"] == _VIRTUAL_PATH
    assert locked_data["package_type"] == "claude_skill"
    module_root = (
        LockedDependency.from_dict(locked_data)
        .to_dependency_ref()
        .get_install_path(project.root / "apm_modules")
    )
    assert (module_root / "SKILL.md").is_file()
    assert not (module_root / "apm.yml").exists()

    shutil.rmtree(project.root / "apm_modules")
    frozen_replay = runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy", "--frozen"),),
        expected_returncodes=(0,),
        scenario_id="virtual-skill-frozen-replay",
        cwd=project.root,
        env=environment,
    )[0]
    assert frozen_replay.stdout
    assert (module_root / "SKILL.md").is_file()
    assert not (module_root / "apm.yml").exists()

    return _VirtualSkillLifecycle(
        project_root=project.root,
        module_root=module_root,
        environment=environment,
        runner=runner,
    )


def test_valid_manifestless_virtual_skill_audits_clean(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A supported virtual Claude skill remains clean after frozen replay."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(tmp_path, apm_binary_path)
    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=0,
        scenario_id="valid-virtual-skill-audit",
    )

    config_check = _config_check(audit)
    assert config_check["passed"] is True
    assert config_check["message"] == "No MCP configs to check"


def test_non_virtual_manifestless_package_still_fails_audit(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The filesystem skill shape alone cannot waive a required manifest."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(tmp_path, apm_binary_path)
    lock_path = virtual_skill_lifecycle.project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["is_virtual"] = False
    dump_yaml(lock, lock_path)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="non-virtual-manifestless-audit",
    )

    config_check = _config_check(audit)
    assert config_check["passed"] is False
    assert any("package manifest not found" in detail for detail in config_check["details"])


def test_malformed_virtual_package_still_fails_audit(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An is_virtual lock bit cannot waive an invalid installed package shape."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(tmp_path, apm_binary_path)
    (virtual_skill_lifecycle.module_root / "SKILL.md").unlink()

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="malformed-virtual-skill-audit",
    )

    config_check = _config_check(audit)
    assert config_check["passed"] is False
    assert any("package manifest not found" in detail for detail in config_check["details"])


def test_virtual_skill_exemption_does_not_hide_mcp_config_drift(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The narrow manifest exemption cannot suppress the symmetric MCP diff."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(tmp_path, apm_binary_path)
    lock_path = virtual_skill_lifecycle.project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["mcp_configs"] = {"stale-server": {"name": "stale-server"}}
    dump_yaml(lock, lock_path)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="virtual-skill-mcp-drift-audit",
    )

    config_check = _config_check(audit)
    assert config_check["passed"] is False
    assert config_check["details"] == ["stale-server: in lockfile but not in manifest"]
