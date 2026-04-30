"""End-to-end integration tests for the deployed_files manifest system.

Tests the complete lifecycle of deployed_files tracking with real packages:
- Clean filenames (no -apm suffix) after install
- deployed_files recorded in apm.lock after install
- Collision detection (skip when user-authored file exists)
- --force flag overrides collision detection
- Prune removes deployed files for pruned packages
- Uninstall cleans deployed files
- Re-install preserves existing deployed files context

Requires network access and GITHUB_TOKEN/GITHUB_APM_PAT for GitHub API.
"""

import json  # noqa: F401
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# Skip all tests if no GitHub token is available
pytestmark = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project with .github/ for VSCode target detection."""
    project_dir = tmp_path / "deployed-files-test"
    project_dir.mkdir()

    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text(
        "name: deployed-files-test\n"
        "version: 1.0.0\n"
        "description: Test project for deployed_files manifest\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n"
    )

    # Create .github folder so VSCode target is detected
    (project_dir / ".github").mkdir()

    return project_dir


def _run_apm(apm_command, args, cwd, timeout=120):
    """Run an apm CLI command and return the result."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_lockfile(project_dir):
    """Read and parse apm.lock from the project directory."""
    lock_path = project_dir / "apm.lock.yaml"
    if not lock_path.exists():
        return None
    with open(lock_path) as f:
        return yaml.safe_load(f)


def _get_locked_dep(lockfile, key):
    """Get a dependency entry from lockfile by key (repo_url match)."""
    if not lockfile or "dependencies" not in lockfile:
        return None
    deps = lockfile["dependencies"]
    if isinstance(deps, list):
        for entry in deps:
            repo_url = entry.get("repo_url", "")
            virtual_path = entry.get("virtual_path")
            dep_key = f"{repo_url}/{virtual_path}" if virtual_path else repo_url
            if dep_key == key or repo_url == key:  # noqa: PLR1714
                return entry
        return None
    # dict format (shouldn't happen, but be safe)
    return deps.get(key)


# ---------------------------------------------------------------------------
# Clean filename tests
# ---------------------------------------------------------------------------


class TestCleanFilenames:
    """Verify installed files use clean names (no -apm suffix)."""

    def test_prompts_have_clean_names(self, temp_project, apm_command):
        """Prompts should be deployed without -apm suffix."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        prompts_dir = temp_project / ".github" / "prompts"
        if prompts_dir.exists():
            prompt_files = list(prompts_dir.glob("*.prompt.md"))
            for f in prompt_files:
                assert "-apm.prompt.md" not in f.name, f"Prompt {f.name} still uses -apm suffix"

    def test_agents_have_clean_names(self, temp_project, apm_command):
        """Agents should be deployed without -apm suffix."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        agents_dir = temp_project / ".github" / "agents"
        if agents_dir.exists():
            agent_files = list(agents_dir.glob("*.agent.md"))
            for f in agent_files:
                assert "-apm.agent.md" not in f.name, f"Agent {f.name} still uses -apm suffix"


# ---------------------------------------------------------------------------
# deployed_files lockfile tracking
# ---------------------------------------------------------------------------


class TestDeployedFilesInLockfile:
    """Verify deployed_files are recorded in apm.lock after install."""

    def test_lockfile_has_deployed_files_after_install(self, temp_project, apm_command):
        """apm.lock should contain deployed_files for each installed package."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(temp_project)
        assert lockfile is not None, "apm.lock not created"

        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        assert dep is not None, "Dependency not found in lockfile"
        assert "deployed_files" in dep, "deployed_files key missing from lockfile entry"
        assert len(dep["deployed_files"]) > 0, "deployed_files list is empty"

    def test_deployed_files_point_to_existing_files(self, temp_project, apm_command):
        """Every path in deployed_files should exist on disk after install."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(temp_project)
        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        assert dep is not None

        for rel_path in dep["deployed_files"]:
            full_path = temp_project / rel_path
            assert full_path.exists(), f"Deployed file {rel_path} does not exist on disk"

    def test_deployed_files_are_under_github_or_claude(self, temp_project, apm_command):
        """deployed_files should only be under .github/ or .claude/ directories."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(temp_project)
        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        assert dep is not None

        for rel_path in dep["deployed_files"]:
            assert rel_path.startswith(".github/") or rel_path.startswith(".claude/"), (
                f"Deployed file {rel_path} is not under .github/ or .claude/"
            )

    def test_deployed_files_have_clean_names_in_lockfile(self, temp_project, apm_command):
        """deployed_files paths in lockfile should use clean names (no -apm suffix)."""
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(temp_project)
        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        assert dep is not None

        for rel_path in dep["deployed_files"]:
            assert "-apm." not in rel_path, f"Deployed file path {rel_path} still uses -apm suffix"

    def test_skill_deployed_files_tracked(self, temp_project, apm_command):
        """Skill packages should have deployed_files entries for .github/skills/."""
        result = _run_apm(
            apm_command,
            ["install", "anthropics/skills/skills/brand-guidelines"],
            temp_project,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        lockfile = _read_lockfile(temp_project)
        assert lockfile is not None

        # Find the skill dependency in lockfile
        dep = None
        deps = lockfile.get("dependencies", [])
        if isinstance(deps, list):
            for entry in deps:
                repo = entry.get("repo_url", "")
                vpath = entry.get("virtual_path", "")
                if "brand-guidelines" in repo or "brand-guidelines" in vpath:
                    dep = entry
                    break
        else:
            for key, entry in deps.items():
                if "brand-guidelines" in key:
                    dep = entry
                    break

        assert dep is not None, "Skill dependency not found in lockfile"
        assert "deployed_files" in dep, "deployed_files missing for skill"
        skill_paths = [p for p in dep["deployed_files"] if ".github/skills/" in p]
        assert len(skill_paths) > 0, "No skill paths in deployed_files"


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


class TestCollisionDetection:
    """Test that user-authored files are not overwritten on re-install."""

    def test_user_file_not_overwritten_on_reinstall(self, temp_project, apm_command):
        """Pre-existing user-authored file should be preserved on re-install."""
        # First install to get the package
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"First install failed: {result.stderr}\n{result.stdout}"

        # Find a deployed prompt file
        prompts_dir = temp_project / ".github" / "prompts"
        if not prompts_dir.exists():
            pytest.skip("No prompts deployed by sample-package")

        prompt_files = list(prompts_dir.glob("*.prompt.md"))
        if not prompt_files:
            pytest.skip("No prompt files found")

        target_file = prompt_files[0]
        target_name = target_file.name  # noqa: F841

        # Delete the lockfile to clear deployed_files tracking, then create
        # a user-authored file at the same path
        lock_path = temp_project / "apm.lock.yaml"
        lock_path.unlink(missing_ok=True)

        user_content = "# User-authored content - DO NOT OVERWRITE\n"
        target_file.write_text(user_content)

        # Re-install (should detect collision and skip)
        result2 = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result2.returncode == 0, f"Re-install failed: {result2.stderr}\n{result2.stdout}"

        # User content should be preserved
        assert target_file.read_text() == user_content, (
            "User-authored file was overwritten during re-install"
        )

    def test_force_flag_overwrites_collision(self, temp_project, apm_command):
        """--force should overwrite even user-authored files."""
        # First install
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0

        prompts_dir = temp_project / ".github" / "prompts"
        if not prompts_dir.exists():
            pytest.skip("No prompts deployed by sample-package")

        prompt_files = list(prompts_dir.glob("*.prompt.md"))
        if not prompt_files:
            pytest.skip("No prompt files found")

        target_file = prompt_files[0]

        # Delete lockfile to clear tracking, then create user file
        lock_path = temp_project / "apm.lock.yaml"
        lock_path.unlink(missing_ok=True)

        user_content = "# User-authored content\n"
        target_file.write_text(user_content)

        # Re-install with --force
        result2 = _run_apm(
            apm_command,
            ["install", "microsoft/apm-sample-package", "--force"],
            temp_project,
        )
        assert result2.returncode == 0

        # User content should be overwritten
        assert target_file.read_text() != user_content, (
            "--force did not overwrite the user-authored file"
        )


# ---------------------------------------------------------------------------
# Re-install preserves manifest
# ---------------------------------------------------------------------------


class TestReinstallPreservesManifest:
    """Verify that re-install updates deployed_files correctly."""

    def test_reinstall_same_package_updates_lockfile(self, temp_project, apm_command):
        """Re-installing the same package should keep deployed_files in lockfile."""
        # First install
        result1 = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result1.returncode == 0

        lockfile1 = _read_lockfile(temp_project)
        dep1 = _get_locked_dep(lockfile1, "microsoft/apm-sample-package")
        files1 = dep1.get("deployed_files", []) if dep1 else []

        # Second install
        result2 = _run_apm(apm_command, ["install"], temp_project)
        assert result2.returncode == 0

        lockfile2 = _read_lockfile(temp_project)
        dep2 = _get_locked_dep(lockfile2, "microsoft/apm-sample-package")
        files2 = dep2.get("deployed_files", []) if dep2 else []

        # Should be the same set (possibly different order)
        assert sorted(files1) == sorted(files2), (
            f"deployed_files changed after re-install:\n  Before: {files1}\n  After: {files2}"
        )


# ---------------------------------------------------------------------------
# Prune cleans deployed files
# ---------------------------------------------------------------------------


class TestPruneDeployedFiles:
    """Verify that prune removes deployed files for pruned packages."""

    def test_prune_removes_deployed_files(self, temp_project, apm_command):
        """After removing a package from apm.yml and pruning, deployed files should be cleaned."""
        # Install a package
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        # Read deployed files before prune
        lockfile = _read_lockfile(temp_project)
        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        if not dep or not dep.get("deployed_files"):
            pytest.skip("No deployed_files tracked for this package")

        deployed = dep["deployed_files"]
        existing_files = [f for f in deployed if (temp_project / f).exists()]
        assert len(existing_files) > 0, "No deployed files exist on disk"

        # Remove the package from apm.yml
        apm_yml = temp_project / "apm.yml"
        apm_yml.write_text(
            "name: deployed-files-test\n"
            "version: 1.0.0\n"
            "description: Test project\n"
            "dependencies:\n"
            "  apm: []\n"
            "  mcp: []\n"
        )

        # Run prune
        result2 = _run_apm(apm_command, ["prune"], temp_project)
        assert result2.returncode == 0, f"Prune failed: {result2.stderr}\n{result2.stdout}"

        # Verify deployed files were cleaned up
        for rel_path in existing_files:
            full_path = temp_project / rel_path
            assert not full_path.exists(), f"Deployed file {rel_path} was not cleaned up by prune"

    def test_prune_removes_package_from_lockfile(self, temp_project, apm_command):
        """After prune, the pruned package should not be in apm.lock."""
        # Install
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0

        # Remove from apm.yml
        apm_yml = temp_project / "apm.yml"
        apm_yml.write_text("name: deployed-files-test\nversion: 1.0.0\ndependencies:\n  apm: []\n")

        # Prune
        result2 = _run_apm(apm_command, ["prune"], temp_project)
        assert result2.returncode == 0

        # Lockfile should not have the pruned package
        lockfile = _read_lockfile(temp_project)
        if lockfile and "dependencies" in lockfile:
            dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
            assert dep is None, "Pruned package still in apm.lock"


# ---------------------------------------------------------------------------
# Uninstall cleans deployed files
# ---------------------------------------------------------------------------


class TestUninstallDeployedFiles:
    """Verify that uninstall removes deployed files for the package."""

    def test_uninstall_removes_deployed_files(self, temp_project, apm_command):
        """Uninstalling a package should clean up its deployed files."""
        # Install
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

        # Record deployed files
        lockfile = _read_lockfile(temp_project)
        dep = _get_locked_dep(lockfile, "microsoft/apm-sample-package")
        if not dep or not dep.get("deployed_files"):
            pytest.skip("No deployed_files tracked")

        deployed = dep["deployed_files"]
        existing_before = [f for f in deployed if (temp_project / f).exists()]

        # Uninstall
        result2 = _run_apm(apm_command, ["uninstall", "microsoft/apm-sample-package"], temp_project)
        assert result2.returncode == 0, f"Uninstall failed: {result2.stderr}\n{result2.stdout}"

        # Deployed files should be cleaned
        for rel_path in existing_before:
            full_path = temp_project / rel_path
            assert not full_path.exists(), (
                f"Deployed file {rel_path} was not cleaned up by uninstall"
            )

    def test_uninstall_removes_package_dir(self, temp_project, apm_command):
        """Uninstalling should remove the package from apm_modules/."""
        # Install
        result = _run_apm(apm_command, ["install", "microsoft/apm-sample-package"], temp_project)
        assert result.returncode == 0

        pkg_dir = temp_project / "apm_modules" / "microsoft" / "apm-sample-package"
        assert pkg_dir.exists(), "Package not installed"

        # Uninstall
        result2 = _run_apm(apm_command, ["uninstall", "microsoft/apm-sample-package"], temp_project)
        assert result2.returncode == 0

        assert not pkg_dir.exists(), "Package dir not removed after uninstall"
