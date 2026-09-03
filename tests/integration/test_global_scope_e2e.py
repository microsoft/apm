"""Integration tests for the --global / -g scoped installation feature.

Tests the user-scope installation lifecycle end-to-end:
- Directory structure creation under ~/.apm/
- Manifest and lockfile placement at user scope
- Install and uninstall with --global flag
- Cross-platform path resolution (HOME vs USERPROFILE)
- Warning output for unsupported targets

These tests override HOME (and USERPROFILE on Windows) to use a temporary
directory so they are safe to run without affecting the real user home.
They do NOT require network access -- they validate scope plumbing, path
resolution, and CLI output using local fixtures only.
"""

import json
import os
import platform  # noqa: F401
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path):
    """Create an isolated home directory for user-scope tests.

    Sets HOME (Unix) and USERPROFILE (Windows) so that ``Path.home()``
    inside subprocesses resolves to a temporary directory.
    """
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    # Mark the home dir as a copilot harness so install --global passes
    # target detection (post-#1154 the bare directory is no longer a signal).
    (home_dir / ".github").mkdir()
    (home_dir / ".github" / "copilot-instructions.md").write_text("# test\n")
    return home_dir


def _env_with_home(fake_home):
    """Return an env dict with HOME/USERPROFILE pointing to *fake_home*."""
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    return env


def _run_apm(apm_binary_path, args, cwd, fake_home, timeout=60):
    """Run an apm CLI command with an overridden home directory."""
    return subprocess.run(
        [apm_binary_path] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env_with_home(fake_home),
    )


def _write_targeted_package(
    root: Path,
    name: str,
    primitive_path: str,
    filename: str,
    content: str,
) -> Path:
    """Create one local package with a primitive for a selected global target."""
    package = root / name
    package.mkdir()
    (package / "apm.yml").write_text(
        yaml.dump({"name": name, "version": "1.0.0", "description": f"{name} fixture"}),
        encoding="utf-8",
    )
    primitive_dir = package / primitive_path
    primitive_dir.mkdir(parents=True)
    (primitive_dir / filename).write_text(content, encoding="utf-8")
    return package


@pytest.fixture
def local_package(tmp_path):
    """Create a minimal local APM package for testing global install.

    Layout:
        local-pkg/
        +-- apm.yml
        +-- .apm/
            +-- instructions/
                +-- test.instructions.md
    """
    pkg = tmp_path / "local-pkg"
    pkg.mkdir()
    (pkg / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "local-pkg",
                "version": "1.0.0",
                "description": "Test package for global scope",
            }
        )
    )
    instructions_dir = pkg / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "test.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Test instruction\nTest content."
    )
    return pkg


@pytest.fixture
def opencode_package(tmp_path):
    """Create a local package with global and scoped instructions plus a skill."""
    pkg = tmp_path / "opencode-package"
    pkg.mkdir()
    (pkg / "apm.yml").write_text("name: opencode-package\nversion: 1.0.0\n", encoding="utf-8")
    (pkg / "SKILL.md").write_text("# OpenCode package\n", encoding="utf-8")
    instructions = pkg / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "global.instructions.md").write_text(
        "---\ndescription: Global marker\n---\nGLOBAL_OPENCODE_MARKER\n",
        encoding="utf-8",
    )
    (instructions / "python.instructions.md").write_text(
        "---\napplyTo: '**/*.py'\ndescription: Python marker\n---\nSCOPED_OPENCODE_MARKER\n",
        encoding="utf-8",
    )
    skill = pkg / ".apm" / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Reviewer\n", encoding="utf-8")
    return pkg


# ---------------------------------------------------------------------------
# User-scope directory creation
# ---------------------------------------------------------------------------


class TestGlobalDirectoryCreation:
    """Verify that --global creates ~/.apm/ and its children."""

    def test_global_flag_creates_apm_dir(self, apm_binary_path, fake_home):
        """apm install --global should create ~/.apm/ even when the command
        ultimately fails (e.g. no manifest and no packages)."""
        result = _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)

        apm_dir = fake_home / ".apm"
        assert apm_dir.is_dir(), (
            f"~/.apm/ not created. stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_global_flag_creates_modules_subdir(self, apm_binary_path, fake_home):
        """apm install --global should create ~/.apm/apm_modules/."""
        _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)

        modules = fake_home / ".apm" / "apm_modules"
        assert modules.is_dir(), "~/.apm/apm_modules/ not created"

    def test_short_flag_g_creates_apm_dir(self, apm_binary_path, fake_home):
        """-g short flag should behave identically to --global."""
        _run_apm(apm_binary_path, ["install", "-g"], fake_home, fake_home)

        assert (fake_home / ".apm").is_dir(), "-g did not create ~/.apm/"
        assert (fake_home / ".apm" / "apm_modules").is_dir()

    def test_directory_creation_is_idempotent(self, apm_binary_path, fake_home):
        """Running --global twice should not raise or corrupt the directory."""
        _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)
        _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)

        assert (fake_home / ".apm").is_dir()
        assert (fake_home / ".apm" / "apm_modules").is_dir()


# ---------------------------------------------------------------------------
# CLI output / warnings
# ---------------------------------------------------------------------------


class TestGlobalScopeOutput:
    """Verify CLI output when using --global."""

    def test_shows_user_scope_info(self, apm_binary_path, fake_home):
        """Install --global should display user scope info message."""
        result = _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)
        combined = result.stdout + result.stderr
        assert "user scope" in combined.lower() or "~/.apm/" in combined, (
            f"Missing scope info in output: {combined}"
        )

    def test_warns_about_unsupported_targets(self, apm_binary_path, fake_home):
        """Install --global should warn about targets that lack user-scope support."""
        result = _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)
        combined = result.stdout + result.stderr
        assert "cursor" in combined.lower(), f"Missing cursor warning in output: {combined}"

    def test_uninstall_global_shows_scope_info(self, apm_binary_path, fake_home):
        """Uninstall --global should mention user scope in output."""
        # Create a minimal manifest so uninstall doesn't fail on missing apm.yml
        apm_dir = fake_home / ".apm"
        apm_dir.mkdir(parents=True, exist_ok=True)
        (apm_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "global-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["test/pkg"]},
                }
            )
        )

        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", "test/pkg"],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "user scope" in combined.lower(), (
            f"Missing scope info in uninstall output: {combined}"
        )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestGlobalErrorHandling:
    """Verify error paths for --global installs."""

    def test_no_manifest_no_packages_errors(self, apm_binary_path, fake_home):
        """--global without packages and without ~/.apm/apm.yml should fail."""
        result = _run_apm(apm_binary_path, ["install", "--global"], fake_home, fake_home)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        # The error message includes the full path which may be line-wrapped
        # by Rich, so check for the key parts separately
        assert ".apm" in combined and "found" in combined.lower(), (
            f"Error should mention missing manifest: {combined}"
        )

    def test_uninstall_global_no_manifest_errors(self, apm_binary_path, fake_home):
        """Uninstall --global without ~/.apm/apm.yml should fail."""
        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", "test/pkg"],
            fake_home,
            fake_home,
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert ".apm" in combined and ("apm.yml" in combined or "found" in combined.lower()), (
            f"Error should mention missing manifest: {combined}"
        )


# ---------------------------------------------------------------------------
# Manifest creation and placement
# ---------------------------------------------------------------------------


class TestGlobalManifestPlacement:
    """Verify that manifest/lockfile are written under ~/.apm/."""

    def test_auto_bootstrap_creates_user_manifest(self, apm_binary_path, fake_home, local_package):
        """Installing a local package with --global auto-creates ~/.apm/apm.yml."""
        result = _run_apm(
            apm_binary_path,
            ["install", "--global", str(local_package)],
            fake_home,
            fake_home,
        )

        user_manifest = fake_home / ".apm" / "apm.yml"
        assert user_manifest.exists(), (
            f"~/.apm/apm.yml not created. stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        data = yaml.safe_load(user_manifest.read_text())
        assert "dependencies" in data
        apm_deps = data.get("dependencies", {}).get("apm", [])
        assert any(str(local_package) in str(d) for d in apm_deps), (
            f"Package not recorded in manifest: {apm_deps}"
        )

        # Regression guard for #937: manifest entry alone is not enough --
        # the package contents must actually be deployed under ~/.apm/.
        # Previously a USER-scope guard in sources.py / phases/resolve.py
        # silently dropped local refs, leaving the user with a poisoned
        # manifest and zero deployed content.
        cached_pkg = (
            fake_home
            / ".apm"
            / "apm_modules"
            / "_local"
            / local_package.name
            / ".apm"
            / "instructions"
            / "test.instructions.md"
        )
        assert cached_pkg.exists(), (
            f"Local package content not deployed under ~/.apm/apm_modules/_local/. "
            f"Looked for: {cached_pkg}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_user_manifest_does_not_pollute_cwd(self, apm_binary_path, fake_home, local_package):
        """--global must not create apm.yml in the working directory."""
        work_dir = fake_home / "workdir"
        work_dir.mkdir()

        _run_apm(
            apm_binary_path,
            ["install", "--global", str(local_package)],
            work_dir,
            fake_home,
        )

        assert not (work_dir / "apm.yml").exists(), (
            "apm.yml was incorrectly created in the working directory"
        )

    def test_lockfile_placed_under_user_dir(self, apm_binary_path, fake_home, local_package):
        """Lockfile should be created under ~/.apm/, not in the working directory."""
        work_dir = fake_home / "workdir"
        work_dir.mkdir()

        result = _run_apm(  # noqa: F841
            apm_binary_path,
            ["install", "--global", str(local_package)],
            work_dir,
            fake_home,
        )

        # Lockfile should NOT be in the working directory regardless of outcome
        assert not (work_dir / "apm.lock.yaml").exists(), (
            "Lockfile was incorrectly created in the working directory"
        )
        assert not (work_dir / "apm.lock").exists(), (
            "Legacy lockfile was incorrectly created in the working directory"
        )

        # If a lockfile was created, it must be under ~/.apm/
        user_lockfile = fake_home / ".apm" / "apm.lock.yaml"
        if user_lockfile.exists():
            # Sanity: should be parseable YAML
            data = yaml.safe_load(user_lockfile.read_text())
            assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Cross-platform path resolution
# ---------------------------------------------------------------------------


class TestCrossPlatformPaths:
    """Verify path resolution works on the current platform."""

    def test_home_based_paths_are_absolute(self, apm_binary_path, fake_home):
        """All user-scope paths should resolve to absolute paths."""
        from unittest.mock import patch

        from apm_cli.core.scope import (
            InstallScope,
            get_apm_dir,
            get_deploy_root,
            get_lockfile_dir,
            get_manifest_path,
            get_modules_dir,
        )

        with patch.object(Path, "home", return_value=fake_home):
            for fn in [
                get_apm_dir,
                get_deploy_root,
                get_lockfile_dir,
                get_manifest_path,
                get_modules_dir,
            ]:
                result = fn(InstallScope.USER)
                assert result.is_absolute(), (
                    f"{fn.__name__}(USER) returned non-absolute path: {result}"
                )

    def test_forward_slash_paths_on_all_platforms(self, apm_binary_path, fake_home):
        """User-scope paths should use forward slashes (POSIX) when
        stored as strings, matching the lockfile convention."""
        from unittest.mock import patch

        from apm_cli.core.scope import InstallScope, get_apm_dir

        with patch.object(Path, "home", return_value=fake_home):
            apm_dir = get_apm_dir(InstallScope.USER)
            posix_str = apm_dir.as_posix()
            # Should not contain backslashes (even on Windows the as_posix()
            # call should convert them)
            assert "\\" not in posix_str, f"Path contains backslashes: {posix_str}"

    def test_user_root_strings_are_relative(self):
        """TargetProfile user_root_dir values should be relative paths starting
        with a dot (or None for targets that use root_dir at user scope)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        for name, profile in KNOWN_TARGETS.items():
            if profile.user_root_dir is not None:
                assert profile.user_root_dir.startswith("."), (
                    f"{name} user_root_dir does not start with '.': {profile.user_root_dir}"
                )


# ---------------------------------------------------------------------------
# Uninstall lifecycle (global scope)
# ---------------------------------------------------------------------------


class TestGlobalGeminiScope:
    """Verify user-scope install/uninstall deploys to ~/.gemini/."""

    def test_global_install_creates_gemini_dirs(self, apm_binary_path, fake_home, local_package):
        """--global should deploy primitives to ~/.gemini/ when .gemini/ exists."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        result = _run_apm(
            apm_binary_path,
            ["install", "--global", str(local_package)],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "gemini" in combined.lower(), f"Gemini not mentioned in output: {combined}"

    def test_global_install_mentions_gemini_full_support(self, apm_binary_path, fake_home):
        """--global output should list gemini as fully supported."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        result = _run_apm(
            apm_binary_path,
            ["install", "--global"],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "gemini" in combined.lower(), f"Gemini not in scope support message: {combined}"

    def test_global_uninstall_runs_in_user_scope(self, apm_binary_path, fake_home, local_package):
        """Uninstall --global with .gemini/ present operates in user scope."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        _run_apm(
            apm_binary_path,
            ["install", "--global", str(local_package)],
            fake_home,
            fake_home,
        )

        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", "local-pkg"],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "user scope" in combined.lower(), f"Uninstall did not run in user scope: {combined}"


class TestGlobalOpenCodeScope:
    """Verify the native OpenCode user-scope lifecycle through the installed CLI."""

    @pytest.mark.lifecycle_smoke
    def test_opencode_skill_and_scoped_instruction_lifecycle(
        self, apm_binary_path, fake_home, opencode_package
    ):
        """OpenCode preserves native project and user-scope ownership."""
        project_root = fake_home.parent / "project"
        project_root.mkdir()
        (project_root / ".opencode").mkdir()
        project_install = _run_apm(
            apm_binary_path,
            ["install", str(opencode_package), "--target", "opencode"],
            project_root,
            fake_home,
        )
        assert project_install.returncode == 0, project_install.stdout + project_install.stderr
        project_skill = project_root / ".agents" / "skills" / "reviewer" / "SKILL.md"
        assert project_skill.is_file()

        project_compile = _run_apm(
            apm_binary_path,
            ["compile", "--target", "opencode"],
            project_root,
            fake_home,
        )
        assert project_compile.returncode == 0, project_compile.stdout + project_compile.stderr
        project_agents = (project_root / "AGENTS.md").read_text(encoding="utf-8")
        assert "GLOBAL_OPENCODE_MARKER" in project_agents
        assert "SCOPED_OPENCODE_MARKER" in project_agents

        opencode_root = fake_home / ".config" / "opencode"
        opencode_root.mkdir(parents=True)
        claude_root = fake_home / ".claude"
        claude_root.mkdir()
        foreign_skill = fake_home / ".agents" / "skills" / "foreign" / "SKILL.md"
        foreign_skill.parent.mkdir(parents=True)
        foreign_skill.write_text("# Foreign\n", encoding="utf-8")
        foreign_native_skill = opencode_root / "skills" / "foreign" / "SKILL.md"
        foreign_native_skill.parent.mkdir(parents=True)
        foreign_native_skill.write_text("# Foreign native\n", encoding="utf-8")

        install = _run_apm(
            apm_binary_path,
            ["install", "--global", str(opencode_package), "--target", "opencode"],
            fake_home,
            fake_home,
        )
        assert install.returncode == 0, install.stdout + install.stderr

        native_skill = opencode_root / "skills" / "reviewer" / "SKILL.md"
        assert native_skill.is_file()
        assert not (fake_home / ".agents" / "skills" / "reviewer" / "SKILL.md").exists()

        compile_result = _run_apm(apm_binary_path, ["compile", "--global"], fake_home, fake_home)
        assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
        opencode_agents = (opencode_root / "AGENTS.md").read_text(encoding="utf-8")
        assert "GLOBAL_OPENCODE_MARKER" in opencode_agents
        assert "## Files matching `**/*.py`" in opencode_agents
        assert "SCOPED_OPENCODE_MARKER" in opencode_agents

        # The global manifest declares only OpenCode, so compile must not
        # materialize a root context file for the unrelated Claude target.
        assert not (claude_root / "CLAUDE.md").exists()

        lock_path = fake_home / ".apm" / "apm.lock.yaml"
        first_lock = lock_path.read_bytes()
        assert ".config/opencode/skills/reviewer/SKILL.md" in first_lock.decode("utf-8")
        first_agents = opencode_agents.encode("utf-8")
        repeat = _run_apm(apm_binary_path, ["compile", "--global"], fake_home, fake_home)
        assert repeat.returncode == 0, repeat.stdout + repeat.stderr
        assert lock_path.read_bytes() == first_lock
        assert (opencode_root / "AGENTS.md").read_bytes() == first_agents

        uninstall = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", str(opencode_package)],
            fake_home,
            fake_home,
        )
        assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
        assert not native_skill.exists()
        assert foreign_skill.read_text(encoding="utf-8") == "# Foreign\n"
        assert foreign_native_skill.read_text(encoding="utf-8") == "# Foreign native\n"
        assert project_skill.is_file()
        assert (project_root / "AGENTS.md").read_text(encoding="utf-8") == project_agents
        assert not (claude_root / "CLAUDE.md").exists()


class TestGlobalUninstallLifecycle:
    """Test uninstall --global removes packages from user-scope metadata."""

    @staticmethod
    def _install_survivor_and_removed_target_packages(
        apm_binary_path: Path, fake_home: Path, tmp_path: Path
    ) -> tuple[Path, Path, Path]:
        """Install global agent-skills survivor and OpenCode removal fixtures."""
        survivor = _write_targeted_package(
            tmp_path,
            "survivor-target-cleanup",
            ".apm/skills/survivor",
            "SKILL.md",
            "---\nname: survivor\ndescription: Survives removal.\n---\n# Survivor\n",
        )
        removed = _write_targeted_package(
            tmp_path,
            "removed-target-cleanup",
            ".apm/agents",
            "orphan.agent.md",
            "---\nname: orphan\ndescription: Must be cleaned.\n---\n# Orphan\n",
        )
        survivor_install = _run_apm(
            apm_binary_path,
            ["install", "--global", str(survivor), "--target", "agent-skills"],
            fake_home,
            fake_home,
        )
        assert survivor_install.returncode == 0, survivor_install.stdout + survivor_install.stderr
        removed_install = _run_apm(
            apm_binary_path,
            ["install", "--global", str(removed), "--target", "opencode"],
            fake_home,
            fake_home,
        )
        assert removed_install.returncode == 0, removed_install.stdout + removed_install.stderr
        survivor_file = fake_home / ".agents" / "skills" / "survivor" / "SKILL.md"
        removed_file = fake_home / ".config" / "opencode" / "agents" / "orphan.md"
        assert survivor_file.exists()
        assert removed_file.exists()
        return removed, survivor_file, removed_file

    def test_uninstall_removes_package_from_user_manifest(self, apm_binary_path, fake_home):
        """Uninstall --global should remove the package entry from ~/.apm/apm.yml."""
        apm_dir = fake_home / ".apm"
        apm_dir.mkdir(parents=True, exist_ok=True)
        (apm_dir / "apm_modules").mkdir(exist_ok=True)

        # Seed the manifest with a package
        manifest = apm_dir / "apm.yml"
        manifest.write_text(
            yaml.dump(
                {
                    "name": "global-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["test/pkg-to-remove"]},
                }
            )
        )

        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", "test/pkg-to-remove"],
            fake_home,
            fake_home,
        )

        data = yaml.safe_load(manifest.read_text())
        apm_deps = data.get("dependencies", {}).get("apm", [])
        assert "test/pkg-to-remove" not in apm_deps, (
            f"Package not removed from manifest: {apm_deps}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_uninstall_global_package_not_found_warns(self, apm_binary_path, fake_home):
        """Uninstalling a package that is not in the manifest should warn."""
        apm_dir = fake_home / ".apm"
        apm_dir.mkdir(parents=True, exist_ok=True)
        (apm_dir / "apm_modules").mkdir(exist_ok=True)

        manifest = apm_dir / "apm.yml"
        manifest.write_text(
            yaml.dump(
                {
                    "name": "global-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": []},
                }
            )
        )

        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", "nonexistent/pkg"],
            fake_home,
            fake_home,
        )

        combined = result.stdout + result.stderr
        assert "not found" in combined.lower() or "not in apm.yml" in combined.lower(), (
            f"Expected 'not found' warning: {combined}"
        )

    def test_uninstall_global_cleans_removed_only_target_before_state_removal(
        self,
        apm_binary_path,
        fake_home,
        tmp_path,
    ):
        """A removed OpenCode agent cannot survive a manifest with agent-skills only."""
        removed, survivor_file, removed_file = self._install_survivor_and_removed_target_packages(
            apm_binary_path, fake_home, tmp_path
        )

        apm_dir = fake_home / ".apm"
        uninstall_result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", str(removed)],
            fake_home,
            fake_home,
        )
        assert uninstall_result.returncode == 0, uninstall_result.stdout + uninstall_result.stderr

        assert not removed_file.exists()
        assert survivor_file.exists()
        manifest = yaml.safe_load((apm_dir / "apm.yml").read_text(encoding="utf-8"))
        assert isinstance(manifest, dict)
        manifest_text = yaml.safe_dump(manifest)
        assert "removed-target-cleanup" not in manifest_text
        assert "survivor-target-cleanup" in manifest_text
        lockfile_text = (apm_dir / "apm.lock.yaml").read_text(encoding="utf-8")
        assert "removed-target-cleanup" not in lockfile_text
        assert "survivor-target-cleanup" in lockfile_text
        assert not (apm_dir / "apm_modules" / "_local" / "removed-target-cleanup").exists()
        assert (apm_dir / "apm_modules" / "_local" / "survivor-target-cleanup").exists()

    def test_uninstall_global_preserves_state_for_user_edited_removed_target_file(
        self,
        apm_binary_path,
        fake_home,
        tmp_path,
    ):
        """A retained edited file leaves all removal state available for retry."""
        removed, survivor_file, removed_file = self._install_survivor_and_removed_target_packages(
            apm_binary_path, fake_home, tmp_path
        )
        removed_file.write_text("# user edit\n", encoding="utf-8")

        result = _run_apm(
            apm_binary_path,
            ["uninstall", "--global", str(removed)],
            fake_home,
            fake_home,
        )

        combined = result.stdout + result.stderr
        apm_dir = fake_home / ".apm"
        assert result.returncode != 0, combined
        assert ".config/opencode/agents/orphan.md" in combined
        assert "retry uninstall" in combined
        assert removed_file.exists()
        assert survivor_file.exists()
        assert "removed-target-cleanup" in (apm_dir / "apm.yml").read_text(encoding="utf-8")
        assert "removed-target-cleanup" in (apm_dir / "apm.lock.yaml").read_text(encoding="utf-8")
        assert (apm_dir / "apm_modules" / "_local" / "removed-target-cleanup").exists()


# ---------------------------------------------------------------------------
# Hook integration on the global install pipeline (regression for #1499)
# ---------------------------------------------------------------------------


@pytest.fixture
def naked_hook_package(tmp_path):
    """Package whose only hook file uses the "naked" Claude settings slice.

    Top-level keys are event names (no outer ``hooks:`` wrap), exactly
    as Claude Code accepts inside its own ``settings.json``. This is the
    literal repro shape from microsoft/apm#1499.
    """
    pkg = tmp_path / "naked-hook-pkg"
    pkg.mkdir()
    (pkg / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "naked-hook-pkg",
                "version": "1.0.0",
                "description": "Repro package for #1499 naked-format hook regression",
            }
        )
    )
    hooks_dir = pkg / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    scripts_dir = pkg / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "example.py").write_text("print('hi')\n")
    (hooks_dir / "session-metrics.json").write_text(
        '{"Stop": [{"matcher": "", "hooks": [{"type": "command", '
        '"command": "python3 ${PLUGIN_ROOT}/scripts/example.py", '
        '"timeout": 20000}]}]}'
    )
    return pkg


class TestGlobalHookIntegrationNakedFormat:
    """End-to-end regression for #1499 on the ``apm install -g`` pipeline.

    Drives the real CLI binary against a fake HOME and a package whose
    only hook file uses the naked Claude settings-slice format. Before
    the fix the global pipeline reported ``1 hook(s) integrated`` while
    leaving ``~/.claude/settings.json`` with ``{"hooks": {}}`` and never
    rewriting ``${PLUGIN_ROOT}`` for the copilot target.
    """

    def test_claude_settings_receives_naked_stop_entry(
        self, apm_binary_path, fake_home, naked_hook_package
    ):
        """``~/.claude/settings.json`` must carry the Stop entry after global install."""
        (fake_home / ".claude").mkdir()

        result = _run_apm(
            apm_binary_path,
            ["install", "--global", str(naked_hook_package)],
            fake_home,
            fake_home,
        )

        settings_path = fake_home / ".claude" / "settings.json"
        assert settings_path.exists(), (
            f"~/.claude/settings.json not created. stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        settings = json.loads(settings_path.read_text())
        assert settings.get("hooks", {}), (
            f"~/.claude/settings.json has empty hooks (the #1499 regression). "
            f"Got: {settings!r}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Stop" in settings["hooks"], (
            f"Stop event missing from ~/.claude/settings.json: {settings['hooks']!r}"
        )

    def test_integrated_counter_does_not_lie_on_empty_merge(
        self, apm_binary_path, fake_home, tmp_path
    ):
        """A hook file whose events are all empty must NOT bump the counter.

        Companion regression for #1499: the user-facing summary line
        ``N hook(s) integrated`` previously incremented even when the
        merge produced zero entries on disk. The new fail-closed code
        path now logs a warning AND keeps the counter accurate.
        """
        pkg = tmp_path / "empty-events-pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text(yaml.dump({"name": "empty-events-pkg", "version": "1.0.0"}))
        hooks_dir = pkg / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        # Naked-format file with an empty event list -- parses cleanly but
        # contributes zero entries.
        (hooks_dir / "noop.json").write_text('{"Stop": []}')

        (fake_home / ".claude").mkdir()

        result = _run_apm(
            apm_binary_path,
            ["install", "--global", str(pkg)],
            fake_home,
            fake_home,
        )

        combined = result.stdout + result.stderr
        assert "1 hook" not in combined, (
            f"Counter must not report '1 hook(s) integrated' for an empty merge. Got: {combined}"
        )
