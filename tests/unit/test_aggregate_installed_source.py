"""Installed source identity is distinct from acquisition trust."""

from urllib.parse import urlparse

import pytest

from apm_cli.models.apm_package import APMPackage, restore_installed_package_source
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("source", [None, "git", "local", "registry"])
def test_restore_installed_source_only_changes_git_identity(source: str | None) -> None:
    """Local and registry identities must never become Git acquisition claims."""
    package = APMPackage(name="fixture", version="1.0.0", source="authored")
    dependency = DependencyReference(repo_url="fixture/package", source=source)

    restore_installed_package_source(package, dependency)

    if source in (None, "git"):
        assert urlparse(package.source) == urlparse(dependency.to_github_url())
    else:
        assert package.source == "authored"
