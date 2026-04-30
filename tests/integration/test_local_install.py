"""Integration tests for local filesystem path dependency support.

Tests the full install/uninstall/deps workflow using local path dependencies.
These tests create real file structures and invoke CLI commands via subprocess.
"""

import os  # noqa: F401
import shutil
import subprocess
import sys  # noqa: F401
import tempfile  # noqa: F401
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def temp_workspace(tmp_path):
    """Create a workspace with a consumer project and local packages.

    Layout:
        workspace/
        ├── consumer/               ← project that installs local deps
        │   └── apm.yml
        └── packages/
            ├── local-skills/       ← valid APM package
            │   ├── apm.yml
            │   └── instructions/
            │       └── test-skill.instructions.md
            ├── local-prompts/      ← valid APM package with prompts
            │   ├── apm.yml
            │   └── prompts/
            │       └── review.prompt.md
            └── no-manifest/        ← invalid package (no apm.yml/SKILL.md)
                └── README.md
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Consumer project
    consumer = workspace / "consumer"
    consumer.mkdir()
    (consumer / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "consumer-project",
                "version": "1.0.0",
                "dependencies": {"apm": []},
            }
        )
    )
    # Create .github directory for instructions deployment
    (consumer / ".github").mkdir()

    # Local skills package
    skills_pkg = workspace / "packages" / "local-skills"
    skills_pkg.mkdir(parents=True)
    (skills_pkg / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "local-skills",
                "version": "1.0.0",
                "description": "Local test skills package",
            }
        )
    )
    instructions_dir = skills_pkg / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "test-skill.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Test Skill\nThis is a test skill."
    )

    # Local prompts package
    prompts_pkg = workspace / "packages" / "local-prompts"
    prompts_pkg.mkdir(parents=True)
    (prompts_pkg / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "local-prompts",
                "version": "1.0.0",
                "description": "Local test prompts package",
            }
        )
    )
    prompts_dir = prompts_pkg / ".apm" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "review.prompt.md").write_text(
        "---\nmode: agent\n---\nReview this code carefully."
    )

    # Invalid package (no manifest)
    no_manifest = workspace / "packages" / "no-manifest"
    no_manifest.mkdir(parents=True)
    (no_manifest / "README.md").write_text("# No manifest here")

    return workspace


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLocalInstall:
    """Test `apm install ./local/path` workflow."""

    def test_install_local_package_relative_path(self, temp_workspace, apm_command):
        """Install a local package using a relative path."""
        consumer = temp_workspace / "consumer"
        result = subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify apm.yml updated
        with open(consumer / "apm.yml") as f:
            data = yaml.safe_load(f)
        apm_deps = data.get("dependencies", {}).get("apm", [])
        assert "../packages/local-skills" in apm_deps

        # Verify apm_modules populated
        install_dir = consumer / "apm_modules" / "_local" / "local-skills"
        assert install_dir.exists(), "Package not copied to apm_modules/_local/"
        assert (install_dir / "apm.yml").exists()
        assert (install_dir / ".apm" / "instructions" / "test-skill.instructions.md").exists()

        # Verify lockfile
        lock_path = consumer / "apm.lock.yaml"
        assert lock_path.exists(), "Lockfile not created"
        with open(lock_path) as f:
            lock_data = yaml.safe_load(f)
        deps = lock_data.get("dependencies", [])
        # Local deps have source: local
        assert any(d.get("source") == "local" for d in deps), f"No local source in lockfile: {deps}"

    def test_install_local_package_absolute_path(self, temp_workspace, apm_command):
        """Install a local package using an absolute path."""
        consumer = temp_workspace / "consumer"
        abs_path = str(temp_workspace / "packages" / "local-skills")
        result = subprocess.run(
            [apm_command, "install", abs_path],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify apm.yml has the absolute path
        with open(consumer / "apm.yml") as f:
            data = yaml.safe_load(f)
        apm_deps = data.get("dependencies", {}).get("apm", [])
        assert abs_path in apm_deps

    def test_install_local_deploys_instructions(self, temp_workspace, apm_command):
        """Verify that instructions from a local package are deployed to .github/instructions/."""
        consumer = temp_workspace / "consumer"
        result = subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Check instructions deployed
        deployed = consumer / ".github" / "instructions" / "test-skill.instructions.md"
        all_files = list((consumer / ".github").rglob("*"))
        assert deployed.exists(), (
            f"Instructions not deployed. Files in .github/: {all_files}\nstdout: {result.stdout}"
        )

    def test_install_local_package_no_manifest_fails(self, temp_workspace, apm_command):
        """Installing a path with no apm.yml or SKILL.md should fail gracefully."""
        consumer = temp_workspace / "consumer"
        result = subprocess.run(
            [apm_command, "install", "../packages/no-manifest"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Should report the package as not recognizable (validation fails)
        combined = result.stdout + result.stderr
        assert (
            "not accessible" in combined.lower()
            or "doesn't exist" in combined.lower()
            or "no apm.yml" in combined.lower()
            or "failed validation" in combined.lower()
        ), f"Expected failure message. stdout: {result.stdout}, stderr: {result.stderr}"

    def test_install_nonexistent_local_path_fails(self, temp_workspace, apm_command):
        """Installing a non-existent path should fail."""
        consumer = temp_workspace / "consumer"
        result = subprocess.run(
            [apm_command, "install", "./does-not-exist"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert (
            "not accessible" in combined.lower()
            or "doesn't exist" in combined.lower()
            or "no apm.yml" in combined.lower()
            or "failed validation" in combined.lower()
        )

    def test_install_local_from_apm_yml(self, temp_workspace, apm_command):
        """Install local deps declared in apm.yml (bare `apm install`)."""
        consumer = temp_workspace / "consumer"

        # Write apm.yml with local dep
        (consumer / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "consumer-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["../packages/local-skills"],
                    },
                }
            )
        )

        result = subprocess.run(
            [apm_command, "install"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Install failed: {result.stderr}"

        # Verify package installed
        install_dir = consumer / "apm_modules" / "_local" / "local-skills"
        assert install_dir.exists()

    def test_reinstall_copies_fresh(self, temp_workspace, apm_command):
        """Re-running `apm install` on local deps should re-copy (no SHA to cache)."""
        consumer = temp_workspace / "consumer"

        # First install
        subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Modify source file
        skill_file = (
            temp_workspace
            / "packages"
            / "local-skills"
            / ".apm"
            / "instructions"
            / "test-skill.instructions.md"
        )
        skill_file.write_text(
            "---\napplyTo: '**'\n---\n# Updated Test Skill\nThis skill was updated."
        )

        # Re-install
        result = subprocess.run(
            [apm_command, "install"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0

        # Verify updated content in apm_modules
        copied_file = (
            consumer
            / "apm_modules"
            / "_local"
            / "local-skills"
            / ".apm"
            / "instructions"
            / "test-skill.instructions.md"
        )
        assert "Updated Test Skill" in copied_file.read_text()


class TestLocalUninstall:
    """Test `apm uninstall ./local/path` workflow."""

    def test_uninstall_local_package(self, temp_workspace, apm_command):
        """Uninstall a previously installed local package."""
        consumer = temp_workspace / "consumer"

        # Install first
        subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Verify installed
        assert (consumer / "apm_modules" / "_local" / "local-skills").exists()

        # Uninstall
        result = subprocess.run(
            [apm_command, "uninstall", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Uninstall failed: {result.stderr}"

        # Verify removed from apm.yml
        with open(consumer / "apm.yml") as f:
            data = yaml.safe_load(f)
        apm_deps = data.get("dependencies", {}).get("apm", []) or []
        assert "../packages/local-skills" not in apm_deps

        # Verify apm_modules cleaned up
        assert not (consumer / "apm_modules" / "_local" / "local-skills").exists()


class TestLocalDeps:
    """Test `apm deps` with local dependencies."""

    def test_deps_shows_local_packages(self, temp_workspace, apm_command):
        """The `apm deps list` command should list local dependencies."""
        consumer = temp_workspace / "consumer"

        # Install a local package
        subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )

        result = subprocess.run(
            [apm_command, "deps", "list"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"deps list failed: {result.stderr}"
        combined = result.stdout + result.stderr
        # Should mention the local dep somehow
        assert "local-skills" in combined.lower() or "local" in combined.lower(), (
            f"Expected 'local-skills' in deps output: {combined}"
        )


class TestLocalPackMixed:
    """Test that `apm pack` rejects local deps."""

    def test_pack_rejects_with_local_deps(self, temp_workspace, apm_command):
        """apm pack should refuse when apm.yml has local deps."""
        consumer = temp_workspace / "consumer"

        # Write apm.yml with local dep
        (consumer / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "consumer-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["../packages/local-skills"],
                    },
                }
            )
        )

        # Create a valid lockfile via the LockFile API
        from apm_cli.deps.lockfile import LockedDependency as _LD
        from apm_cli.deps.lockfile import LockFile as _LF

        _lock = _LF()
        _lock.add_dependency(
            _LD(
                repo_url="_local/local-skills",
                source="local",
                local_path="../packages/local-skills",
            )
        )
        _lock.write(consumer / "apm.lock.yaml")

        result = subprocess.run(
            [apm_command, "pack"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0 or "local" in combined.lower(), (
            f"Expected pack to reject local deps. stdout: {result.stdout}, stderr: {result.stderr}"
        )


class TestRootProjectPrimitives:
    """Test #714: root project .apm/ integration without a sub-package stub.

    Users should be able to place .apm/ rules directly in their project root
    alongside apm.yml without creating a dummy ./agent/apm.yml workaround.
    """

    def _make_project(self, tmp_path, *, apm_deps=None):
        """Return a project root with .apm/instructions/ and optional deps."""
        project = tmp_path / "project"
        project.mkdir()

        deps_section = {"apm": apm_deps} if apm_deps else {}
        (project / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-project",
                    "version": "1.0.0",
                    "dependencies": deps_section,
                }
            )
        )

        instructions_dir = project / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "local-rules.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Local Rules\nFollow these local rules."
        )

        # Create .claude/rules/ so claude target is auto-detected
        (project / ".claude" / "rules").mkdir(parents=True)
        return project

    def test_root_apm_primitives_deployed_with_no_deps(self, tmp_path, apm_command):
        """root apm.yml with no deps + root .apm/ -> rules deployed.

        Before the fix, apm install returned early with nothing to install
        and never deployed the local .apm/ rules.
        """
        project = self._make_project(tmp_path)

        result = subprocess.run(
            [apm_command, "install"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Install failed:\n{combined}"

        deployed = project / ".claude" / "rules" / "local-rules.md"
        assert deployed.exists(), (
            f"Root .apm/ rules were NOT deployed to .claude/rules/.\nOutput:\n{combined}"
        )
        assert "Local Rules" in deployed.read_text()

    def test_root_apm_primitives_deployed_alongside_external_dep(self, tmp_path, apm_command):
        """root apm.yml with external dep + root .apm/ -> both rule sets deployed.

        This is the exact scenario from #714: external dependencies in apm.yml
        and local .apm/ rules at the root. Before the fix, only the external
        dep's rules were deployed.
        """
        ext_pkg = tmp_path / "ext-pkg"
        ext_pkg.mkdir()
        (ext_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "ext-pkg",
                    "version": "1.0.0",
                }
            )
        )
        ext_instr = ext_pkg / ".apm" / "instructions"
        ext_instr.mkdir(parents=True)
        (ext_instr / "ext-rules.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# External Rules\nFrom external package."
        )

        project = self._make_project(tmp_path, apm_deps=["../ext-pkg"])

        result = subprocess.run(
            [apm_command, "install"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Install failed:\n{combined}"

        deployed_names = {f.name for f in (project / ".claude" / "rules").glob("*.md")}
        assert "local-rules.md" in deployed_names, (
            f"Root .apm/ rule NOT deployed. Files: {deployed_names}\nOutput:\n{combined}"
        )
        assert "ext-rules.md" in deployed_names, (
            f"External dep rule NOT deployed. Files: {deployed_names}\nOutput:\n{combined}"
        )

    def test_workaround_sub_package_still_works(self, tmp_path, apm_command):
        """Old ./agent/apm.yml workaround continues to work (regression guard)."""
        project = tmp_path / "project"
        project.mkdir()

        agent_dir = project / "agent"
        agent_dir.mkdir()
        (agent_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-project-agent",
                    "version": "1.0.0",
                }
            )
        )
        agent_instr = agent_dir / ".apm" / "instructions"
        agent_instr.mkdir(parents=True)
        (agent_instr / "agent-rules.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Agent Rules\nFrom sub-package stub."
        )

        (project / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["./agent"]},
                }
            )
        )
        (project / ".claude" / "rules").mkdir(parents=True)

        result = subprocess.run(
            [apm_command, "install"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Install failed:\n{combined}"
        assert (project / ".claude" / "rules" / "agent-rules.md").exists(), (
            f"Sub-package rules NOT deployed.\nOutput:\n{combined}"
        )

    def test_root_apm_primitives_idempotent(self, tmp_path, apm_command):
        """Running apm install twice with root .apm/ is idempotent."""
        project = self._make_project(tmp_path)

        for run in range(2):
            result = subprocess.run(
                [apm_command, "install"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, f"Run {run + 1} failed:\n{result.stdout + result.stderr}"

        assert (project / ".claude" / "rules" / "local-rules.md").exists()

    def test_root_apm_hooks_deployed(self, tmp_path, apm_command):
        """root .apm/hooks/ is detected and integrated (not just instructions).

        Guards the _ROOT_PRIM_SUBDIRS list: a project that only has .apm/hooks/
        must still enter the integration path and not hit the early-return guard.
        """
        project = tmp_path / "project"
        project.mkdir()

        (project / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-project",
                    "version": "1.0.0",
                }
            )
        )

        hooks_dir = project / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "on-save.json").write_text(
            '{"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "echo saved"}]}]}}'
        )

        # Create .claude/ so claude target is auto-detected
        (project / ".claude").mkdir(parents=True)

        result = subprocess.run(
            [apm_command, "install"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Install failed:\n{combined}"
        # The hook integrator merges into settings.json; confirm it was created
        # or that install did not silently early-return (exit 0 with no output).
        assert "nothing to install" not in combined.lower(), (
            f"Install returned 'nothing to install' — hooks detection guard may "
            f"have triggered early return.\nOutput:\n{combined}"
        )

    def test_root_skill_md_detected(self, tmp_path, apm_command):
        """A root SKILL.md alone triggers the integration path.

        Guards the (project_root / "SKILL.md").exists() branch in the
        root-primitive detection logic.
        """
        project = tmp_path / "project"
        project.mkdir()

        (project / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-project",
                    "version": "1.0.0",
                }
            )
        )
        (project / "SKILL.md").write_text("# My Skill\nThis skill does something useful.")

        # Create .claude/ so claude target is auto-detected
        (project / ".claude").mkdir(parents=True)

        result = subprocess.run(
            [apm_command, "install"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"Install failed:\n{combined}"
        assert "nothing to install" not in combined.lower(), (
            f"Install returned 'nothing to install' — SKILL.md detection may "
            f"have been skipped.\nOutput:\n{combined}"
        )


class TestLocalMixedWithRemote:
    """Test mixing local and remote dependencies."""

    def test_install_local_alongside_remote_in_apm_yml(self, temp_workspace, apm_command):
        """Both local and remote deps in apm.yml should install correctly."""
        consumer = temp_workspace / "consumer"

        # First install local
        result = subprocess.run(
            [apm_command, "install", "../packages/local-skills"],
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"Install local failed: {result.stderr}"

        # Verify local installed
        assert (consumer / "apm_modules" / "_local" / "local-skills").exists()

        # Verify apm.yml has both
        with open(consumer / "apm.yml") as f:
            data = yaml.safe_load(f)
        apm_deps = data.get("dependencies", {}).get("apm", [])
        assert "../packages/local-skills" in apm_deps
