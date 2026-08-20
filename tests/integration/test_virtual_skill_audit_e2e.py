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
from tests.integration.test_required_lifecycle_state_machine import _assert_same_state
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
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

    isolated: IsolatedApmEnvironment
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    skill_root: Path
    project_root: Path
    module_root: Path
    environment: dict[str, str]
    runner: ApmLifecycleRunner


def _audit_payload(result: CommandResult) -> dict[str, object]:
    """Return the parsed public audit payload."""
    return json.loads(result.stdout)


def _config_check(result: CommandResult) -> dict[str, object]:
    """Return the public audit payload's config-consistency check."""
    payload = _audit_payload(result)
    return next(check for check in payload["checks"] if check["name"] == "config-consistency")


def _drift_check(result: CommandResult) -> dict[str, object]:
    """Return the public audit payload's drift check."""
    payload = _audit_payload(result)
    return next(check for check in payload["checks"] if check["name"] == "drift")


def _capture_state(lifecycle: _VirtualSkillLifecycle) -> LifecycleStateSnapshot:
    """Capture exact and semantic state for the copilot target."""
    return LifecycleStateSnapshot.capture(
        lifecycle.project_root,
        targets=("copilot",),
    )


def _evict_live_modules_and_cache(lifecycle: _VirtualSkillLifecycle) -> None:
    """Delete live modules plus the isolated APM cache to mimic setup-only CI."""
    modules_root = lifecycle.project_root / "apm_modules"
    if modules_root.exists():
        shutil.rmtree(modules_root)
    if lifecycle.isolated.cache_root.exists():
        shutil.rmtree(lifecycle.isolated.cache_root)
    lifecycle.isolated.cache_root.mkdir(parents=True, exist_ok=True)


def _rewrite_dependency_commit(project_root: Path, new_sha: str) -> None:
    """Point the manifest and lockfile at a new locked commit."""
    manifest_path = project_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["dependencies"]["apm"][0]["ref"] = new_sha
    dump_yaml(manifest, manifest_path)

    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["resolved_commit"] = new_sha
    lock["dependencies"][0]["resolved_ref"] = new_sha
    dump_yaml(lock, lock_path)


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
    *,
    rewarm_live_modules: bool = True,
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
    if rewarm_live_modules:
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
        isolated=isolated,
        repositories=repositories,
        repository=repository,
        skill_root=skill_root,
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


def test_valid_manifestless_virtual_skill_audits_clean_from_cold_cache(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """`apm audit --ci` self-hydrates a cold cache without mutating the checkout."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(
        tmp_path,
        apm_binary_path,
        rewarm_live_modules=False,
    )
    _evict_live_modules_and_cache(virtual_skill_lifecycle)
    before = _capture_state(virtual_skill_lifecycle)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=0,
        scenario_id="cold-cache-valid-virtual-skill-audit",
    )

    payload = _audit_payload(audit)
    config_check = _config_check(audit)
    drift_check = _drift_check(audit)
    after = _capture_state(virtual_skill_lifecycle)
    assert config_check["passed"] is True
    assert config_check["message"] == "No MCP configs to check"
    assert drift_check["passed"] is True
    assert "skipped" not in drift_check["message"].lower()
    assert payload["drift"]["drift"] == []
    assert not virtual_skill_lifecycle.module_root.exists()
    _assert_same_state(before, after)


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
    assert config_check["details"]


def test_non_virtual_manifestless_package_still_fails_audit_from_cold_cache(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Cold-cache self-hydration must not trust a non-virtual lock row."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(
        tmp_path,
        apm_binary_path,
        rewarm_live_modules=False,
    )
    lock_path = virtual_skill_lifecycle.project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["is_virtual"] = False
    dump_yaml(lock, lock_path)
    _evict_live_modules_and_cache(virtual_skill_lifecycle)
    before = _capture_state(virtual_skill_lifecycle)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="cold-cache-non-virtual-manifestless-audit",
    )

    after = _capture_state(virtual_skill_lifecycle)
    config_check = _config_check(audit)
    drift_check = _drift_check(audit)
    assert config_check["passed"] is False
    assert config_check["details"]
    assert "skipped" not in drift_check["message"].lower()
    assert not virtual_skill_lifecycle.module_root.exists()
    _assert_same_state(before, after)


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


def test_wrong_virtual_package_type_still_fails_audit_from_cold_cache(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Cold-cache self-hydration must not trust malformed lock package_type rows."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(
        tmp_path,
        apm_binary_path,
        rewarm_live_modules=False,
    )
    lock_path = virtual_skill_lifecycle.project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["package_type"] = "not-a-real-package-type"
    dump_yaml(lock, lock_path)
    _evict_live_modules_and_cache(virtual_skill_lifecycle)
    before = _capture_state(virtual_skill_lifecycle)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="cold-cache-wrong-package-type-audit",
    )

    after = _capture_state(virtual_skill_lifecycle)
    config_check = _config_check(audit)
    drift_check = _drift_check(audit)
    assert config_check["passed"] is False
    assert config_check["details"]
    assert "skipped" not in drift_check["message"].lower()
    assert not virtual_skill_lifecycle.module_root.exists()
    _assert_same_state(before, after)


def test_cold_cache_malformed_virtual_skill_bytes_fail_closed(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Cold-cache self-hydration must reject malformed bytes fetched from a locked commit."""
    virtual_skill_lifecycle = _install_virtual_skill_lifecycle(
        tmp_path,
        apm_binary_path,
        rewarm_live_modules=False,
    )
    (virtual_skill_lifecycle.repository.worktree / _VIRTUAL_PATH / "SKILL.md").unlink()
    malformed_commit = virtual_skill_lifecycle.repositories.commit(
        virtual_skill_lifecycle.repository,
        message="remove skill marker",
    )
    _rewrite_dependency_commit(
        virtual_skill_lifecycle.project_root,
        malformed_commit.sha,
    )
    _evict_live_modules_and_cache(virtual_skill_lifecycle)
    before = _capture_state(virtual_skill_lifecycle)

    audit = _run_audit(
        virtual_skill_lifecycle,
        expected_returncode=1,
        scenario_id="cold-cache-malformed-virtual-skill-audit",
    )

    after = _capture_state(virtual_skill_lifecycle)
    config_check = _config_check(audit)
    drift_check = _drift_check(audit)
    assert config_check["passed"] is False
    assert config_check["details"]
    assert "skipped" not in drift_check["message"].lower()
    assert not virtual_skill_lifecycle.module_root.exists()
    _assert_same_state(before, after)


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
