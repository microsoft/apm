"""End-to-end resolver tests with a fake HTTP client.

Confirms the install-side orchestration: list_versions -> pick best -> download
-> verify -> extract -> validate -> build PackageInfo. Failure paths checked:
hash mismatch, no matching version, 401/403 surfaces remediation, 404 surfaces
clear "no package" message.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.registry.client import RegistryClient, RegistryError, VersionEntry
from apm_cli.deps.registry.resolver import (
    RegistryPackageResolver,
    RegistryResolutionError,
)
from apm_cli.models.dependency.reference import DependencyReference


def _make_apm_tarball(name: str = "acme-web-skills", version: str = "1.2.0"):
    """Build a minimal valid APM package tarball + return (bytes, sha256_hex)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        apm_yml = (
            f"name: {name}\nversion: {version}\ndescription: x\nauthor: a\n"
        ).encode()
        ti = tarfile.TarInfo(name="apm.yml")
        ti.size = len(apm_yml)
        tar.addfile(ti, io.BytesIO(apm_yml))
        keep = tarfile.TarInfo(name=".apm/.keep")
        keep.size = 0
        tar.addfile(keep, io.BytesIO(b""))
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _make_apm_zip(name: str = "acme-skill", version: str = "1.0.0"):
    """Build a minimal valid APM package zip + return (bytes, sha256_hex)."""
    import zipfile as _zip

    buf = io.BytesIO()
    apm_yml = f"name: {name}\nversion: {version}\ndescription: x\nauthor: a\n"
    with _zip.ZipFile(buf, mode="w", compression=_zip.ZIP_DEFLATED) as zf:
        zf.writestr("apm.yml", apm_yml)
        zf.writestr(".apm/.keep", "")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _make_resolver(client_double):
    return RegistryPackageResolver(
        {"corp-main": "https://reg.example.com/apm"},
        client_factory=lambda url, auth: client_double,
    )


def _make_dep(version: str = "^1.2") -> DependencyReference:
    return DependencyReference(
        repo_url="acme/web-skills",
        reference=version,
        source="registry",
        registry_name="corp-main",
    )


class TestHappyPath:
    def test_install_full_package(self, tmp_path):
        raw, digest = _make_apm_tarball()
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="1.2.0", digest=f"sha256:{digest}")
        ]
        fake.download_archive.return_value = (raw, "application/gzip")
        fake.archive_url.return_value = (
            "https://reg.example.com/apm/v1/packages/acme/web-skills/versions/1.2.0/download"
        )

        resolver = _make_resolver(fake)
        target = tmp_path / "apm_modules" / "acme" / "web-skills"
        info = resolver.download_package(_make_dep(), target)

        assert info.install_path == target
        assert (target / "apm.yml").exists()
        assert (target / ".apm").is_dir()

        res = resolver.last_resolutions[_make_dep().get_unique_key()]
        assert res.version == "1.2.0"
        assert res.resolved_hash == f"sha256:{digest}"
        assert res.resolved_url.endswith("/versions/1.2.0/download")

    def test_install_zip_archive(self, tmp_path):
        # Anthropic skill format: zip with the same package shape.
        raw, digest = _make_apm_zip()
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="1.0.0", digest=f"sha256:{digest}")
        ]
        fake.download_archive.return_value = (raw, "application/zip")
        fake.archive_url.return_value = (
            "https://reg.example.com/apm/v1/packages/acme/skill/versions/1.0.0/download"
        )

        resolver = _make_resolver(fake)
        target = tmp_path / "apm_modules" / "acme" / "skill"
        info = resolver.download_package(
            DependencyReference(
                repo_url="acme/skill",
                reference="^1.0",
                source="registry",
                registry_name="corp-main",
            ),
            target,
        )
        assert (target / "apm.yml").exists()
        assert (target / ".apm").is_dir()
        assert info.install_path == target

    def test_picks_highest_matching_version(self, tmp_path):
        raw, digest = _make_apm_tarball(version="1.5.3")
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="1.2.0", digest=f"sha256:{digest}"),
            VersionEntry(version="1.5.3", digest=f"sha256:{digest}"),
            VersionEntry(version="2.0.0", digest=f"sha256:{digest}"),
        ]
        fake.download_archive.return_value = (raw, "application/gzip")
        fake.archive_url.return_value = "https://x/download"

        resolver = _make_resolver(fake)
        target = tmp_path / "p"
        resolver.download_package(_make_dep("^1.0"), target)

        # Confirm download_archive was asked for the highest matching version.
        fake.download_archive.assert_called_once_with("acme", "web-skills", "1.5.3")


class TestFailurePaths:
    def test_hash_mismatch_fails_closed(self, tmp_path):
        raw, _ = _make_apm_tarball()
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="1.2.0", digest="sha256:" + "0" * 64)
        ]
        fake.download_archive.return_value = (raw, "application/gzip")
        fake.archive_url.return_value = "https://x"

        resolver = _make_resolver(fake)
        with pytest.raises(Exception) as excinfo:
            resolver.download_package(_make_dep(), tmp_path / "p")
        # Either HashMismatchError or RegistryResolutionError wrapping it.
        assert "mismatch" in str(excinfo.value).lower() or "Hash" in type(
            excinfo.value
        ).__name__

    def test_no_matching_version(self, tmp_path):
        _, digest = _make_apm_tarball()
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="2.0.0", digest=f"sha256:{digest}")
        ]
        resolver = _make_resolver(fake)
        with pytest.raises(RegistryResolutionError, match="no version"):
            resolver.download_package(_make_dep("^1.0"), tmp_path / "p")

    def test_empty_versions_list(self, tmp_path):
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = []
        resolver = _make_resolver(fake)
        with pytest.raises(RegistryResolutionError, match="no versions"):
            resolver.download_package(_make_dep(), tmp_path / "p")

    def test_401_includes_remediation(self, tmp_path):
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.side_effect = RegistryError(
            "auth required",
            status=401,
            url="https://reg.example.com/apm/v1/packages/acme/web-skills/versions",
        )
        resolver = _make_resolver(fake)
        with pytest.raises(RegistryResolutionError) as excinfo:
            resolver.download_package(_make_dep(), tmp_path / "p")
        msg = str(excinfo.value)
        assert "APM_REGISTRY_TOKEN_<NAME>" in msg
        assert "https://reg.example.com/apm" in msg

    def test_404_clear_message(self, tmp_path):
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.side_effect = RegistryError(
            "not found",
            status=404,
            url="https://reg.example.com/apm/v1/packages/acme/web-skills/versions",
        )
        resolver = _make_resolver(fake)
        with pytest.raises(RegistryResolutionError) as excinfo:
            resolver.download_package(_make_dep(), tmp_path / "p")
        assert "no package" in str(excinfo.value)

    def test_unconfigured_registry_name(self, tmp_path):
        # Resolver knows about 'corp-main', dep references 'corp-other'.
        fake = MagicMock(spec=RegistryClient)
        resolver = RegistryPackageResolver(
            {"corp-main": "https://reg.example.com/apm"},
            client_factory=lambda url, auth: fake,
        )
        dep = DependencyReference(
            repo_url="acme/x",
            reference="1.0.0",
            source="registry",
            registry_name="corp-other",
        )
        with pytest.raises(RegistryResolutionError, match="not configured"):
            resolver.download_package(dep, tmp_path / "p")

    def test_non_registry_dep_rejected(self, tmp_path):
        fake = MagicMock(spec=RegistryClient)
        resolver = _make_resolver(fake)
        dep = DependencyReference(
            repo_url="acme/x", reference="1.0.0", source="git"
        )
        with pytest.raises(RegistryResolutionError, match="non-registry"):
            resolver.download_package(dep, tmp_path / "p")

    def test_invalid_semver_constraint_rejected_at_resolver(self, tmp_path):
        # Defense in depth: if a "registry" dep slips past the parser with a
        # branch name, the resolver still rejects it.
        fake = MagicMock(spec=RegistryClient)
        fake.list_versions.return_value = [
            VersionEntry(version="1.0.0", digest="sha256:" + "0" * 64)
        ]
        resolver = _make_resolver(fake)
        dep = DependencyReference(
            repo_url="acme/x",
            reference="main",
            source="registry",
            registry_name="corp-main",
        )
        with pytest.raises(RegistryResolutionError, match="not a valid semver"):
            resolver.download_package(dep, tmp_path / "p")
