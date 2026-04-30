"""Tests for the apm install command auto-bootstrap feature."""

import contextlib
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.models.results import InstallResult

from apm_cli.cli import cli


class TestInstallCommandAutoBootstrap:
    """Test cases for apm install command auto-bootstrap feature."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        """Clean up after tests."""
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    @contextlib.contextmanager
    def _chdir_tmp(self):
        """Context manager: create a temp dir, chdir into it, restore CWD on exit.

        Restoring CWD *before* TemporaryDirectory.__exit__ avoids
        PermissionError [WinError 32] on Windows when the process's current
        directory is inside the directory being deleted.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    def test_install_no_apm_yml_no_packages_shows_helpful_error(self):
        """Test that install without apm.yml and without packages shows helpful error."""
        with self._chdir_tmp():
            result = self.runner.invoke(cli, ["install"])

            assert result.exit_code == 1
            assert "No apm.yml found" in result.output
            assert "apm init" in result.output
            assert "apm install <org/repo>" in result.output

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_install_no_apm_yml_with_packages_creates_minimal_apm_yml(
        self, mock_install_apm, mock_apm_package, mock_validate, monkeypatch
    ):
        """Test that install with packages but no apm.yml creates minimal apm.yml."""
        with self._chdir_tmp():
            # Mock package validation to return True
            mock_validate.return_value = True

            # Mock APMPackage to return empty dependencies
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="test/package", reference="main")
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            # Mock the install function to avoid actual installation
            mock_install_apm.return_value = InstallResult(diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False))

            result = self.runner.invoke(cli, ["install", "test/package"])
            assert result.exit_code == 0
            assert "Created apm.yml" in result.output
            assert Path("apm.yml").exists()

            # Verify apm.yml structure
            with open("apm.yml", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                assert "dependencies" in config
                assert "apm" in config["dependencies"]
                assert "test/package" in config["dependencies"]["apm"]
                assert config["dependencies"]["mcp"] == []

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_install_no_apm_yml_with_multiple_packages(
        self, mock_install_apm, mock_apm_package, mock_validate, monkeypatch
    ):
        """Test that install with multiple packages creates apm.yml and adds all."""
        with self._chdir_tmp():
            # Mock package validation
            mock_validate.return_value = True

            # Mock APMPackage
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="org1/pkg1", reference="main"),
                MagicMock(repo_url="org2/pkg2", reference="main"),
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = InstallResult(diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False))

            result = self.runner.invoke(cli, ["install", "org1/pkg1", "org2/pkg2"])

            assert result.exit_code == 0
            assert "Created apm.yml" in result.output
            assert Path("apm.yml").exists()

            # Verify both packages are in apm.yml
            with open("apm.yml", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                assert "org1/pkg1" in config["dependencies"]["apm"]
                assert "org2/pkg2" in config["dependencies"]["apm"]

    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_install_existing_apm_yml_preserves_behavior(
        self, mock_install_apm, mock_apm_package
    ):
        """Test that install with existing apm.yml works as before."""
        with self._chdir_tmp():
            # Create existing apm.yml
            existing_config = {
                "name": "test-project",
                "version": "1.0.0",
                "description": "Test project",
                "author": "Test Author",
                "dependencies": {"apm": [], "mcp": []},
                "scripts": {},
            }
            with open("apm.yml", "w", encoding="utf-8") as f:
                yaml.dump(existing_config, f)

            # Mock APMPackage
            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = []
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = InstallResult(diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False))

            result = self.runner.invoke(cli, ["install"])

            # Should succeed and NOT show "Created apm.yml"
            assert result.exit_code == 0
            assert "Created apm.yml" not in result.output

            # Verify original config is preserved
            with open("apm.yml", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                assert config["name"] == "test-project"
                assert config["author"] == "Test Author"

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_install_auto_created_apm_yml_has_correct_metadata(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Test that auto-created apm.yml has correct metadata."""
        with self._chdir_tmp() as tmp_dir:
            # Create a directory with a specific name to test project name detection
            project_dir = tmp_dir / "my-awesome-project"
            project_dir.mkdir()
            os.chdir(project_dir)

            # Mock validation and installation
            mock_validate.return_value = True

            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = [
                MagicMock(repo_url="test/package", reference="main")
            ]
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            mock_install_apm.return_value = InstallResult(diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False))

            result = self.runner.invoke(cli, ["install", "test/package"])

            assert result.exit_code == 0
            assert Path("apm.yml").exists()

            # Verify auto-detected project name
            with open("apm.yml", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                assert config["name"] == "my-awesome-project"
                assert "version" in config
                assert "description" in config
                assert "APM project" in config["description"]

    @patch("apm_cli.commands.install._validate_package_exists")
    def test_install_invalid_package_format_with_no_apm_yml(self, mock_validate):
        """Test that invalid package format fails gracefully even with auto-bootstrap."""
        with self._chdir_tmp():
            # Don't mock validation - let it handle invalid format
            result = self.runner.invoke(cli, ["install", "invalid-package"])

            # Should create apm.yml but fail to add invalid package
            assert Path("apm.yml").exists()
            assert "invalid format" in result.output

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_install_dry_run_with_no_apm_yml_shows_what_would_be_created(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Test that dry-run with no apm.yml shows what would be created."""
        with self._chdir_tmp():
            mock_validate.return_value = True

            mock_pkg_instance = MagicMock()
            mock_pkg_instance.get_apm_dependencies.return_value = []
            mock_pkg_instance.get_mcp_dependencies.return_value = []
            mock_apm_package.from_apm_yml.return_value = mock_pkg_instance

            result = self.runner.invoke(cli, ["install", "test/package", "--dry-run"])

            # Should show what would be added
            assert result.exit_code == 0
            assert "Would add" in result.output or "Dry run" in result.output
            # apm.yml should still be created (for dry-run to work)
            assert Path("apm.yml").exists()


class TestValidationFailureReasonMessages:
    """Test that validation failure reasons include actionable auth guidance."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).parent.parent.parent))

    @contextlib.contextmanager
    def _chdir_tmp(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    @patch("apm_cli.commands.install._validate_package_exists", return_value=False)
    def test_validation_failure_without_verbose_includes_verbose_hint(self, mock_validate):
        """When validation fails without --verbose, reason should suggest --verbose."""
        with self._chdir_tmp():
            # Create apm.yml so we exercise the validation path
            Path("apm.yml").write_text("name: test\ndependencies:\n  apm: []\n  mcp: []\n")
            result = self.runner.invoke(cli, ["install", "owner/repo"])
            # Normalize terminal line-wrapping before checking
            output = " ".join(result.output.split())
            assert "run with --verbose for auth details" in output

    @patch("apm_cli.commands.install._validate_package_exists", return_value=False)
    def test_validation_failure_with_verbose_omits_verbose_hint(self, mock_validate):
        """When validation fails with --verbose, reason should NOT suggest --verbose."""
        with self._chdir_tmp():
            Path("apm.yml").write_text("name: test\ndependencies:\n  apm: []\n  mcp: []\n")
            result = self.runner.invoke(cli, ["install", "owner/repo", "--verbose"])
            assert "not accessible or doesn't exist" in result.output
            assert "run with --verbose for auth details" not in result.output

    @patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git", return_value=None)
    @patch("urllib.request.urlopen")
    def test_verbose_validation_failure_calls_build_error_context(self, mock_urlopen, _mock_cred):
        """When GitHub validation fails in verbose mode, build_error_context should be invoked."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo", code=404,
            msg="Not Found", hdrs={}, fp=None,
        )

        with patch.object(
            __import__("apm_cli.core.auth", fromlist=["AuthResolver"]).AuthResolver,
            "build_error_context",
            return_value="Authentication failed for accessing owner/repo on github.com.\nNo token available.",
        ) as mock_build_ctx:
            from apm_cli.commands.install import _validate_package_exists
            result = _validate_package_exists("owner/repo", verbose=True)
            assert result is False
            mock_build_ctx.assert_called_once()
            call_args = mock_build_ctx.call_args
            assert call_args[0][0] == "github.com"  # host
            assert call_args[0][1].endswith("owner/repo")  # operation

    def test_verbose_virtual_package_validation_shows_auth_diagnostics(self):
        """When virtual package validation fails in verbose mode, auth diagnostics are shown."""
        from apm_cli.commands.install import _validate_package_exists

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.validate_virtual_package_exists",
            return_value=False,
        ), patch.object(
            __import__("apm_cli.core.auth", fromlist=["AuthResolver"]).AuthResolver,
            "resolve_for_dep",
            return_value=MagicMock(source="none", token_type="none", token=None),
        ) as mock_resolve, patch.object(
            __import__("apm_cli.core.auth", fromlist=["AuthResolver"]).AuthResolver,
            "build_error_context",
            return_value="Authentication failed for accessing owner/repo/skills/my-skill on github.com.\nNo token available.",
        ) as mock_build_ctx:
            result = _validate_package_exists("owner/repo/skills/my-skill", verbose=True)
            assert result is False
            mock_resolve.assert_called_once()
            mock_build_ctx.assert_called_once()
            call_args = mock_build_ctx.call_args
            assert call_args[0][0] == "github.com"  # host
            assert "owner/repo/skills/my-skill" in call_args[0][1]  # operation

    def test_virtual_package_validation_reuses_auth_resolver(self):
        """Virtual package validation should pass its AuthResolver to the downloader."""
        from apm_cli.commands.install import _validate_package_exists

        with patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.__init__",
            return_value=None,
        ) as mock_init, patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader.validate_virtual_package_exists",
            return_value=True,
        ):
            _validate_package_exists("owner/repo/skills/my-skill", verbose=False)
            mock_init.assert_called_once()
            # The auth_resolver kwarg should be passed (not creating a new one)
            _, kwargs = mock_init.call_args
            assert "auth_resolver" in kwargs


# ---------------------------------------------------------------------------
# Transitive dep parent chain breadcrumb
# ---------------------------------------------------------------------------


class TestTransitiveDepParentChain:
    """Tests for DependencyNode.get_ancestor_chain() breadcrumb."""

    def test_get_ancestor_chain_returns_breadcrumb(self):
        """get_ancestor_chain walks up parent links and returns 'a > b > c'."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        root_ref = DependencyReference.parse("acme/root-pkg")
        mid_ref = DependencyReference.parse("acme/mid-pkg")
        leaf_ref = DependencyReference.parse("other-org/leaf-pkg")

        root_node = DependencyNode(
            package=APMPackage(name="root-pkg", version="1.0", source="acme/root-pkg"),
            dependency_ref=root_ref,
            depth=1,
        )
        mid_node = DependencyNode(
            package=APMPackage(name="mid-pkg", version="1.0", source="acme/mid-pkg"),
            dependency_ref=mid_ref,
            depth=2,
            parent=root_node,
        )
        leaf_node = DependencyNode(
            package=APMPackage(name="leaf-pkg", version="1.0", source="other-org/leaf-pkg"),
            dependency_ref=leaf_ref,
            depth=3,
            parent=mid_node,
        )

        chain = leaf_node.get_ancestor_chain()
        assert chain == "acme/root-pkg > acme/mid-pkg > other-org/leaf-pkg"

    def test_get_ancestor_chain_single_node(self):
        """Direct dep (no parent) returns just its own name."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        ref = DependencyReference.parse("acme/direct-pkg")
        node = DependencyNode(
            package=APMPackage(name="direct-pkg", version="1.0", source="acme/direct-pkg"),
            dependency_ref=ref,
            depth=1,
        )
        chain = node.get_ancestor_chain()
        assert chain == "acme/direct-pkg"

    def test_get_ancestor_chain_root_node(self):
        """Root node (no parent) returns just the node's display name."""
        from apm_cli.deps.dependency_graph import DependencyNode
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        ref = DependencyReference.parse("acme/root-pkg")
        node = DependencyNode(
            package=APMPackage(name="root-pkg", version="1.0", source="acme/root-pkg"),
            dependency_ref=ref,
            depth=0,
        )
        assert node.get_ancestor_chain() == "acme/root-pkg"

    def test_download_callback_includes_chain_in_error(self, tmp_path):
        """When a transitive dep download fails, the error message includes
        the parent chain breadcrumb for debugging.

        Tests the resolver + callback interaction directly: we create a
        resolver with a callback that fails on the leaf dep, and verify
        the parent_chain arg is passed through correctly.
        """
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage, DependencyReference

        # Set up apm_modules with root-pkg that declares leaf-pkg as dep
        modules_dir = tmp_path / "apm_modules"
        root_dir = modules_dir / "acme" / "root-pkg"
        root_dir.mkdir(parents=True)
        (root_dir / "apm.yml").write_text(yaml.safe_dump({
            "name": "root-pkg",
            "version": "1.0.0",
            "dependencies": {"apm": ["other-org/leaf-pkg"], "mcp": []},
        }))

        # Write root apm.yml that depends on root-pkg
        (tmp_path / "apm.yml").write_text(yaml.safe_dump({
            "name": "test-project",
            "version": "0.0.1",
            "dependencies": {"apm": ["acme/root-pkg"], "mcp": []},
        }))

        # Track what the callback receives
        callback_calls = []

        def tracking_callback(dep_ref, mods_dir, parent_chain=""):
            callback_calls.append({
                "dep": dep_ref.get_display_name(),
                "parent_chain": parent_chain,
            })
            if "leaf-pkg" in dep_ref.get_display_name():
                # Simulate what the real callback does: catch internal error,
                # return None (non-blocking). The resolver treats None as
                # "download failed, skip transitive deps".
                return None
            # Root-pkg is already on disk, return its path
            return dep_ref.get_install_path(mods_dir)

        resolver = APMDependencyResolver(
            apm_modules_dir=modules_dir,
            download_callback=tracking_callback,
        )

        os.chdir(tmp_path)
        resolver.resolve_dependencies(tmp_path)

        # The callback should have been called for leaf-pkg
        leaf_calls = [c for c in callback_calls if "leaf-pkg" in c["dep"]]
        assert len(leaf_calls) == 1, (
            f"Expected 1 call for leaf-pkg, got {len(leaf_calls)}. "
            f"All calls: {callback_calls}"
        )

        # The parent chain should contain root-pkg
        chain = leaf_calls[0]["parent_chain"]
        assert "root-pkg" in chain, (
            f"Expected 'root-pkg' in parent chain, got: '{chain}'"
        )
        # Chain should show the full path: root > leaf
        assert ">" in chain, (
            f"Expected '>' separator in chain, got: '{chain}'"
        )


class TestDownloadCallbackErrorMessages:
    """Tests for direct vs transitive dep error message differentiation."""

    def test_direct_dep_failure_says_download_dependency(self, tmp_path, monkeypatch):
        """Direct dependency failure uses 'Failed to download dependency', not 'transitive dep'."""
        from apm_cli.commands.install import _install_apm_dependencies
        from apm_cli.models.apm_package import APMPackage

        monkeypatch.chdir(tmp_path)

        # Create a minimal apm.yml with a direct dep
        (tmp_path / "apm.yml").write_text(yaml.safe_dump({
            "name": "test-project",
            "version": "0.0.1",
            "dependencies": {"apm": ["acme/direct-pkg"], "mcp": []},
        }))

        apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")

        # Patch the downloader to always fail
        with patch("apm_cli.deps.github_downloader.GitHubPackageDownloader") as MockDownloader:
            mock_dl = MockDownloader.return_value
            mock_dl.download_package.side_effect = RuntimeError("auth failed")

            result = _install_apm_dependencies(
                apm_package, verbose=False, force=False, parallel_downloads=0,
            )

        # Check that the error message says "download dependency", not "transitive dep"
        errors = result.diagnostics.by_category().get("error", [])
        assert len(errors) == 1, f"Expected 1 error, got {len(errors)}: {errors}"
        assert "Failed to download dependency" in errors[0].message
        assert "transitive" not in errors[0].message.lower()

    def test_transitive_dep_key_not_in_direct_dep_keys(self):
        """Transitive dep keys are correctly absent from direct_dep_keys set.

        The download_callback uses this check to select the right error label.
        End-to-end transitive error flow is covered by
        TestTransitiveDepParentChain.test_download_callback_includes_chain_in_error.
        """
        from apm_cli.models.apm_package import DependencyReference

        direct_dep_keys = {"acme/root-pkg"}
        transitive_ref = DependencyReference.parse("other-org/leaf-pkg")

        # Transitive deps must NOT be in the direct set
        assert transitive_ref.get_unique_key() not in direct_dep_keys
        # Direct deps must be in the direct set
        assert "acme/root-pkg" in direct_dep_keys


class TestCallbackFailureDeduplication:
    """Tests for error deduplication when download_callback failures are not re-tried."""

    def test_callback_failure_not_duplicated_in_main_loop(self, tmp_path, monkeypatch):
        """A dep that fails in download_callback should produce only one error."""
        from apm_cli.commands.install import _install_apm_dependencies
        from apm_cli.models.apm_package import APMPackage

        monkeypatch.chdir(tmp_path)

        (tmp_path / "apm.yml").write_text(yaml.safe_dump({
            "name": "test-project",
            "version": "0.0.1",
            "dependencies": {"apm": ["acme/failing-pkg"], "mcp": []},
        }))
        apm_package = APMPackage.from_apm_yml(tmp_path / "apm.yml")

        with patch("apm_cli.deps.github_downloader.GitHubPackageDownloader") as MockDownloader:
            mock_dl = MockDownloader.return_value
            mock_dl.download_package.side_effect = RuntimeError("auth failed")

            result = _install_apm_dependencies(
                apm_package, verbose=False, force=False, parallel_downloads=0,
            )

        errors = result.diagnostics.by_category().get("error", [])
        # Should be exactly 1 error, not 2 (one from callback + one from main loop)
        assert len(errors) == 1, (
            f"Expected 1 error (deduplicated), got {len(errors)}: "
            f"{[e.message for e in errors]}"
        )


class TestLocalPathValidationMessages:
    """Tests for improved local path validation error messages."""

    def test_local_path_failure_reason_nonexistent(self, tmp_path):
        """Non-existent path returns 'path does not exist'."""
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference.parse(str(tmp_path / "does-not-exist-xyz-9999"))
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path does not exist"

    def test_local_path_failure_reason_file_not_dir(self, tmp_path):
        """A file (not directory) returns 'path is not a directory'."""
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        f = tmp_path / "somefile.txt"
        f.write_text("hello")
        dep_ref = DependencyReference.parse(str(f))
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path is not a directory"

    def test_local_path_failure_reason_no_markers(self, tmp_path):
        """Directory without markers returns specific message."""
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        empty_dir = tmp_path / "empty-pkg"
        empty_dir.mkdir()
        dep_ref = DependencyReference.parse(str(empty_dir))
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "no apm.yml, SKILL.md, or plugin.json found"

    def test_local_path_failure_reason_valid_apm_yml(self, tmp_path):
        """Directory with apm.yml still returns 'no markers' message.

        _local_path_failure_reason is only called when _validate_package_exists
        already returned False, so it doesn't re-check markers. We verify it
        returns a string (not None) and doesn't crash.
        """
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        pkg = tmp_path / "valid-pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
        dep_ref = DependencyReference.parse(str(pkg))
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "no apm.yml, SKILL.md, or plugin.json found"

    def test_local_path_failure_reason_remote_ref(self):
        """Remote refs return None (not a local path)."""
        from apm_cli.commands.install import _local_path_failure_reason
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference.parse("owner/repo")
        reason = _local_path_failure_reason(dep_ref)
        assert reason is None

    def test_hint_finds_skill_in_subdirectory(self, tmp_path, capsys):
        """Hint discovers SKILL.md in a child directory."""
        from apm_cli.commands.install import _local_path_no_markers_hint

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n")

        _local_path_no_markers_hint(tmp_path)
        captured = capsys.readouterr()
        # Rich may wrap long paths across lines; collapse before asserting
        flat = captured.out.replace("\n", "")
        assert "my-skill" in flat

    def test_hint_finds_nested_skill(self, tmp_path, capsys):
        """Hint discovers SKILL.md two levels deep (skills/<name>/)."""
        from apm_cli.commands.install import _local_path_no_markers_hint

        nested = tmp_path / "skills" / "deep-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("---\nname: deep-skill\n---\n")

        _local_path_no_markers_hint(tmp_path)
        captured = capsys.readouterr()
        flat = captured.out.replace("\n", "")
        assert "deep-skill" in flat

    def test_hint_silent_when_no_packages(self, tmp_path, capsys):
        """Hint produces no output when no sub-packages found."""
        from apm_cli.commands.install import _local_path_no_markers_hint

        (tmp_path / "random-file.txt").write_text("nothing here")
        _local_path_no_markers_hint(tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_hint_caps_at_five(self, tmp_path, capsys):
        """Hint shows at most 5 packages then a '... and N more' line."""
        from apm_cli.commands.install import _local_path_no_markers_hint

        for i in range(8):
            d = tmp_path / f"skill-{i:02d}"
            d.mkdir()
            (d / "SKILL.md").write_text(f"---\nname: skill-{i:02d}\n---\n")

        _local_path_no_markers_hint(tmp_path)
        captured = capsys.readouterr()
        assert "apm install" in captured.out
        assert "... and 3 more" in captured.out


# ---------------------------------------------------------------------------
# Global scope (--global / -g) tests
# ---------------------------------------------------------------------------


class TestInstallGlobalFlag:
    """Tests for the --global / -g flag on apm install."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    def test_global_flag_shows_scope_info(self):
        """--global flag should display user scope info message and unsupported target warning."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                # Create a fake home with no manifest so the command errors early
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()
                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(cli, ["install", "--global"])
                assert result.exit_code == 1
                assert "user scope" in result.output.lower() or "~/.apm/" in result.output
                # Should warn about unsupported targets
                assert "cursor" in result.output.lower()
            finally:
                os.chdir(self.original_dir)

    def test_global_short_flag_g(self):
        """-g short flag creates user dirs and shows scope info like --global."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()
                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(cli, ["install", "-g"])
                # Should create ~/.apm/ directory
                assert (fake_home / ".apm").is_dir()
                assert (fake_home / ".apm" / "apm_modules").is_dir()
                assert result.exit_code == 1  # No packages or manifest provided
                assert "user scope" in result.output.lower() or "~/.apm/" in result.output
                # Should warn about unsupported targets
                assert "cursor" in result.output.lower()
            finally:
                os.chdir(self.original_dir)

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_global_creates_user_apm_yml(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """--global auto-creates ~/.apm/apm.yml when installing packages."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()

                mock_validate.return_value = True
                mock_pkg = MagicMock()
                mock_pkg.get_apm_dependencies.return_value = [
                    MagicMock(repo_url="test/pkg", reference="main")
                ]
                mock_pkg.get_mcp_dependencies.return_value = []
                mock_pkg.get_dev_apm_dependencies.return_value = []
                mock_pkg.target = None
                mock_apm_package.from_apm_yml.return_value = mock_pkg
                mock_install_apm.return_value = InstallResult(
                    diagnostics=MagicMock(has_diagnostics=False, has_critical_security=False)
                )

                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(
                        cli, ["install", "--global", "test/pkg"]
                    )

                assert result.exit_code == 0
                user_manifest = fake_home / ".apm" / "apm.yml"
                assert user_manifest.exists(), f"Expected {user_manifest} to exist"
                assert (fake_home / ".apm" / "apm_modules").is_dir()
            finally:
                os.chdir(self.original_dir)

    def test_global_without_packages_and_no_manifest_errors(self):
        """--global without packages and no ~/.apm/apm.yml shows error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()
                with patch.object(Path, "home", return_value=fake_home), \
                     patch.dict(os.environ, {"COLUMNS": "200"}):
                    result = self.runner.invoke(cli, ["install", "--global"])
                assert result.exit_code == 1
                assert "apm.yml" in result.output
            finally:
                os.chdir(self.original_dir)

    def test_global_rejects_local_path_package(self):
        """--global should reject local path packages with a clear error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()
                # Create a local package directory that would pass normal validation
                local_pkg = Path(tmp_dir) / "my-local-pkg"
                local_pkg.mkdir()
                (local_pkg / "apm.yml").write_text("name: my-local-pkg\n")

                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(
                        cli, ["install", "--global", "./my-local-pkg"]
                    )

                # Should fail with clear message about local packages
                # Use shorter substring to tolerate Rich word-wrapping on
                # platforms with long temp paths (Windows).
                assert "not supported at user scope" in result.output
                # Should suggest using remote reference
                assert "owner/repo" in result.output
            finally:
                os.chdir(self.original_dir)

    def test_global_rejects_absolute_local_path(self):
        """--global should reject absolute local paths too."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()
                local_pkg = Path(tmp_dir) / "abs-pkg"
                local_pkg.mkdir()
                (local_pkg / "apm.yml").write_text("name: abs-pkg\n")

                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(
                        cli, ["install", "--global", str(local_pkg)]
                    )

                normalized = " ".join(result.output.split())
                assert "not supported at user scope" in normalized
            finally:
                os.chdir(self.original_dir)

    def test_global_rejects_tilde_local_path(self):
        """--global should reject ~/path local packages."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                fake_home = Path(tmp_dir) / "fakehome"
                fake_home.mkdir()

                with patch.object(Path, "home", return_value=fake_home):
                    result = self.runner.invoke(
                        cli, ["install", "--global", "~/some-pkg"]
                    )

                assert "not supported at user scope" in result.output
            finally:
                os.chdir(self.original_dir)

# ---------------------------------------------------------------------------
# Generic-host SSH-first validation tests
# ---------------------------------------------------------------------------

class TestGenericHostSshFirstValidation:
    """Tests for the SSH-first ls-remote logic added for generic (non-GitHub/ADO) hosts."""

    def _make_completed_process(self, returncode, stderr=""):
        """Return a minimal subprocess.CompletedProcess-like mock."""
        mock = MagicMock()
        mock.returncode = returncode
        mock.stderr = stderr
        mock.stdout = ""
        return mock

    @patch("subprocess.run")
    def test_generic_host_tries_ssh_first_and_succeeds(self, mock_run):
        """SSH URL is tried first for generic hosts and used when it succeeds."""
        from apm_cli.commands.install import _validate_package_exists

        # SSH probe succeeds on the first call
        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists(
            "git@git.example.org:org/group/repo.git", verbose=False
        )

        assert result is True
        # subprocess.run must have been called at least once
        assert mock_run.call_count >= 1
        # First call must use the SSH URL
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org" in arg for arg in first_call_cmd), (
            f"Expected SSH URL in first ls-remote call, got: {first_call_cmd}"
        )

    @patch("subprocess.run")
    def test_explicit_ssh_url_does_not_fall_back_to_https(self, mock_run):
        """Strict-by-default (issue #992): explicit SSH URLs must NOT silently
        fall back to HTTPS, mirroring ``_clone_with_fallback`` semantics. The
        legacy permissive chain stays available behind
        ``APM_ALLOW_PROTOCOL_FALLBACK=1``."""
        from apm_cli.commands.install import _validate_package_exists

        # SSH probe fails; previously this would have silently retried HTTPS.
        mock_run.return_value = self._make_completed_process(
            returncode=128, stderr="ssh: connect to host"
        )

        result = _validate_package_exists(
            "git@git.example.org:org/group/repo.git", verbose=False
        )

        assert result is False
        assert mock_run.call_count == 1, (
            "explicit ssh:// must be strict; got "
            f"{[c[0][0] for c in mock_run.call_args_list]!r}"
        )
        first_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org:" in arg for arg in first_cmd), (
            f"Expected SSH URL in only call, got: {first_cmd}"
        )

    @patch.dict(os.environ, {"APM_ALLOW_PROTOCOL_FALLBACK": "1"})
    @patch("subprocess.run")
    def test_explicit_ssh_falls_back_to_https_with_allow_fallback_env(self, mock_run):
        """Legacy permissive chain restored when the env opt-in is set."""
        from urllib.parse import urlsplit

        from apm_cli.commands.install import _validate_package_exists

        mock_run.side_effect = [
            self._make_completed_process(returncode=128, stderr="ssh: connect to host"),
            self._make_completed_process(returncode=0),
        ]

        result = _validate_package_exists(
            "git@git.example.org:org/group/repo.git", verbose=False
        )

        assert result is True
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        assert any("git@git.example.org:" in arg for arg in first_cmd)
        second_cmd = mock_run.call_args_list[1][0][0]
        https_arg_found = any(
            urlsplit(arg).scheme == "https"
            and urlsplit(arg).netloc == "git.example.org"
            for arg in second_cmd
        )
        assert https_arg_found, (
            f"Expected https://git.example.org URL in second call, got: {second_cmd}"
        )

    @patch("subprocess.run")
    def test_generic_host_returns_false_when_explicit_ssh_fails(self, mock_run):
        """Strict mode: a single failed SSH probe is the only attempt."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(
            returncode=128, stderr="fatal: could not read Username"
        )

        result = _validate_package_exists(
            "git@git.example.org:org/group/repo.git", verbose=False
        )

        assert result is False
        assert mock_run.call_count == 1  # strict: SSH only, no HTTPS retry

    @patch("subprocess.run")
    def test_explicit_http_generic_host_tries_http_first(self, mock_run):
        """Explicit HTTP must probe HTTP before any SSH fallback."""
        from urllib.parse import urlsplit

        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists(
            "http://gitlab.company.internal/acme/rules.git", verbose=False
        )

        assert result is True
        assert mock_run.call_count == 1
        first_cmd = mock_run.call_args_list[0][0][0]
        # Parse each arg as a URL and check scheme + netloc explicitly to
        # avoid substring false-positives (e.g. the hostname appearing in a
        # path segment or query value on an otherwise-SSH URL).
        http_arg_found = False
        for arg in first_cmd:
            parts = urlsplit(arg)
            if parts.scheme == "http" and parts.netloc == "gitlab.company.internal":
                http_arg_found = True
                break
        assert http_arg_found, (
            f"Expected http://gitlab.company.internal URL in first call, got: {first_cmd}"
        )
        assert all("git@" not in arg for arg in first_cmd), (
            f"Expected no SSH URL in first call, got: {first_cmd}"
        )

    @patch("subprocess.run")
    def test_github_host_skips_ssh_attempt(self, mock_run):
        """GitHub.com repositories do NOT go through the SSH-first ls-remote path."""
      
        import urllib.request
        import urllib.error

        from apm_cli.commands.install import _validate_package_exists

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://api.github.com/repos/owner/repo",
                code=404, msg="Not Found", hdrs={}, fp=None,
            )
            result = _validate_package_exists("owner/repo", verbose=False)

        assert result is False
        # No ls-remote call should have been made for a github.com host
        ls_remote_calls = [
            call for call in mock_run.call_args_list
            if "ls-remote" in (call[0][0] if call[0] else [])
        ]
        assert len(ls_remote_calls) == 0, (
            f"Expected no ls-remote calls for github.com, got: {ls_remote_calls}"
        )

    @patch("subprocess.run")
    def test_ghes_host_skips_ssh_attempt(self, mock_run):
        """A GHES host is treated as GitHub, not generic SSH probe is skipped."""
        from apm_cli.commands.install import _validate_package_exists

        mock_run.return_value = self._make_completed_process(returncode=0)

        result = _validate_package_exists(
            "company.ghe.com/team/internal-repo", verbose=False
        )

        assert result is True
        ls_remote_calls = [
            call for call in mock_run.call_args_list
            if "ls-remote" in (call[0][0] if call[0] else [])
        ]
        assert len(ls_remote_calls) == 1, (
            f"Expected exactly 1 ls-remote call for GHES host, got: {ls_remote_calls}"
        )
        only_cmd = ls_remote_calls[0][0][0]
        # Must use HTTPS, not SSH
        assert all("git@" not in arg for arg in only_cmd), (
            f"Expected HTTPS-only URL for GHES host, got: {only_cmd}"
        )


class TestExplicitTargetDirCreation:
    """Verify --target creates root_dir even when auto_create=False (GH bug fix)."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self._tmpdir)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_explicit_target_creates_dir_for_auto_create_false(self):
        """When _explicit is set, target dirs are created even if auto_create=False."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        claude = KNOWN_TARGETS["claude"]
        assert claude.auto_create is False

        # Simulate the fixed loop logic: create dir when _explicit is set
        _explicit = "claude"
        _targets = [claude]
        for _t in _targets:
            if not _t.auto_create and not _explicit:
                continue
            _target_dir = self.project_root / _t.root_dir
            if not _target_dir.exists():
                _target_dir.mkdir(parents=True, exist_ok=True)

        assert (self.project_root / ".claude").is_dir()

    def test_auto_detect_skips_dir_for_auto_create_false(self):
        """Without _explicit, auto_create=False targets don't get dirs created."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        claude = KNOWN_TARGETS["claude"]
        assert claude.auto_create is False

        _explicit = None
        _targets = [claude]
        for _t in _targets:
            if not _t.auto_create and not _explicit:
                continue
            _target_dir = self.project_root / _t.root_dir
            if not _target_dir.exists():
                _target_dir.mkdir(parents=True, exist_ok=True)

        assert not (self.project_root / ".claude").exists()

    def test_auto_create_true_always_creates_dir(self):
        """auto_create=True targets create dir regardless of _explicit."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.auto_create is True

        for _explicit in [None, "copilot"]:
            import shutil
            shutil.rmtree(self.project_root / copilot.root_dir, ignore_errors=True)

            _targets = [copilot]
            for _t in _targets:
                if not _t.auto_create and not _explicit:
                    continue
                _target_dir = self.project_root / _t.root_dir
                if not _target_dir.exists():
                    _target_dir.mkdir(parents=True, exist_ok=True)

            assert (self.project_root / ".github").is_dir(), (
                f"auto_create=True should create dir when _explicit={_explicit!r}"
            )


class TestContentHashFallback:
    """Verify content-hash fallback when .git is removed from installed packages."""

    def test_hash_match_skips_redownload(self):
        """Content hash verification allows skipping re-download."""
        from apm_cli.utils.content_hash import compute_package_hash, verify_package_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = Path(tmpdir) / "pkg"
            pkg_dir.mkdir()
            (pkg_dir / "file.txt").write_text("hello")
            content_hash = compute_package_hash(pkg_dir)

            assert verify_package_hash(pkg_dir, content_hash) is True

    def test_hash_mismatch_triggers_redownload(self):
        """Mismatched content hash means re-download should proceed."""
        from apm_cli.utils.content_hash import verify_package_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = Path(tmpdir) / "pkg"
            pkg_dir.mkdir()
            (pkg_dir / "file.txt").write_text("original")

            assert verify_package_hash(pkg_dir, "sha256:badhash") is False

    def test_missing_content_hash_skips_fallback(self):
        """When locked dep has no content_hash, the fallback guard prevents
        verify_package_hash from being called."""
        from apm_cli.utils.content_hash import verify_package_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = Path(tmpdir) / "pkg"
            pkg_dir.mkdir()
            (pkg_dir / "file.txt").write_text("data")

            # Simulate the guard logic from install.py:
            # if _pd_locked_chk.content_hash and _pd_path.is_dir():
            content_hash = None  # no content_hash recorded in lockfile
            fallback_triggered = False
            if content_hash and pkg_dir.is_dir():
                fallback_triggered = verify_package_hash(pkg_dir, content_hash)

            assert not fallback_triggered, (
                "Fallback must not trigger when content_hash is None"
            )


class TestAllowInsecureFlag:
    """Tests for --allow-insecure flag and HTTP dependency security checks."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def test_http_dep_rejected_without_allow_insecure_flag(self):
        """Adding http:// package without --allow-insecure is rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
                    result = self.runner.invoke(
                        cli, ["install", "http://my-server.example.com/owner/repo"]
                     )
            finally:
                os.chdir(self.original_dir)
            assert "allow_insecure: true" in result.output
            assert "--allow-insecure" in result.output

    def test_install_help_mentions_allow_insecure_for_http_deps(self):
        """Install help should mention the HTTP allow-insecure flow."""
        result = self.runner.invoke(cli, ["install", "--help"])

        assert result.exit_code == 0
        normalized = " ".join(result.output.split())
        assert "use --allow-insecure for http:// packages" in normalized
        assert "--allow-insecure-host HOSTNAME" in result.output

    def test_allow_insecure_host_rejects_non_hostname(self):
        """The explicit transitive host option only accepts bare hostnames."""
        result = self.runner.invoke(
            cli,
            [
                "install",
                "--allow-insecure-host",
                "https://mirror.example.com",
                "owner/repo",
            ],
        )

        assert result.exit_code != 0
        assert "Invalid value for '--allow-insecure-host'" in result.output

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_http_dep_addition_passes_with_allow_insecure_flag(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """HTTP dependency can be added when the CLI flag is passed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                mock_pkg_instance = MagicMock()
                mock_pkg_instance.get_apm_dependencies.return_value = []
                mock_pkg_instance.get_mcp_dependencies.return_value = []
                mock_apm_package.from_apm_yml.return_value = mock_pkg_instance
                mock_install_apm.return_value = InstallResult(
                    diagnostics=MagicMock(
                        has_diagnostics=False, has_critical_security=False
                    )
                )

                result = self.runner.invoke(
                    cli, ["install", "--allow-insecure", "http://my-server.example.com/owner/repo"]
                )

                assert result.exit_code == 0
                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)

                assert config["dependencies"]["apm"] == [
                    {
                        "git": "http://my-server.example.com/owner/repo",
                        "allow_insecure": True,
                    }
                ]
                assert mock_install_apm.call_args.kwargs["allow_insecure"] is True
                assert mock_install_apm.call_args.kwargs["allow_insecure_hosts"] == ()
            finally:
                os.chdir(self.original_dir)

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_allow_insecure_host_is_passed_to_install_engine(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """The explicit transitive host option is threaded into the install engine."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                mock_pkg_instance = MagicMock()
                mock_pkg_instance.get_apm_dependencies.return_value = [MagicMock()]
                mock_pkg_instance.get_mcp_dependencies.return_value = []
                mock_pkg_instance.get_dev_apm_dependencies.return_value = []
                mock_apm_package.from_apm_yml.return_value = mock_pkg_instance
                mock_install_apm.return_value = InstallResult(
                    diagnostics=MagicMock(
                        has_diagnostics=False, has_critical_security=False
                    )
                )

                result = self.runner.invoke(
                    cli,
                    [
                        "install",
                        "--allow-insecure-host",
                        "mirror.example.com",
                        "owner/repo",
                    ],
                )

                assert result.exit_code == 0
                assert (
                    mock_install_apm.call_args.kwargs["allow_insecure_hosts"]
                    == ("mirror.example.com",)
                )
            finally:
                os.chdir(self.original_dir)

    def test_http_dep_validation_check(self):
        """_check_insecure_dependencies blocks HTTP dep without allow_insecure flag."""
        from apm_cli.commands.install import _check_insecure_dependencies
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        dep.allow_insecure = True
        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _check_insecure_dependencies([dep], False, logger)
        assert "http://my-server.example.com/owner/repo" in str(exc_info.value)
        message = logger.error.call_args.args[0]
        assert "http://my-server.example.com/owner/repo" in message
        # Manifest is already set (allow_insecure: true on the dep), so only
        # the CLI flag step should be mentioned - not the manifest edit step.
        assert "--allow-insecure" in message
        assert "Set allow_insecure: true" not in message

    def test_http_dep_passes_with_allow_insecure_flag(self):
        """_check_insecure_dependencies passes when flag is set and dep has allow_insecure."""
        from apm_cli.commands.install import _check_insecure_dependencies
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        dep.allow_insecure = True
        logger = MagicMock()

        _check_insecure_dependencies([dep], True, logger)

    def test_http_dep_without_dep_level_allow_insecure_is_blocked(self):
        """_check_insecure_dependencies blocks HTTP dep missing allow_insecure=True on dep."""
        from apm_cli.commands.install import _check_insecure_dependencies
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://my-server.example.com/owner/repo")
        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _check_insecure_dependencies([dep], True, logger)
        assert "http://my-server.example.com/owner/repo" in str(exc_info.value)
        message = logger.error.call_args.args[0]
        assert "http://my-server.example.com/owner/repo" in message
        # CLI flag is already set, so only the manifest edit step should be
        # mentioned - not the CLI flag step.
        assert "Set allow_insecure: true" in message
        assert "Pass --allow-insecure" not in message

    def test_https_dep_passes_without_flag(self):
        """_check_insecure_dependencies does not block HTTPS deps."""
        from apm_cli.commands.install import _check_insecure_dependencies
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("owner/repo")
        _check_insecure_dependencies([dep], False, MagicMock())

    def test_empty_deps_list_passes(self):
        """_check_insecure_dependencies handles empty dep list."""
        from apm_cli.commands.install import _check_insecure_dependencies

        _check_insecure_dependencies([], False, MagicMock())


class TestInsecureDependencyWarnings:
    """Tests for install-time insecure dependency warnings."""

    def test_collect_insecure_dependency_infos_marks_direct_dependency(self):
        """Direct HTTP dependencies are collected without parent provenance."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _collect_insecure_dependency_infos,
        )
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://mirror.example.com/acme/rules")
        tree = MagicMock()
        tree.get_node.return_value = None
        graph = MagicMock(dependency_tree=tree)

        infos = _collect_insecure_dependency_infos([dep], graph)

        assert infos == [
            _InsecureDependencyInfo(
                url="http://mirror.example.com/acme/rules",
                is_transitive=False,
                introduced_by=None,
            )
        ]

    def test_collect_insecure_dependency_infos_marks_transitive_dependency(self):
        """Transitive HTTP dependencies carry introducer information."""
        from apm_cli.commands.install import _collect_insecure_dependency_infos
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("http://mirror.example.com/acme/transitive")
        parent_ref = DependencyReference.parse("owner/root-package")
        parent_node = types.SimpleNamespace(dependency_ref=parent_ref)
        node = types.SimpleNamespace(parent=parent_node)
        tree = MagicMock()
        tree.get_node.return_value = node
        graph = MagicMock(dependency_tree=tree)

        infos = _collect_insecure_dependency_infos([dep], graph)

        assert len(infos) == 1
        assert infos[0].url == "http://mirror.example.com/acme/transitive"
        assert infos[0].is_transitive is True
        assert infos[0].introduced_by == "owner/root-package"

    def test_format_insecure_dependency_warning_for_transitive_dep(self):
        """Transitive warning strings include the introducer."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _format_insecure_dependency_warning,
        )

        message = _format_insecure_dependency_warning(
            _InsecureDependencyInfo(
                url="http://mirror.example.com/acme/transitive",
                is_transitive=True,
                introduced_by="owner/root-package",
            )
        )

        assert "Insecure HTTP fetch (unencrypted)" in message
        assert "http://mirror.example.com/acme/transitive" in message
        assert "transitive, introduced by owner/root-package" in message


class TestTransitiveInsecureDependencyGuard:
    """Tests for host-based consent around transitive insecure dependencies."""

    def test_transitive_guard_blocks_unapproved_host(self):
        """Transitive insecure deps are blocked when their host is not approved."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _guard_transitive_insecure_dependencies,
        )
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _guard_transitive_insecure_dependencies(
                [
                    _InsecureDependencyInfo(
                        url="http://mirror.example.com/acme/transitive",
                        is_transitive=True,
                        introduced_by="owner/root-package",
                    )
                ],
                logger,
                allow_insecure=False,
                allow_insecure_hosts=(),
            )

        message = str(exc_info.value)
        assert message.startswith(
            "Re-run with --allow-insecure-host mirror.example.com"
        )
        assert "unapproved host(s): mirror.example.com" in message

    def test_transitive_guard_allows_same_host_as_direct_insecure_dependency(self):
        """A direct insecure dependency host also permits transitive deps on that host."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _guard_transitive_insecure_dependencies,
        )
        logger = MagicMock()

        _guard_transitive_insecure_dependencies(
            [
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/direct",
                    is_transitive=False,
                    introduced_by=None,
                ),
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/transitive",
                    is_transitive=True,
                    introduced_by="owner/root-package",
                ),
            ],
            logger,
            allow_insecure=True,
            allow_insecure_hosts=(),
        )

    def test_transitive_guard_accepts_explicit_host(self):
        """Explicitly allowed hosts permit transitive insecure dependencies."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _guard_transitive_insecure_dependencies,
        )
        logger = MagicMock()

        _guard_transitive_insecure_dependencies(
            [
                _InsecureDependencyInfo(
                    url="http://mirror.example.com/acme/transitive",
                    is_transitive=True,
                    introduced_by="owner/root-package",
                )
            ],
            logger,
            allow_insecure=False,
            allow_insecure_hosts=("mirror.example.com",),
        )

    def test_transitive_guard_blocks_different_host_without_explicit_allowance(self):
        """A direct insecure host does not permit transitive deps on other hosts."""
        from apm_cli.commands.install import (
            _InsecureDependencyInfo,
            _guard_transitive_insecure_dependencies,
        )
        from apm_cli.install.insecure_policy import InsecureDependencyPolicyError
        logger = MagicMock()

        with pytest.raises(InsecureDependencyPolicyError) as exc_info:
            _guard_transitive_insecure_dependencies(
                [
                    _InsecureDependencyInfo(
                        url="http://my-server.example.com/acme/direct",
                        is_transitive=False,
                        introduced_by=None,
                    ),
                    _InsecureDependencyInfo(
                        url="http://mirror.example.com/acme/transitive",
                        is_transitive=True,
                        introduced_by="owner/root-package",
                    ),
                ],
                logger,
                allow_insecure=True,
                allow_insecure_hosts=(),
            )

        message = str(exc_info.value)
        assert message.startswith(
            "Re-run with --allow-insecure-host mirror.example.com"
        )
        assert "unapproved host(s): mirror.example.com" in message


# ---------------------------------------------------------------------------
# `apm install --mcp NAME ...` flag tests (W3 T-install)
# ---------------------------------------------------------------------------


class TestInstallMcpFlag:
    """End-to-end Click tests for the --mcp flag on `apm install`."""

    def setup_method(self):
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            os.chdir(str(Path(__file__).parent.parent.parent))

    @contextlib.contextmanager
    def _chdir_with_apm_yml(self):
        """Provision a tmp dir with a minimal apm.yml; chdir into it."""
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with open("apm.yml", "w", encoding="utf-8") as fh:
                    yaml.safe_dump(
                        {
                            "name": "demo",
                            "version": "0.1.0",
                            "description": "",
                            "author": "",
                            "dependencies": {"apm": [], "mcp": []},
                            "scripts": {},
                        },
                        fh,
                        sort_keys=False,
                    )
                yield Path(tmp)
            finally:
                os.chdir(self.original_dir)

    # --- Argv `--` boundary handling ---

    def test_mcp_with_double_dash_collects_stdio_command(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo", "--",
                                 "npx", "-y", "server-foo"]), \
             patch("apm_cli.commands.install.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo", "--",
                      "npx", "-y", "server-foo"],
            )
            assert result.exit_code == 0, result.output
            assert "Added MCP server 'foo'" in result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            mcp = data["dependencies"]["mcp"][0]
            assert mcp["name"] == "foo"
            assert mcp["registry"] is False
            assert mcp["transport"] == "stdio"
            assert mcp["command"] == "npx"
            assert mcp["args"] == ["-y", "server-foo"]

    def test_mcp_remote_http(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "api",
                                 "--transport", "http",
                                 "--url", "https://x.example/mcp"]), \
             patch("apm_cli.commands.install.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "api",
                      "--transport", "http",
                      "--url", "https://x.example/mcp"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            mcp = data["dependencies"]["mcp"][0]
            assert mcp["url"] == "https://x.example/mcp"
            assert mcp["transport"] == "http"

    def test_mcp_env_repeats_collect_into_dict(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--env", "A=1", "--env", "B=2",
                                 "--", "srv"]), \
             patch("apm_cli.commands.install.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--env", "A=1", "--env", "B=2",
                      "--", "srv"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            assert data["dependencies"]["mcp"][0]["env"] == {"A": "1", "B": "2"}

    def test_mcp_header_repeats_collect_into_dict(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "api",
                                 "--url", "https://x/y",
                                 "--header", "X-A=1",
                                 "--header", "X-B=2"]), \
             patch("apm_cli.commands.install.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "api",
                      "--url", "https://x/y",
                      "--header", "X-A=1",
                      "--header", "X-B=2"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            assert data["dependencies"]["mcp"][0]["headers"] == {"X-A": "1", "X-B": "2"}

    def test_mcp_registry_shorthand_no_overlays_persists_bare_string(self):
        # Bare registry shorthand (no --transport, --url, --mcp-version,
        # --registry, post-`--` argv) is a documented happy path; the
        # builder returns ``str``, and the install path must not introspect
        # the entry as a dict.
        ref = "io.github.github/github-mcp-server"
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", ref]), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"):
            result = self.runner.invoke(cli, ["install", "--mcp", ref])
            assert result.exit_code == 0, result.output
            assert "'str' object has no attribute" not in result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            # Bare-string serialization is the apm.yml UX contract for
            # shorthand-with-no-overlays; do not silently promote to a dict.
            assert data["dependencies"]["mcp"] == [ref]

    def test_mcp_integration_failure_exits_1_with_redacted_message(self):
        """Partial-failure (apm.yml mutated, integrator raised) must exit 1
        with an actionable string -- not exit 0 with a warning that includes
        a raw exception. CI must see a red run on this code path."""
        ref = "io.github.example/srv"
        boom = RuntimeError("internal token=ghp_SECRET path=/tmp/x.yml")
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", ref]), \
             patch("apm_cli.install.mcp.command.MCPIntegrator") as mock_integ:
            mock_integ.install.side_effect = boom
            result = self.runner.invoke(cli, ["install", "--mcp", ref])
            assert result.exit_code != 0, result.output
            # Raw exception details must NOT appear at default log level.
            assert "ghp_SECRET" not in result.output
            assert "/tmp/x.yml" not in result.output
            # Actionable fixed string is shown instead.
            assert "tool integration failed" in result.output
            assert "--verbose" in result.output

    # --- Conflict matrix E1-E14 ---

    def test_e1_mcp_with_positional_packages(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo", "owner/repo"],
            )
            assert result.exit_code == 2
            assert "cannot mix --mcp with positional packages" in result.output

    def test_e2_mcp_with_global(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--mcp", "foo", "--global"])
            assert result.exit_code == 2
            assert "project-scoped" in result.output

    def test_e3_mcp_with_only_apm(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo", "--only", "apm"],
            )
            assert result.exit_code == 2
            assert "--only apm" in result.output

    def test_e4_mcp_with_ssh(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--mcp", "foo", "--ssh"])
            assert result.exit_code == 2
            assert "transport selection flags" in result.output

    def test_e5_mcp_with_update(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--mcp", "foo", "--update"])
            assert result.exit_code == 2
            assert "apm update" in result.output

    def test_e7_mcp_empty_name(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--mcp", ""])
            assert result.exit_code == 2
            assert "MCP name cannot be empty" in result.output

    def test_e8_mcp_name_starts_with_dash(self):
        with self._chdir_with_apm_yml():
            # Use --mcp=-foo so Click does not interpret -foo as a flag.
            result = self.runner.invoke(cli, ["install", "--mcp=-foo"])
            assert result.exit_code == 2
            assert "cannot start with '-'" in result.output

    def test_e9_header_without_url(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--header", "X-A=1"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo", "--header", "X-A=1"],
            )
            assert result.exit_code == 2
            assert "--header requires --url" in result.output

    def test_e10_transport_without_mcp(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--transport", "http"])
            assert result.exit_code == 2
            assert "--transport requires --mcp" in result.output

    def test_e10_url_without_mcp(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(cli, ["install", "--url", "https://x"])
            assert result.exit_code == 2
            assert "--url requires --mcp" in result.output

    def test_e11_url_with_stdio_command(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--url", "https://x", "--", "npx", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--url", "https://x", "--", "npx", "srv"],
            )
            assert result.exit_code == 2
            assert "--url and a stdio command" in result.output

    def test_e12_stdio_transport_with_url(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--transport", "stdio", "--url", "https://x"],
            )
            assert result.exit_code == 2
            assert "stdio transport doesn't accept --url" in result.output

    def test_e13_remote_transport_with_command(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--transport", "http", "--", "npx", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--transport", "http", "--", "npx", "srv"],
            )
            assert result.exit_code == 2
            assert "remote transports don't accept stdio command" in result.output

    def test_e14_env_with_url(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "api",
                                 "--url", "https://x/y", "--env", "A=1"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "api",
                      "--url", "https://x/y", "--env", "A=1"],
            )
            assert result.exit_code == 2
            assert "use --header for remote" in result.output

    def test_invalid_env_pair_format(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--env", "BAD_NO_EQUALS", "--", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--env", "BAD_NO_EQUALS", "--", "srv"],
            )
            assert result.exit_code == 2
            assert "expected KEY=VALUE" in result.output

    # --- Dry-run path ---

    def test_dry_run_does_not_modify_apm_yml(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--dry-run", "--", "npx", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--dry-run", "--", "npx", "srv"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            assert data["dependencies"]["mcp"] == []
            assert "would add MCP server 'foo'" in result.output

    # --- Validator path: bad NAME via shared MCPDependency.validate ---

    def test_invalid_mcp_name_shape(self):
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "bad name!",
                                 "--", "npx", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "bad name!",
                      "--", "npx", "srv"],
            )
            assert result.exit_code == 2
            assert "Invalid MCP dependency name" in result.output

    # --- --registry flag (PR #810 follow-up 4a) ---

    def test_registry_https_url_persisted_to_apm_yml(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "srv",
                                 "--registry", "https://mcp.internal.example.com"]), \
             patch("apm_cli.commands.install.MCPIntegrator"), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "https://mcp.internal.example.com"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            mcp = data["dependencies"]["mcp"][0]
            # Promoted to dict form so the URL is captured.
            assert isinstance(mcp, dict)
            assert mcp["name"] == "srv"
            assert mcp["registry"] == "https://mcp.internal.example.com"

    def test_registry_http_url_accepted_for_enterprise(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "srv",
                                 "--registry", "http://mcp.internal.local"]), \
             patch("apm_cli.commands.install.MCPIntegrator"), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "http://mcp.internal.local"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            mcp = data["dependencies"]["mcp"][0]
            assert mcp["registry"] == "http://mcp.internal.local"

    def test_registry_normalizes_trailing_slash(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "srv",
                                 "--registry", "https://mcp.example.com/"]), \
             patch("apm_cli.commands.install.MCPIntegrator"), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "https://mcp.example.com/"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            assert data["dependencies"]["mcp"][0]["registry"] == \
                "https://mcp.example.com"

    def test_registry_file_scheme_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "file:///etc/passwd"],
            )
            assert result.exit_code == 2
            assert "--registry" in result.output
            # file:///path has no netloc -> rejected on missing host.
            # file://host/path would also be rejected on scheme allowlist.
            assert "Invalid URL" in result.output

    def test_registry_file_with_host_scheme_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "file://host/etc/passwd"],
            )
            assert result.exit_code == 2
            assert "scheme 'file'" in result.output

    def test_registry_ws_scheme_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "ws://mcp.example.com"],
            )
            assert result.exit_code == 2
            assert "--registry" in result.output
            assert "scheme 'ws'" in result.output

    def test_registry_javascript_scheme_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "javascript:alert(1)"],
            )
            assert result.exit_code == 2
            assert "--registry" in result.output

    def test_registry_empty_string_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv", "--registry", ""],
            )
            assert result.exit_code == 2
            assert "--registry" in result.output
            assert "cannot be empty" in result.output

    def test_registry_schemeless_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--registry", "mcp.example.com"],
            )
            assert result.exit_code == 2
            assert "scheme://host" in result.output

    def test_registry_with_self_defined_url_rejected(self):
        # E15: --registry only applies to registry-resolved entries.
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "api",
                                 "--url", "https://x/y",
                                 "--registry", "https://r/"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "api",
                      "--url", "https://x/y",
                      "--registry", "https://r/"],
            )
            assert result.exit_code == 2
            assert "--registry only applies" in result.output

    def test_registry_with_stdio_command_rejected(self):
        # E15: --registry incompatible with self-defined stdio.
        with self._chdir_with_apm_yml(), \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "foo",
                                 "--registry", "https://r/", "--", "npx", "srv"]):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "foo",
                      "--registry", "https://r/", "--", "npx", "srv"],
            )
            assert result.exit_code == 2
            assert "--registry only applies" in result.output

    def test_registry_without_mcp_rejected(self):
        with self._chdir_with_apm_yml():
            result = self.runner.invoke(
                cli, ["install", "--registry", "https://r/"],
            )
            assert result.exit_code == 2
            assert "--registry requires --mcp" in result.output

    def test_registry_flag_overrides_env_var(self):
        # Precedence: CLI --registry beats MCP_REGISTRY_URL env var.
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "srv",
                                 "--registry", "https://flag.example.com"]), \
             patch("apm_cli.commands.install.MCPIntegrator"), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"), \
             patch.dict(os.environ, {"MCP_REGISTRY_URL": "https://env.example.com"}):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv", "--verbose",
                      "--registry", "https://flag.example.com"],
            )
            assert result.exit_code == 0, result.output
            data = yaml.safe_load((tmp / "apm.yml").read_text())
            assert data["dependencies"]["mcp"][0]["registry"] == \
                "https://flag.example.com"

    def test_registry_with_version_overlay_persists_both(self):
        with self._chdir_with_apm_yml() as tmp, \
             patch("apm_cli.commands.install._get_invocation_argv",
                   return_value=["apm", "install", "--mcp", "srv",
                                 "--mcp-version", "1.2.3",
                                 "--registry", "https://mcp.example.com"]), \
             patch("apm_cli.commands.install.MCPIntegrator"), \
             patch("apm_cli.install.mcp.command.MCPIntegrator"):
            result = self.runner.invoke(
                cli, ["install", "--mcp", "srv",
                      "--mcp-version", "1.2.3",
                      "--registry", "https://mcp.example.com"],
            )
            assert result.exit_code == 0, result.output
            mcp = yaml.safe_load((tmp / "apm.yml").read_text())[
                "dependencies"]["mcp"][0]
            assert mcp["version"] == "1.2.3"
            assert mcp["registry"] == "https://mcp.example.com"

