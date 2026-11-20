"""x-* manifest extensions are kept and other unknown keys warn (#3219) (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.deps.installed_package import InstalledPackage, extensions_from_resolved_package
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import (
    DependencyReference,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-read-only-lockfile-path", "P-OWN-apm-package-manifest-construction"),
]


class TestLockfileExtensions:
    def test_yaml_round_trip_preserves_order(self) -> None:
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="a" * 40,
            extensions={"x-zeta": 1, "x-alpha": {"nested": "keep"}},
        )

        text = LockFile(dependencies={"owner/repo": dep}).to_yaml()

        assert "extensions:" in text
        assert text.index("x-zeta:") < text.index("x-alpha:")
        restored = LockFile.from_yaml(text).get_package_dependencies()[0]
        assert list(restored.extensions) == ["x-zeta", "x-alpha"]
        assert restored.extensions["x-alpha"] == {"nested": "keep"}
        assert "extensions" not in restored._unknown_fields

    def test_empty_extensions_omitted(self) -> None:
        dep = LockedDependency(repo_url="owner/repo", extensions={})

        assert "extensions" not in dep.to_dict()
        text = LockFile(dependencies={"owner/repo": dep}).to_yaml()
        assert "extensions:" not in text

    def test_from_installed_packages_carries_extensions(self) -> None:
        entry = InstalledPackage(
            dep_ref=DependencyReference(repo_url="owner/repo", reference="main"),
            resolved_commit="abc",
            depth=1,
            resolved_by=None,
            extensions={"x-team": "platform"},
        )

        locked = LockFile.from_installed_packages([entry], None).get_package_dependencies()[0]

        assert locked.extensions == {"x-team": "platform"}
        assert "extensions" in locked.to_dict()

    def test_from_installed_packages_omits_when_empty(self) -> None:
        entry = InstalledPackage(
            dep_ref=DependencyReference(repo_url="owner/repo", reference="main"),
            resolved_commit="abc",
            depth=1,
            resolved_by=None,
        )

        locked = LockFile.from_installed_packages([entry], None).get_package_dependencies()[0]

        assert locked.extensions == {}
        assert "extensions" not in locked.to_dict()

    def test_non_mapping_extensions_do_not_raise(self) -> None:
        raw = {"repo_url": "owner/repo", "extensions": "nope"}

        dep = LockedDependency.from_dict(raw)

        assert dep.extensions == {}
        assert "extensions" not in dep._unknown_fields

    def test_from_dict_copies_the_mapping(self) -> None:
        raw = {"repo_url": "owner/repo", "extensions": {"x-a": 1}}

        dep = LockedDependency.from_dict(raw)
        raw["extensions"]["x-b"] = 2

        assert list(dep.extensions) == ["x-a"]

    def test_helper_copies_package_extensions(self) -> None:
        class _Package:
            def __init__(self) -> None:
                self.extensions = {"x-a": 1}

        package = _Package()
        copied = extensions_from_resolved_package(package)
        copied["x-b"] = 2

        assert package.extensions == {"x-a": 1}
        assert extensions_from_resolved_package(None) == {}
