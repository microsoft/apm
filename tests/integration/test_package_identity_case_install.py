"""Integration coverage for case-insensitive GitHub package identity."""

from pathlib import Path
from unittest.mock import patch

import yaml

from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


def test_mixed_case_install_resolves_once_without_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Mixed-case install inputs converge before manifest and graph deduplication."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "case-normalization-test",
                "version": "1.0.0",
                "dependencies": {"apm": []},
            }
        ),
        encoding="utf-8",
    )

    with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
        installed, _outcome = _validate_and_add_packages_to_apm_yml(
            ["Owner/Example-Package", "owner/example-package"]
        )

    clear_apm_yml_cache()
    package = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    dependencies = package.get_apm_dependencies()
    graph = APMDependencyResolver(max_parallel=1).resolve_dependencies(tmp_path)
    resolved = graph.flattened_dependencies.get_installation_list()
    locked = LockedDependency.from_dependency_ref(
        resolved[0],
        resolved_commit="abc123",
        depth=1,
        resolved_by=None,
    )
    lockfile = LockFile()
    lockfile.add_dependency(locked)
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile.write(lock_path)
    reloaded_lockfile = LockFile.read(lock_path)

    assert installed == ["owner/example-package"]
    assert [dependency.repo_url for dependency in dependencies] == ["owner/example-package"]
    assert [dependency.get_unique_key() for dependency in resolved] == ["owner/example-package"]
    assert resolved[0].get_install_path(tmp_path / "apm_modules") == (
        tmp_path / "apm_modules" / "owner" / "example-package"
    )
    assert graph.flattened_dependencies.conflicts == []
    assert list(reloaded_lockfile.dependencies) == ["owner/example-package"]
    assert reloaded_lockfile.dependencies["owner/example-package"].repo_url == (
        "owner/example-package"
    )
