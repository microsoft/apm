import tarfile
from urllib.parse import urlparse

import yaml

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.deps.oci_registry import OCIRegistryClient
from apm_cli.models.apm_package import APMPackage, PackageRequirement
from apm_cli.repositories.config import RepositoryDefinition, load_repositories
from apm_cli.repositories.resolver import ArtifactResolver


def test_apm_package_parses_shorthand_as_logical_requirement(tmp_path):
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "version": "1.0.0",
                "dependencies": {
                    "apm": [
                        "microsoft/apm-standards#v1.2.0",
                        {"name": "acme/security-pack", "version": "1.0.0"},
                        "gitlab.com/group/repo",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    package = APMPackage.from_apm_yml(manifest)
    deps = package.get_apm_dependencies()

    assert isinstance(deps[0], PackageRequirement)
    assert deps[0].name == "microsoft/apm-standards"
    assert deps[0].version == "v1.2.0"
    assert isinstance(deps[1], PackageRequirement)
    assert deps[1].name == "acme/security-pack"
    assert deps[2].host == "gitlab.com"


def test_default_repositories_include_git_and_oci(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "apm_cli.repositories.config.repositories_config_path",
        lambda: tmp_path / "missing-repositories.yml",
    )
    repos = load_repositories()
    names = [repo.name for repo in repos]
    assert "github" in names
    assert "gitlab" in names
    assert "ghcr" in names


def test_locked_dependency_tracks_resolved_requirement_metadata():
    req = PackageRequirement(name="acme/security-pack", version="1.0.0")
    req.resolved_source_type = "oci"
    req.resolved_repository = "ghcr"
    req.resolved_ref = "ghcr.io/apm/acme/security-pack:1.0.0"
    req.resolved_digest = "sha256:abc"

    locked = LockedDependency.from_dependency_ref(req, None, 1, None)
    round_trip = LockedDependency.from_dict(locked.to_dict())

    assert round_trip.source_type == "oci"
    assert round_trip.repository_name == "ghcr"
    assert round_trip.oci_repository == "acme/security-pack"
    assert round_trip.oci_tag == "1.0.0"
    assert round_trip.oci_digest == "sha256:abc"
    assert round_trip.get_unique_key() == "acme/security-pack"


def test_artifact_resolver_annotates_requirement_for_git(monkeypatch, tmp_path):
    class StubDownloader:
        def download_package(self, dep_ref, target_path):
            class Result:
                resolved_reference = type(
                    "Resolved", (), {"ref_name": "v1.2.0", "resolved_commit": "deadbeef"}
                )()

            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
            return Result()

    class StubOCI:
        def pull_package(self, resolved_reference, target_path, requirement=None):
            raise AssertionError("OCI backend should not be used in this test")

    resolver = ArtifactResolver(StubDownloader(), StubOCI())
    req = PackageRequirement(name="microsoft/apm-standards", version="v1.2.0", repository="github")
    monkeypatch.setattr(
        "apm_cli.repositories.resolver.load_repositories",
        lambda: [
            RepositoryDefinition(name="github", type="git", base="https://github.com", priority=100)
        ],
    )

    resolver.fetch_requirement(req, tmp_path / "pkg")

    assert req.resolved_source_type == "git"
    assert req.resolved_repository == "github"
    locator, fragment = req.resolved_ref.split("#", 1)
    parsed = urlparse(locator)
    assert parsed.hostname == "github.com"
    assert parsed.path == "/microsoft/apm-standards.git"
    assert fragment == "v1.2.0"


def test_package_requirement_rejects_host_qualified_refs():
    try:
        PackageRequirement.parse("gitlab.com/group/repo")
    except ValueError as exc:
        assert "Host-qualified references" in str(exc)
    else:
        raise AssertionError("Expected host-qualified ref to be rejected")


def test_oci_registry_client_extracts_raw_package_tarball(tmp_path, monkeypatch):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    src_dir = tmp_path / "src"
    pkg_dir = src_dir / "security-pack"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "apm.yml").write_text("name: security-pack\nversion: 1.0.0\n", encoding="utf-8")
    (pkg_dir / ".apm").mkdir()

    archive_path = artifact_dir / "security-pack.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(pkg_dir, arcname=pkg_dir.name)

    def fake_pull(self, resolved_reference, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / archive_path.name
        target.write_bytes(archive_path.read_bytes())

    monkeypatch.setattr(OCIRegistryClient, "_pull_artifact", fake_pull)

    client = OCIRegistryClient()
    req = PackageRequirement(name="acme/security-pack", version="1.0.0", repository="ghcr")
    result = client.pull_package("ghcr.io/apm/acme/security-pack:1.0.0", tmp_path / "install", req)

    assert (result.install_path / "apm.yml").exists()
    assert result.package.name == "security-pack"
