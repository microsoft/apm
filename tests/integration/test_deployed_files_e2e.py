"""End-to-end integration tests for the deployed_files manifest system.

The suite uses an owned local Git origin exposed as
``microsoft/apm-sample-package`` so its package lifecycle assertions remain
hermetic while preserving GitHub-shaped lockfile provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.hermetic_packaged_sample import (
    DEPENDENCY,
    HermeticPackagedSample,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _read_lockfile(project_dir: Path) -> dict[str, object] | None:
    """Read and parse apm.lock from the project directory."""
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with lock_path.open(encoding="utf-8") as handle:
        lockfile = yaml.safe_load(handle)
    assert lockfile is None or isinstance(lockfile, dict)
    return lockfile


def _get_locked_dep(
    lockfile: dict[str, object] | None,
    key: str,
) -> dict[str, object] | None:
    """Get a dependency entry from lockfile by its repository URL."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    dependencies = lockfile["dependencies"]
    if isinstance(dependencies, list):
        for entry in dependencies:
            assert isinstance(entry, dict)
            repo_url = entry.get("repo_url", "")
            virtual_path = entry.get("virtual_path")
            dep_key = f"{repo_url}/{virtual_path}" if virtual_path else repo_url
            if key in (dep_key, repo_url):
                return entry
        return None
    assert isinstance(dependencies, dict)
    entry = dependencies.get(key)
    assert entry is None or isinstance(entry, dict)
    return entry


def _install(
    packaged_sample: HermeticPackagedSample,
    *,
    scenario_id: str,
    args: tuple[str, ...] = (),
) -> None:
    """Install the fixture package and retain full command evidence on failure."""
    result = packaged_sample.run(("install", *args), scenario_id=scenario_id)
    assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"


def _remove_dependency(project_dir: Path) -> None:
    """Rewrite the consumer manifest without its package dependency."""
    manifest_path = project_dir / "apm.yml"
    manifest = load_yaml(manifest_path)
    assert isinstance(manifest, dict)
    manifest.pop("dependencies", None)
    dump_yaml(manifest, manifest_path)


class TestCleanFilenames:
    """Verify installed files use clean names without an -apm suffix."""

    def test_prompts_have_clean_names(self, packaged_sample: HermeticPackagedSample) -> None:
        """Prompts should be deployed without -apm suffix."""
        _install(packaged_sample, scenario_id="deployed-files-clean-prompts")

        prompts_dir = packaged_sample.project.root / ".github" / "prompts"
        prompt_files = list(prompts_dir.glob("*.prompt.md"))
        assert prompt_files, "Fixture package must deploy a prompt"
        for path in prompt_files:
            assert "-apm.prompt.md" not in path.name, f"Prompt {path.name} still uses -apm suffix"

    def test_agents_have_clean_names(self, packaged_sample: HermeticPackagedSample) -> None:
        """Agents should be deployed without -apm suffix."""
        _install(packaged_sample, scenario_id="deployed-files-clean-agents")

        agents_dir = packaged_sample.project.root / ".github" / "agents"
        agent_files = list(agents_dir.glob("*.agent.md"))
        assert agent_files, "Fixture package must deploy an agent"
        for path in agent_files:
            assert "-apm.agent.md" not in path.name, f"Agent {path.name} still uses -apm suffix"


class TestDeployedFilesInLockfile:
    """Verify deployed_files are recorded in apm.lock after install."""

    def test_lockfile_has_deployed_files_after_install(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """apm.lock should contain deployed_files for each installed package."""
        _install(packaged_sample, scenario_id="deployed-files-lock-recorded")

        lockfile = _read_lockfile(packaged_sample.project.root)
        assert lockfile is not None, "apm.lock not created"
        dependency = _get_locked_dep(lockfile, DEPENDENCY)
        assert dependency is not None, "Dependency not found in lockfile"
        assert "deployed_files" in dependency, "deployed_files key missing from lockfile entry"
        deployed_files = dependency["deployed_files"]
        assert isinstance(deployed_files, list)
        assert deployed_files, "deployed_files list is empty"

    def test_deployed_files_point_to_existing_files(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Every path in deployed_files should exist on disk after install."""
        _install(packaged_sample, scenario_id="deployed-files-lock-paths")

        project_dir = packaged_sample.project.root
        dependency = _get_locked_dep(_read_lockfile(project_dir), DEPENDENCY)
        assert dependency is not None
        deployed_files = dependency["deployed_files"]
        assert isinstance(deployed_files, list)
        for rel_path in deployed_files:
            assert isinstance(rel_path, str)
            assert (project_dir / rel_path).exists(), (
                f"Deployed file {rel_path} does not exist on disk"
            )

    def test_deployed_files_are_under_known_target_roots(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """deployed_files must land under one of the known target roots."""
        _install(packaged_sample, scenario_id="deployed-files-target-roots")

        dependency = _get_locked_dep(_read_lockfile(packaged_sample.project.root), DEPENDENCY)
        assert dependency is not None
        deployed_files = dependency["deployed_files"]
        assert isinstance(deployed_files, list)
        allowed_roots = (".github/", ".claude/", ".agents/")
        for rel_path in deployed_files:
            assert isinstance(rel_path, str)
            assert rel_path.startswith(allowed_roots), (
                f"Deployed file {rel_path} is not under any of {allowed_roots}"
            )

    def test_deployed_files_have_clean_names_in_lockfile(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """deployed_files paths in lockfile should use clean names."""
        _install(packaged_sample, scenario_id="deployed-files-clean-lock-paths")

        dependency = _get_locked_dep(_read_lockfile(packaged_sample.project.root), DEPENDENCY)
        assert dependency is not None
        deployed_files = dependency["deployed_files"]
        assert isinstance(deployed_files, list)
        for rel_path in deployed_files:
            assert isinstance(rel_path, str)
            assert "-apm." not in rel_path, f"Deployed file path {rel_path} still uses -apm suffix"

    def test_skill_deployed_files_tracked(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Skill packages should record deployed_files under .agents/skills/."""
        _install(packaged_sample, scenario_id="deployed-files-skill-tracking")

        lockfile = _read_lockfile(packaged_sample.project.root)
        assert lockfile is not None
        dependency = _get_locked_dep(lockfile, DEPENDENCY)
        assert dependency is not None, "Skill dependency not found in lockfile"
        assert "deployed_files" in dependency, "deployed_files missing for skill"
        deployed_files = dependency["deployed_files"]
        assert isinstance(deployed_files, list)
        skill_paths = [path for path in deployed_files if ".agents/skills/" in path]
        assert skill_paths, "No skill paths in deployed_files"


class TestCollisionDetection:
    """Test that user-authored files are not overwritten on re-install."""

    def test_user_file_not_overwritten_on_reinstall(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Pre-existing user-authored file should be preserved on re-install."""
        _install(packaged_sample, scenario_id="deployed-files-collision-initial")

        project_dir = packaged_sample.project.root
        prompt_files = list((project_dir / ".github" / "prompts").glob("*.prompt.md"))
        assert prompt_files, "Fixture package must deploy a prompt"
        target_file = prompt_files[0]

        (project_dir / "apm.lock.yaml").unlink()
        user_content = "# User-authored content - DO NOT OVERWRITE\n"
        target_file.write_text(user_content, encoding="utf-8")

        _install(packaged_sample, scenario_id="deployed-files-collision-reinstall")
        assert target_file.read_text(encoding="utf-8") == user_content, (
            "User-authored file was overwritten during re-install"
        )

    def test_force_flag_overwrites_collision(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """--force should overwrite even user-authored files."""
        _install(packaged_sample, scenario_id="deployed-files-force-initial")

        project_dir = packaged_sample.project.root
        prompt_files = list((project_dir / ".github" / "prompts").glob("*.prompt.md"))
        assert prompt_files, "Fixture package must deploy a prompt"
        target_file = prompt_files[0]

        (project_dir / "apm.lock.yaml").unlink()
        user_content = "# User-authored content\n"
        target_file.write_text(user_content, encoding="utf-8")

        _install(
            packaged_sample,
            scenario_id="deployed-files-force-reinstall",
            args=("--force",),
        )
        assert target_file.read_text(encoding="utf-8") != user_content, (
            "--force did not overwrite the user-authored file"
        )


class TestReinstallPreservesManifest:
    """Verify that re-install updates deployed_files correctly."""

    def test_reinstall_same_package_updates_lockfile(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Re-installing the same package should keep deployed_files in apm.lock."""
        _install(packaged_sample, scenario_id="deployed-files-reinstall-initial")

        project_dir = packaged_sample.project.root
        first_dependency = _get_locked_dep(_read_lockfile(project_dir), DEPENDENCY)
        assert first_dependency is not None
        first_files = first_dependency.get("deployed_files", [])
        assert isinstance(first_files, list)

        result = packaged_sample.run(("install",), scenario_id="deployed-files-reinstall-replay")
        assert result.returncode == 0, f"Re-install failed: {result.stderr}\n{result.stdout}"

        second_dependency = _get_locked_dep(_read_lockfile(project_dir), DEPENDENCY)
        assert second_dependency is not None
        second_files = second_dependency.get("deployed_files", [])
        assert isinstance(second_files, list)
        assert sorted(first_files) == sorted(second_files), (
            f"deployed_files changed after re-install:\n"
            f"  Before: {first_files}\n"
            f"  After: {second_files}"
        )


class TestPruneDeployedFiles:
    """Verify that prune removes deployed files for pruned packages."""

    def test_prune_removes_deployed_files(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Pruning a removed package should clean its deployed files."""
        _install(packaged_sample, scenario_id="deployed-files-prune-initial")

        project_dir = packaged_sample.project.root
        dependency = _get_locked_dep(_read_lockfile(project_dir), DEPENDENCY)
        assert dependency is not None
        deployed_files = dependency.get("deployed_files", [])
        assert isinstance(deployed_files, list)
        existing_files = [
            rel_path
            for rel_path in deployed_files
            if isinstance(rel_path, str) and (project_dir / rel_path).exists()
        ]
        assert existing_files, "No deployed files exist on disk"

        _remove_dependency(project_dir)

        result = packaged_sample.run(("prune",), scenario_id="deployed-files-prune")
        assert result.returncode == 0, f"Prune failed: {result.stderr}\n{result.stdout}"
        for rel_path in existing_files:
            assert not (project_dir / rel_path).exists(), (
                f"Deployed file {rel_path} was not cleaned up by prune"
            )

    def test_prune_removes_package_from_lockfile(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """After prune, the pruned package should not be in apm.lock."""
        _install(packaged_sample, scenario_id="deployed-files-prune-lock-initial")

        project_dir = packaged_sample.project.root
        _remove_dependency(project_dir)

        result = packaged_sample.run(("prune",), scenario_id="deployed-files-prune-lock")
        assert result.returncode == 0, f"Prune failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(project_dir)
        if lockfile and "dependencies" in lockfile:
            assert _get_locked_dep(lockfile, DEPENDENCY) is None, "Pruned package still in apm.lock"


class TestUninstallDeployedFiles:
    """Verify that uninstall removes deployed files for the package."""

    def test_uninstall_removes_deployed_files(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Uninstalling a package should clean up its deployed files."""
        _install(packaged_sample, scenario_id="deployed-files-uninstall-initial")

        project_dir = packaged_sample.project.root
        dependency = _get_locked_dep(_read_lockfile(project_dir), DEPENDENCY)
        assert dependency is not None
        deployed_files = dependency.get("deployed_files", [])
        assert isinstance(deployed_files, list)
        existing_before = [
            rel_path
            for rel_path in deployed_files
            if isinstance(rel_path, str) and (project_dir / rel_path).exists()
        ]

        result = packaged_sample.run(
            ("uninstall", DEPENDENCY),
            scenario_id="deployed-files-uninstall",
        )
        assert result.returncode == 0, f"Uninstall failed: {result.stderr}\n{result.stdout}"
        for rel_path in existing_before:
            assert not (project_dir / rel_path).exists(), (
                f"Deployed file {rel_path} was not cleaned up by uninstall"
            )

    def test_uninstall_removes_package_dir(
        self,
        packaged_sample: HermeticPackagedSample,
    ) -> None:
        """Uninstalling should remove the package from apm_modules/."""
        _install(packaged_sample, scenario_id="deployed-files-uninstall-dir-initial")

        project_dir = packaged_sample.project.root
        package_dir = project_dir / "apm_modules" / "microsoft" / "apm-sample-package"
        assert package_dir.exists(), "Package not installed"

        result = packaged_sample.run(
            ("uninstall", DEPENDENCY),
            scenario_id="deployed-files-uninstall-dir",
        )
        assert result.returncode == 0, f"Uninstall failed: {result.stderr}\n{result.stdout}"
        assert not package_dir.exists(), "Package dir not removed after uninstall"
