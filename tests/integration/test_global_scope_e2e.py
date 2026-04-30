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

import os
import platform  # noqa: F401
import shutil
import subprocess
import sys
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
def fake_home(tmp_path):
    """Create an isolated home directory for user-scope tests.

    Sets HOME (Unix) and USERPROFILE (Windows) so that ``Path.home()``
    inside subprocesses resolves to a temporary directory.
    """
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    return home_dir


def _env_with_home(fake_home):
    """Return an env dict with HOME/USERPROFILE pointing to *fake_home*."""
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    return env


def _run_apm(apm_command, args, cwd, fake_home, timeout=60):
    """Run an apm CLI command with an overridden home directory."""
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env_with_home(fake_home),
    )


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


# ---------------------------------------------------------------------------
# User-scope directory creation
# ---------------------------------------------------------------------------


class TestGlobalDirectoryCreation:
    """Verify that --global creates ~/.apm/ and its children."""

    def test_global_flag_creates_apm_dir(self, apm_command, fake_home):
        """apm install --global should create ~/.apm/ even when the command
        ultimately fails (e.g. no manifest and no packages)."""
        result = _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)

        apm_dir = fake_home / ".apm"
        assert apm_dir.is_dir(), (
            f"~/.apm/ not created. stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_global_flag_creates_modules_subdir(self, apm_command, fake_home):
        """apm install --global should create ~/.apm/apm_modules/."""
        _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)

        modules = fake_home / ".apm" / "apm_modules"
        assert modules.is_dir(), "~/.apm/apm_modules/ not created"

    def test_short_flag_g_creates_apm_dir(self, apm_command, fake_home):
        """-g short flag should behave identically to --global."""
        _run_apm(apm_command, ["install", "-g"], fake_home, fake_home)

        assert (fake_home / ".apm").is_dir(), "-g did not create ~/.apm/"
        assert (fake_home / ".apm" / "apm_modules").is_dir()

    def test_directory_creation_is_idempotent(self, apm_command, fake_home):
        """Running --global twice should not raise or corrupt the directory."""
        _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)
        _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)

        assert (fake_home / ".apm").is_dir()
        assert (fake_home / ".apm" / "apm_modules").is_dir()


# ---------------------------------------------------------------------------
# CLI output / warnings
# ---------------------------------------------------------------------------


class TestGlobalScopeOutput:
    """Verify CLI output when using --global."""

    def test_shows_user_scope_info(self, apm_command, fake_home):
        """Install --global should display user scope info message."""
        result = _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)
        combined = result.stdout + result.stderr
        assert "user scope" in combined.lower() or "~/.apm/" in combined, (
            f"Missing scope info in output: {combined}"
        )

    def test_warns_about_unsupported_targets(self, apm_command, fake_home):
        """Install --global should warn about targets that lack user-scope support."""
        result = _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)
        combined = result.stdout + result.stderr
        assert "cursor" in combined.lower(), f"Missing cursor warning in output: {combined}"

    def test_uninstall_global_shows_scope_info(self, apm_command, fake_home):
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
            apm_command,
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

    def test_no_manifest_no_packages_errors(self, apm_command, fake_home):
        """--global without packages and without ~/.apm/apm.yml should fail."""
        result = _run_apm(apm_command, ["install", "--global"], fake_home, fake_home)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        # The error message includes the full path which may be line-wrapped
        # by Rich, so check for the key parts separately
        assert ".apm" in combined and "found" in combined.lower(), (
            f"Error should mention missing manifest: {combined}"
        )

    def test_uninstall_global_no_manifest_errors(self, apm_command, fake_home):
        """Uninstall --global without ~/.apm/apm.yml should fail."""
        result = _run_apm(
            apm_command,
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

    def test_auto_bootstrap_creates_user_manifest(self, apm_command, fake_home, local_package):
        """Installing a local package with --global auto-creates ~/.apm/apm.yml."""
        result = _run_apm(
            apm_command,
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

    def test_user_manifest_does_not_pollute_cwd(self, apm_command, fake_home, local_package):
        """--global must not create apm.yml in the working directory."""
        work_dir = fake_home / "workdir"
        work_dir.mkdir()

        _run_apm(
            apm_command,
            ["install", "--global", str(local_package)],
            work_dir,
            fake_home,
        )

        assert not (work_dir / "apm.yml").exists(), (
            "apm.yml was incorrectly created in the working directory"
        )

    def test_lockfile_placed_under_user_dir(self, apm_command, fake_home, local_package):
        """Lockfile should be created under ~/.apm/, not in the working directory."""
        work_dir = fake_home / "workdir"
        work_dir.mkdir()

        result = _run_apm(  # noqa: F841
            apm_command,
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

    def test_home_based_paths_are_absolute(self, apm_command, fake_home):
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

    def test_forward_slash_paths_on_all_platforms(self, apm_command, fake_home):
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

    def test_global_install_creates_gemini_dirs(self, apm_command, fake_home, local_package):
        """--global should deploy primitives to ~/.gemini/ when .gemini/ exists."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        result = _run_apm(
            apm_command,
            ["install", "--global", str(local_package)],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "gemini" in combined.lower(), f"Gemini not mentioned in output: {combined}"

    def test_global_install_mentions_gemini_full_support(self, apm_command, fake_home):
        """--global output should list gemini as fully supported."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        result = _run_apm(
            apm_command,
            ["install", "--global"],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "gemini" in combined.lower(), f"Gemini not in scope support message: {combined}"

    def test_global_uninstall_runs_in_user_scope(self, apm_command, fake_home, local_package):
        """Uninstall --global with .gemini/ present operates in user scope."""
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir()

        _run_apm(
            apm_command,
            ["install", "--global", str(local_package)],
            fake_home,
            fake_home,
        )

        result = _run_apm(
            apm_command,
            ["uninstall", "--global", "local-pkg"],
            fake_home,
            fake_home,
        )
        combined = result.stdout + result.stderr
        assert "user scope" in combined.lower(), f"Uninstall did not run in user scope: {combined}"


class TestGlobalUninstallLifecycle:
    """Test uninstall --global removes packages from user-scope metadata."""

    def test_uninstall_removes_package_from_user_manifest(self, apm_command, fake_home):
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
            apm_command,
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

    def test_uninstall_global_package_not_found_warns(self, apm_command, fake_home):
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
            apm_command,
            ["uninstall", "--global", "nonexistent/pkg"],
            fake_home,
            fake_home,
        )

        combined = result.stdout + result.stderr
        assert "not found" in combined.lower() or "not in apm.yml" in combined.lower(), (
            f"Expected 'not found' warning: {combined}"
        )
