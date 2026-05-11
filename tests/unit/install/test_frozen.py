"""Unit tests for ``InstallService._enforce_frozen``.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.dependency.reference import DependencyReference


def _write_lockfile(project_dir: Path, deps: list[LockedDependency]) -> None:
    lock = LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )
    for dep in deps:
        lock.add_dependency(dep)
    (project_dir / "apm.lock.yaml").write_text(lock.to_yaml())


def _write_apm_yml(project_dir: Path) -> None:
    (project_dir / "apm.yml").write_text("name: test\nversion: 1.0.0\n")


def _make_request(*, project_dir: Path, manifest_deps: list[DependencyReference]) -> InstallRequest:
    pkg = MagicMock()
    pkg.package_path = project_dir / "apm.yml"
    pkg.get_apm_dependencies.return_value = manifest_deps
    pkg.get_dev_apm_dependencies.return_value = []
    return InstallRequest(apm_package=pkg, frozen=True)


class TestEnforceFrozen:
    def test_raises_when_lockfile_missing(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        req = _make_request(project_dir=tmp_path, manifest_deps=[])

        with pytest.raises(FrozenInstallError, match=r"requires apm\.lock\.yaml"):
            InstallService._enforce_frozen(req)

    def test_raises_when_manifest_dep_missing_from_lockfile(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [])
        dep = DependencyReference(repo_url="https://github.com/declared/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        with pytest.raises(FrozenInstallError, match="out of sync") as exc_info:
            InstallService._enforce_frozen(req)

        assert any("declared/r" in r for r in exc_info.value.reasons)

    def test_succeeds_when_lockfile_has_all_manifest_deps(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService._enforce_frozen(req)

    def test_orphan_lockfile_entries_dont_fail(self, tmp_path: Path):
        """Mirrors npm ci: extra lock entries are tolerated; only direct deps must be present."""
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
                LockedDependency(
                    repo_url="https://github.com/orphan/r",
                    resolved_ref="main",
                    resolved_commit="b" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService._enforce_frozen(req)
