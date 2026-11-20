"""Tests for devDependencies support: --dev flag, resolver awareness, lockfile is_dev (filesystem, process and CLI boundary cases)."""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.results import InstallResult

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Part 3d: LockedDependency.is_dev field
# ---------------------------------------------------------------------------


class TestLockedDependencyIsDev:
    """Tests for the is_dev field on LockedDependency."""

    def test_is_dev_round_trip_yaml(self, tmp_path):
        """is_dev survives a write/read YAML cycle."""
        lock = LockFile()
        lock.add_dependency(LockedDependency(repo_url="prod/dep"))
        lock.add_dependency(LockedDependency(repo_url="dev/dep", is_dev=True))
        lock_path = tmp_path / "apm.lock.yaml"
        lock.write(lock_path)

        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert loaded.dependencies["prod/dep"].is_dev is False
        assert loaded.dependencies["dev/dep"].is_dev is True


# ---------------------------------------------------------------------------
# Part 3c: Resolver devDependencies awareness
# ---------------------------------------------------------------------------


class TestResolverDevDeps:
    """Tests for APMDependencyResolver handling devDependencies."""

    def test_resolver_includes_dev_deps(self, tmp_path):
        """Dev dependencies should appear in the resolved tree."""
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["prod/pkg"]},
                    "devDependencies": {"apm": ["dev/pkg"]},
                }
            )
        )

        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        graph = resolver.resolve_dependencies(tmp_path)

        tree = graph.dependency_tree
        # Both prod and dev deps should be in the tree
        assert tree.has_dependency("prod/pkg")
        assert tree.has_dependency("dev/pkg")

    def test_resolver_marks_dev_deps(self, tmp_path):
        """Dev-only dependencies should have is_dev=True in the tree."""
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["prod/pkg"]},
                    "devDependencies": {"apm": ["dev/pkg"]},
                }
            )
        )

        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        graph = resolver.resolve_dependencies(tmp_path)

        tree = graph.dependency_tree
        prod_node = tree.get_node("prod/pkg")
        dev_node = tree.get_node("dev/pkg")
        assert prod_node is not None
        assert dev_node is not None
        assert prod_node.is_dev is False
        assert dev_node.is_dev is True

    def test_resolver_prod_wins_over_dev(self, tmp_path):
        """A dep in both dependencies and devDependencies should be is_dev=False."""
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["shared/pkg"]},
                    "devDependencies": {"apm": ["shared/pkg"]},
                }
            )
        )

        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        graph = resolver.resolve_dependencies(tmp_path)

        tree = graph.dependency_tree
        node = tree.get_node("shared/pkg")
        assert node is not None
        # Prod takes precedence
        assert node.is_dev is False

    def test_resolver_only_dev_deps(self, tmp_path):
        """When only devDependencies exist, they resolve correctly."""
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "devDependencies": {"apm": ["dev/only"]},
                }
            )
        )

        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        graph = resolver.resolve_dependencies(tmp_path)
        tree = graph.dependency_tree
        node = tree.get_node("dev/only")
        assert node is not None
        assert node.is_dev is True


# ---------------------------------------------------------------------------
# Part 3b: apm install --dev flag
# ---------------------------------------------------------------------------


class TestInstallDevFlag:
    """Tests for the --dev flag on apm install."""

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

    @contextlib.contextmanager
    def _chdir_tmp(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                os.chdir(tmp_dir)
                yield Path(tmp_dir)
            finally:
                os.chdir(self.original_dir)

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_dev_flag_writes_to_dev_dependencies(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """--dev should add packages to devDependencies.apm."""
        from apm_cli.cli import cli

        with self._chdir_tmp():
            # Create minimal apm.yml
            apm_yml = {
                "name": "test-project",
                "version": "1.0.0",
                "dependencies": {"apm": [], "mcp": []},
            }
            with open("apm.yml", "w") as f:
                yaml.dump(apm_yml, f)

            mock_validate.return_value = True

            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = []
            mock_pkg.get_dev_apm_dependencies.return_value = [
                MagicMock(repo_url="test/dev-pkg", reference="main")
            ]
            mock_pkg.get_mcp_dependencies.return_value = []
            mock_pkg.target = None
            mock_apm_package.from_apm_yml.return_value = mock_pkg

            mock_install_apm.return_value = InstallResult(
                diagnostics=MagicMock(
                    has_diagnostics=False, has_critical_security=False, error_count=0
                )
            )

            result = self.runner.invoke(cli, ["install", "--dev", "test/dev-pkg"])
            assert result.exit_code == 0

            with open("apm.yml") as f:
                config = yaml.safe_load(f)
            assert "devDependencies" in config
            assert "test/dev-pkg" in config["devDependencies"]["apm"]
            # Prod dependencies should be untouched
            assert config["dependencies"]["apm"] == []

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True)
    @patch("apm_cli.commands.install.APMPackage")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_no_dev_flag_writes_to_dependencies(
        self, mock_install_apm, mock_apm_package, mock_validate
    ):
        """Without --dev, packages go to dependencies.apm."""
        from apm_cli.cli import cli

        with self._chdir_tmp():
            apm_yml = {
                "name": "test-project",
                "version": "1.0.0",
                "dependencies": {"apm": [], "mcp": []},
            }
            with open("apm.yml", "w") as f:
                yaml.dump(apm_yml, f)

            mock_validate.return_value = True

            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = [
                MagicMock(repo_url="test/prod-pkg", reference="main")
            ]
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_pkg.get_mcp_dependencies.return_value = []
            mock_pkg.target = None
            mock_apm_package.from_apm_yml.return_value = mock_pkg

            mock_install_apm.return_value = InstallResult(
                diagnostics=MagicMock(
                    has_diagnostics=False, has_critical_security=False, error_count=0
                )
            )

            result = self.runner.invoke(cli, ["install", "test/prod-pkg"])
            assert result.exit_code == 0

            with open("apm.yml") as f:
                config = yaml.safe_load(f)
            assert "test/prod-pkg" in config["dependencies"]["apm"]
            assert "devDependencies" not in config


class TestValidateAndAddDevDeps:
    """Tests for _validate_and_add_packages_to_apm_yml with dev=True."""

    def setup_method(self):
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

    @patch("apm_cli.commands.install._validate_package_exists")
    def test_dev_creates_dev_dependencies_section(self, mock_validate, tmp_path):
        """dev=True creates devDependencies.apm if missing."""
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        os.chdir(tmp_path)
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "1.0.0",
                    "dependencies": {"apm": [], "mcp": []},
                }
            )
        )

        mock_validate.return_value = True
        validated, _outcome = _validate_and_add_packages_to_apm_yml(["org/dev-pkg"], dev=True)
        assert "org/dev-pkg" in validated

        with open(apm_yml) as f:
            data = yaml.safe_load(f)
        assert "devDependencies" in data
        assert "org/dev-pkg" in data["devDependencies"]["apm"]
        # Prod deps untouched
        assert data["dependencies"]["apm"] == []

    @patch("apm_cli.commands.install._validate_package_exists")
    def test_dev_false_writes_to_dependencies(self, mock_validate, tmp_path):
        """dev=False (default) writes to dependencies.apm."""
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        os.chdir(tmp_path)
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "1.0.0",
                    "dependencies": {"apm": [], "mcp": []},
                }
            )
        )

        mock_validate.return_value = True
        _validate_and_add_packages_to_apm_yml(["org/prod-pkg"], dev=False)

        with open(apm_yml) as f:
            data = yaml.safe_load(f)
        assert "org/prod-pkg" in data["dependencies"]["apm"]
        assert "devDependencies" not in data


# ---------------------------------------------------------------------------
# Regression tests for #2033: devDependencies visible to orphan detection
# ---------------------------------------------------------------------------


class TestOrphanDetectionIncludesDevDeps:
    """Regression tests for #2033: orphan detection must include devDependencies."""

    def test_check_lockfile_exists_recognises_dev_only_deps(self, tmp_path) -> None:
        """_check_lockfile_exists should not say 'no dependencies' for dev-only projects."""
        from apm_cli.policy.ci_checks import _check_lockfile_exists

        manifest = MagicMock(spec=APMPackage)
        manifest.has_apm_dependencies.return_value = False
        manifest.has_any_apm_dependencies.return_value = True
        manifest.get_mcp_dependencies.return_value = []
        manifest.get_all_mcp_dependencies.return_value = []

        # No lockfile on disk -- should say "lockfile missing", not "no deps"
        result = _check_lockfile_exists(tmp_path, manifest)
        assert result.passed is False
        assert "missing" in result.message.lower()
