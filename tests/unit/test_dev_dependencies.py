"""Tests for devDependencies support: --dev flag, resolver awareness, lockfile is_dev."""

from unittest.mock import MagicMock, Mock

import pytest

from apm_cli.deps.dependency_graph import DependencyNode
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, DependencyReference

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Part 3d: LockedDependency.is_dev field
# ---------------------------------------------------------------------------


class TestLockedDependencyIsDev:
    """Tests for the is_dev field on LockedDependency."""

    def test_is_dev_defaults_to_false(self):
        dep = LockedDependency(repo_url="owner/repo")
        assert dep.is_dev is False

    def test_is_dev_can_be_set_true(self):
        dep = LockedDependency(repo_url="owner/repo", is_dev=True)
        assert dep.is_dev is True

    def test_to_dict_omits_is_dev_when_false(self):
        dep = LockedDependency(repo_url="owner/repo", is_dev=False)
        result = dep.to_dict()
        assert "is_dev" not in result

    def test_to_dict_includes_is_dev_when_true(self):
        dep = LockedDependency(repo_url="owner/repo", is_dev=True)
        result = dep.to_dict()
        assert result["is_dev"] is True

    def test_from_dict_reads_is_dev_true(self):
        data = {"repo_url": "owner/repo", "is_dev": True}
        dep = LockedDependency.from_dict(data)
        assert dep.is_dev is True

    def test_from_dict_defaults_missing_is_dev(self):
        data = {"repo_url": "owner/repo"}
        dep = LockedDependency.from_dict(data)
        assert dep.is_dev is False

    def test_from_dependency_ref_passes_is_dev(self):
        dep_ref = DependencyReference(repo_url="owner/repo", host="github.com")
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None, is_dev=True)
        assert locked.is_dev is True

    def test_from_dependency_ref_defaults_is_dev_false(self):
        dep_ref = DependencyReference(repo_url="owner/repo", host="github.com")
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.is_dev is False

    def test_backward_compat_old_lockfile_no_is_dev(self):
        """Old lockfiles without is_dev deserialize with is_dev=False."""
        yaml_str = (
            'lockfile_version: "1"\n'
            "dependencies:\n"
            "  - repo_url: legacy/dep\n"
            "    resolved_commit: abc123\n"
        )
        lock = LockFile.from_yaml(yaml_str)
        assert lock.dependencies["legacy/dep"].is_dev is False


class TestFromInstalledPackagesIsDev:
    """Tests for LockFile.from_installed_packages with 5-element tuples."""

    def _mock_dep_ref(self, repo_url):
        return DependencyReference(repo_url=repo_url, reference="main")

    def test_5_element_tuple_with_is_dev_true(self):
        dep_ref = self._mock_dep_ref("dev/pkg")
        installed = [(dep_ref, "sha1", 1, None, True)]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.dependencies["dev/pkg"].is_dev is True

    def test_5_element_tuple_with_is_dev_false(self):
        dep_ref = self._mock_dep_ref("prod/pkg")
        installed = [(dep_ref, "sha1", 1, None, False)]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.dependencies["prod/pkg"].is_dev is False

    def test_4_element_tuple_backward_compat(self):
        """Old callers passing 4-element tuples still work (is_dev defaults False)."""
        dep_ref = self._mock_dep_ref("old/pkg")
        installed = [(dep_ref, "sha1", 1, None)]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.dependencies["old/pkg"].is_dev is False

    def test_mixed_prod_and_dev(self):
        prod = self._mock_dep_ref("prod/pkg")
        dev = self._mock_dep_ref("dev/pkg")
        installed = [
            (prod, "sha1", 1, None, False),
            (dev, "sha2", 1, None, True),
        ]
        lock = LockFile.from_installed_packages(installed, Mock())
        assert lock.dependencies["prod/pkg"].is_dev is False
        assert lock.dependencies["dev/pkg"].is_dev is True


# ---------------------------------------------------------------------------
# Part 3c: Resolver devDependencies awareness
# ---------------------------------------------------------------------------


class TestDependencyNodeIsDev:
    """Tests for DependencyNode.is_dev field."""

    def test_is_dev_defaults_false(self):
        pkg = APMPackage(name="test", version="1.0.0")
        ref = DependencyReference(repo_url="owner/repo")
        node = DependencyNode(package=pkg, dependency_ref=ref, depth=1)
        assert node.is_dev is False

    def test_is_dev_can_be_set(self):
        pkg = APMPackage(name="test", version="1.0.0")
        ref = DependencyReference(repo_url="owner/repo")
        node = DependencyNode(package=pkg, dependency_ref=ref, depth=1, is_dev=True)
        assert node.is_dev is True


# ---------------------------------------------------------------------------
# Part 3b: apm install --dev flag
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Regression tests for #2033: devDependencies visible to orphan detection
# ---------------------------------------------------------------------------


class TestGetAllApmDependencies:
    """Tests for APMPackage.get_all_apm_dependencies() and has_any_apm_dependencies()."""

    def test_returns_union_of_prod_and_dev(self) -> None:
        """get_all_apm_dependencies returns both prod and dev deps."""
        pkg = MagicMock(spec=APMPackage)
        prod_dep = DependencyReference(repo_url="owner/prod-pkg")
        dev_dep = DependencyReference(repo_url="owner/dev-pkg")
        pkg.get_apm_dependencies.return_value = [prod_dep]
        pkg.get_dev_apm_dependencies.return_value = [dev_dep]
        # Call the real method on the mock
        result = APMPackage.get_all_apm_dependencies(pkg)
        assert result == [prod_dep, dev_dep]

    def test_returns_only_prod_when_no_dev(self) -> None:
        """get_all_apm_dependencies returns prod-only when no dev deps exist."""
        pkg = MagicMock(spec=APMPackage)
        prod_dep = DependencyReference(repo_url="owner/prod-pkg")
        pkg.get_apm_dependencies.return_value = [prod_dep]
        pkg.get_dev_apm_dependencies.return_value = []
        result = APMPackage.get_all_apm_dependencies(pkg)
        assert result == [prod_dep]

    def test_returns_only_dev_when_no_prod(self) -> None:
        """get_all_apm_dependencies returns dev-only when no prod deps exist."""
        pkg = MagicMock(spec=APMPackage)
        dev_dep = DependencyReference(repo_url="owner/dev-pkg")
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = [dev_dep]
        result = APMPackage.get_all_apm_dependencies(pkg)
        assert result == [dev_dep]

    def test_has_any_true_for_dev_only(self) -> None:
        """has_any_apm_dependencies returns True when only dev deps exist."""
        pkg = MagicMock(spec=APMPackage)
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = [DependencyReference(repo_url="owner/dev-pkg")]
        result = APMPackage.has_any_apm_dependencies(pkg)
        assert result is True

    def test_has_any_false_when_empty(self) -> None:
        """has_any_apm_dependencies returns False when no deps at all."""
        pkg = MagicMock(spec=APMPackage)
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        result = APMPackage.has_any_apm_dependencies(pkg)
        assert result is False


class TestOrphanDetectionIncludesDevDeps:
    """Regression tests for #2033: orphan detection must include devDependencies."""

    def test_check_no_orphans_passes_for_dev_dep(self) -> None:
        """_check_no_orphans should not flag a dev dependency as orphaned."""
        from apm_cli.policy.ci_checks import _check_no_orphans

        manifest = MagicMock(spec=APMPackage)
        manifest.get_apm_dependencies.return_value = []
        manifest.get_dev_apm_dependencies.return_value = [
            DependencyReference(repo_url="owner/dev-pkg")
        ]
        manifest.get_all_apm_dependencies.return_value = APMPackage.get_all_apm_dependencies(
            manifest
        )

        lock = MagicMock(spec=LockFile)
        lock.dependencies = {
            "owner/dev-pkg": LockedDependency(
                repo_url="owner/dev-pkg", is_dev=True, resolved_by=None
            ),
        }

        result = _check_no_orphans(manifest, lock)
        assert result.passed is True

    def test_check_ref_consistency_detects_dev_dep_mismatch(self) -> None:
        """_check_ref_consistency should detect ref mismatches in dev deps."""
        from apm_cli.policy.ci_checks import _check_ref_consistency

        dev_dep = DependencyReference(repo_url="owner/dev-pkg", reference="v2.0")
        manifest = MagicMock(spec=APMPackage)
        manifest.get_apm_dependencies.return_value = []
        manifest.get_dev_apm_dependencies.return_value = [dev_dep]
        manifest.get_all_apm_dependencies.return_value = APMPackage.get_all_apm_dependencies(
            manifest
        )

        lock = MagicMock(spec=LockFile)
        lock.get_dependency.return_value = LockedDependency(
            repo_url="owner/dev-pkg",
            is_dev=True,
            resolved_ref="v1.0",
            resolved_by=None,
        )

        result = _check_ref_consistency(manifest, lock)
        assert result.passed is False
        assert "ref mismatch" in result.message.lower()
