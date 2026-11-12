"""Existing consumer contracts at the acquired-manifest metadata boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.git_semver_resolver import GitSemverResolution
from apm_cli.deps.installed_package import InstalledPackage
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.resolver import RegistryResolution
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.source_helpers import materialized_manifest_snapshot
from apm_cli.models.apm_package import APMPackage, PackageType
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


@pytest.mark.req("req-mf-001")
@pytest.mark.parametrize("document", ["- not-a-mapping\n", "scalar\n", "null\n"])
def test_acquired_nonmapping_manifest_is_rejected(tmp_path: Path, document: str) -> None:
    """A valid cached descriptor cannot hide a nonmapping acquired document."""
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "demo", "version": "1.0.0"}, manifest)
    stale = APMPackage.from_apm_yml(manifest)
    manifest.write_text(document, encoding="utf-8")
    acquired_bytes = manifest.read_bytes()

    with pytest.raises(DirectDependencyError, match=r"apm\.yml must contain a YAML object"):
        materialized_manifest_snapshot(stale, tmp_path, PackageType.APM_PACKAGE)

    assert manifest.read_bytes() == acquired_bytes
    assert APMPackage.from_apm_yml(manifest) is stale
    assert stale.version == "1.0.0"
    dump_yaml({"name": "demo", "version": "1.1.0"}, manifest)
    snapshot = materialized_manifest_snapshot(stale, tmp_path, PackageType.APM_PACKAGE)
    assert snapshot.version == "1.1.0"


@pytest.mark.req("req-lk-011", "req-lk-019")
def test_snapshot_preserves_manifestless_optional_version(tmp_path: Path) -> None:
    """Formats without apm.yml retain their projection and omit unset inventory."""
    package = APMPackage(name="native", version="", package_path=tmp_path)

    assert materialized_manifest_snapshot(package, tmp_path) is package
    assert package.version == ""
    assert not (tmp_path / "apm.yml").exists()
    installed = InstalledPackage(
        DependencyReference.parse("owner/native"),
        resolved_commit="a" * 40,
        depth=1,
        resolved_by=None,
        package_version=package.version,
    )
    locked = LockFile.from_installed_packages([installed], None).get_all_dependencies()[0]
    assert "version" not in locked.to_dict()
    assert "version" not in LockedDependency.from_dict(locked.to_dict()).to_dict()


@pytest.mark.req("req-lk-019")
@pytest.mark.parametrize("authority", ["manifest", "git-semver", "registry"])
def test_snapshot_preserves_resolver_version_precedence(tmp_path: Path, authority: str) -> None:
    """Recorded inventory round-trips without overriding resolver-selected versions."""
    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "demo", "version": "1.0.0"}, manifest)
    stale = APMPackage.from_apm_yml(manifest)
    dump_yaml({"name": "demo", "version": "1.1.0"}, manifest)
    snapshot = materialized_manifest_snapshot(stale, tmp_path, PackageType.APM_PACKAGE)
    reference = DependencyReference.parse("owner/demo#release-not-a-version")
    installed = InstalledPackage(
        reference,
        resolved_commit="a" * 40,
        depth=1,
        resolved_by=None,
        package_version=snapshot.version,
    )
    if authority == "git-semver":
        installed.git_semver_resolution = GitSemverResolution(
            constraint="^2.0.0",
            resolved_version="2.0.0",
            resolved_tag="demo-release-2.0.0",
            resolved_sha="a" * 40,
            matched_pattern="demo-release-{version}",
            resolved_at="2026-10-20T00:00:00Z",
        )
    elif authority == "registry":
        reference.source = "registry"
        installed.registry_resolution = RegistryResolution(
            resolved_url="https://registry.example.com/demo.tgz",
            resolved_hash="sha256:" + "a" * 64,
            version="2.0.0",
        )

    locked = LockFile.from_installed_packages([installed], None).get_all_dependencies()[0]

    assert snapshot.version == "1.1.0"
    assert locked.version == ("1.1.0" if authority == "manifest" else "2.0.0")
    assert (
        locked.resolved_ref
        == {
            "manifest": "release-not-a-version",
            "git-semver": "demo-release-2.0.0",
            "registry": "2.0.0",
        }[authority]
    )
    assert locked.resolved_commit == "a" * 40
    assert stale.version == "1.0.0"
    restored = LockedDependency.from_dict(locked.to_dict())
    assert restored.version == locked.version
    assert restored.repo_url == locked.repo_url
    assert restored.resolved_commit == locked.resolved_commit
