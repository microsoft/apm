"""Integration tests for generic git URL support with real repositories.

Tests that APM can install packages from real git URLs using:
- HTTPS git URLs (github.com)
- SSH git URLs (git@github.com:...)
- Object-style entries with git URL + path

These tests require network access and valid GitHub credentials.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import APMPackage, DependencyReference  # noqa: F401


@pytest.mark.integration
class TestGenericGitUrlInstallation:
    """Integration tests for installing packages via generic git URLs."""

    def setup_method(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.original_dir = Path.cwd()
        os.chdir(self.test_dir)
        self.apm_yml_path = self.test_dir / "apm.yml"

    def teardown_method(self):
        os.chdir(self.original_dir)
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_apm_yml(self, deps):
        """Write an apm.yml with the given dependency list (strings and/or dicts)."""
        config = {"name": "integration-test", "version": "1.0.0", "dependencies": {"apm": deps}}
        with open(self.apm_yml_path, "w") as f:
            yaml.dump(config, f)

    # -----------------------------------------------------------------------
    # HTTPS git URL
    # -----------------------------------------------------------------------

    def test_https_git_url_github(self):
        """Install microsoft/apm-sample-package via full HTTPS git URL."""
        self._write_apm_yml(["https://github.com/microsoft/apm-sample-package.git"])

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm-sample-package"

        dl = GitHubPackageDownloader()
        install_dir = self.test_dir / "apm_modules" / "microsoft" / "apm-sample-package"
        install_dir.mkdir(parents=True)
        result = dl.download_package(str(dep), install_dir)

        assert install_dir.exists()
        assert (install_dir / "apm.yml").exists()
        assert result.package.name == "apm-sample-package"

    # -----------------------------------------------------------------------
    # SSH git URL
    # -----------------------------------------------------------------------

    def test_ssh_git_url_github(self):
        """Install microsoft/apm-sample-package via SSH git URL."""
        self._write_apm_yml(["git@github.com:microsoft/apm-sample-package.git"])

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm-sample-package"

        dl = GitHubPackageDownloader()
        install_dir = self.test_dir / "apm_modules" / "microsoft" / "apm-sample-package"
        install_dir.mkdir(parents=True)
        result = dl.download_package(str(dep), install_dir)

        assert install_dir.exists()
        assert (install_dir / "apm.yml").exists()
        assert result.package.name == "apm-sample-package"

    # -----------------------------------------------------------------------
    # Object-style: git URL + path (virtual sub-path)
    # -----------------------------------------------------------------------

    def test_object_format_git_url_with_path(self):
        """Install a skill from awesome-copilot using object format with path."""
        self._write_apm_yml(
            [
                {"git": "https://github.com/github/awesome-copilot.git", "path": "skills/aspire"},
            ]
        )

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "github.com"
        assert dep.repo_url == "github/awesome-copilot"
        assert dep.virtual_path == "skills/aspire"
        assert dep.is_virtual is True

        dl = GitHubPackageDownloader()
        # Virtual packages install at the full path including the virtual sub-path
        install_dir = (
            self.test_dir / "apm_modules" / "github" / "awesome-copilot" / "skills" / "aspire"
        )
        install_dir.mkdir(parents=True)
        result = dl.download_package(str(dep), install_dir)  # noqa: F841

        assert install_dir.exists()
        assert (install_dir / "SKILL.md").exists()

    # -----------------------------------------------------------------------
    # Object-style: git URL + path + ref
    # -----------------------------------------------------------------------

    def test_object_format_with_ref(self):
        """Install with pinned ref via object format."""
        self._write_apm_yml(
            [
                {
                    "git": "https://github.com/github/awesome-copilot.git",
                    "path": "skills/review-and-refactor",
                    "ref": "main",
                },
            ]
        )

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        dep = deps[0]
        assert dep.reference == "main"
        assert dep.virtual_path == "skills/review-and-refactor"

        dl = GitHubPackageDownloader()
        install_dir = (
            self.test_dir
            / "apm_modules"
            / "github"
            / "awesome-copilot"
            / "skills"
            / "review-and-refactor"
        )
        install_dir.mkdir(parents=True)
        result = dl.download_package(str(dep), install_dir)  # noqa: F841

        assert install_dir.exists()
        assert (install_dir / "SKILL.md").exists()

    # -----------------------------------------------------------------------
    # Mixed: string + object in same manifest
    # -----------------------------------------------------------------------

    def test_mixed_string_and_object_deps(self):
        """Install a mix of string shorthand and object-style deps."""
        self._write_apm_yml(
            [
                "microsoft/apm-sample-package",
                {"git": "https://github.com/github/awesome-copilot.git", "path": "skills/aspire"},
            ]
        )

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 2

        # First: string shorthand
        assert deps[0].repo_url == "microsoft/apm-sample-package"
        assert deps[0].is_virtual is False

        # Second: object format
        assert deps[1].repo_url == "github/awesome-copilot"
        assert deps[1].virtual_path == "skills/aspire"
        assert deps[1].is_virtual is True


@pytest.mark.integration
class TestNormalizeOnWriteRoundtrip:
    """Integration tests for normalize-on-write CLI roundtrip.

    Verifies that:
    - apm install <URL> stores canonical form in apm.yml (not raw input)
    - apm uninstall <shorthand> finds and removes the canonical entry
    - Duplicate detection works across input forms
    """

    def setup_method(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.original_dir = Path.cwd()
        os.chdir(self.test_dir)
        self.apm_yml_path = self.test_dir / "apm.yml"

    def teardown_method(self):
        os.chdir(self.original_dir)
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_apm_yml(self, deps=None):
        """Write a minimal apm.yml."""
        config = {"name": "roundtrip-test", "version": "1.0.0", "dependencies": {"apm": deps or []}}
        with open(self.apm_yml_path, "w") as f:
            yaml.dump(config, f)

    # -----------------------------------------------------------------------
    # Normalize-on-write: HTTPS URL → canonical shorthand
    # -----------------------------------------------------------------------

    def test_install_https_url_stores_canonical(self):
        """apm install https://github.com/o/r.git → apm.yml stores 'o/r'."""
        from unittest.mock import patch

        self._write_apm_yml()

        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

            validated, _outcome = _validate_and_add_packages_to_apm_yml(
                ["https://github.com/microsoft/apm-sample-package.git"]
            )

        assert validated == ["microsoft/apm-sample-package"]
        data = yaml.safe_load(self.apm_yml_path.read_text())
        assert "microsoft/apm-sample-package" in data["dependencies"]["apm"]
        # Verify raw URL is NOT stored
        assert (
            "https://github.com/microsoft/apm-sample-package.git" not in data["dependencies"]["apm"]
        )

    # -----------------------------------------------------------------------
    # Normalize-on-write: SSH URL → canonical shorthand
    # -----------------------------------------------------------------------

    def test_install_ssh_url_stores_canonical(self):
        """apm install git@github.com:o/r.git → apm.yml stores 'o/r'."""
        from unittest.mock import patch

        self._write_apm_yml()

        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

            validated, _outcome = _validate_and_add_packages_to_apm_yml(
                ["git@github.com:microsoft/apm-sample-package.git"]
            )

        assert validated == ["microsoft/apm-sample-package"]
        data = yaml.safe_load(self.apm_yml_path.read_text())
        assert "microsoft/apm-sample-package" in data["dependencies"]["apm"]

    # -----------------------------------------------------------------------
    # Duplicate detection across input forms
    # -----------------------------------------------------------------------

    def test_no_duplicate_when_already_in_canonical_form(self):
        """Installing 'o/r' when 'o/r' already exists → no duplicate."""
        from unittest.mock import patch

        self._write_apm_yml(["microsoft/apm-sample-package"])

        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

            validated, _outcome = _validate_and_add_packages_to_apm_yml(
                ["microsoft/apm-sample-package"]
            )

        assert validated == []
        data = yaml.safe_load(self.apm_yml_path.read_text())
        assert data["dependencies"]["apm"].count("microsoft/apm-sample-package") == 1

    def test_no_duplicate_when_url_matches_existing_canonical(self):
        """Installing HTTPS URL when shorthand already exists → no duplicate."""
        from unittest.mock import patch

        self._write_apm_yml(["microsoft/apm-sample-package"])

        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

            validated, _outcome = _validate_and_add_packages_to_apm_yml(
                ["https://github.com/microsoft/apm-sample-package.git"]
            )

        assert validated == []
        data = yaml.safe_load(self.apm_yml_path.read_text())
        # Should still be exactly 1 entry
        apm_deps = data["dependencies"]["apm"]
        assert len(apm_deps) == 1
        assert apm_deps[0] == "microsoft/apm-sample-package"

    # -----------------------------------------------------------------------
    # Parse-and-re-parse stability
    # -----------------------------------------------------------------------

    def test_canonical_form_stable_on_reparse(self):
        """Canonical form stored in apm.yml is stable when reparsed."""
        # Store canonical form
        self._write_apm_yml(["microsoft/apm-sample-package"])

        # Parse back
        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1

        # Re-canonicalize → should be identical
        assert deps[0].to_canonical() == "microsoft/apm-sample-package"

    def test_canonical_with_host_stable(self):
        """Non-default host canonical form is stable on reparse."""
        self._write_apm_yml(["gitlab.com/acme/standards"])

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].host == "gitlab.com"
        assert deps[0].repo_url == "acme/standards"
        assert deps[0].to_canonical() == "gitlab.com/acme/standards"

    # -----------------------------------------------------------------------
    # Real-repo: parse → download → verify
    # -----------------------------------------------------------------------

    def test_canonical_stored_entry_installs_correctly(self):
        """A canonical entry in apm.yml can be downloaded successfully."""
        self._write_apm_yml(["microsoft/apm-sample-package"])

        pkg = APMPackage.from_apm_yml(self.apm_yml_path)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1

        dl = GitHubPackageDownloader()
        install_dir = self.test_dir / "apm_modules" / "microsoft" / "apm-sample-package"
        install_dir.mkdir(parents=True)
        result = dl.download_package(str(deps[0]), install_dir)

        assert install_dir.exists()
        assert (install_dir / "apm.yml").exists()
        assert result.package.name == "apm-sample-package"
