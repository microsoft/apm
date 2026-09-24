"""Real install command regression for collapsed immutable graphs, without network."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef, ResolvedReference

pytestmark = pytest.mark.component

OLD = "a" * 40
NEW = "b" * 40
PARENT = "c" * 40


def _write_package(path: Path, name: str, deps: list[str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "dependencies": {"apm": deps},
                "targets": ["copilot"],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("frozen", [False, True], ids=["normal", "frozen"])
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("compatible", [False, True], ids=["conflict", "equivalent"])
@pytest.mark.parametrize(
    "direct_shared", [False, True], ids=["transitive-only", "direct-transitive"]
)
def test_install_checks_every_immutable_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen: bool,
    warm: bool,
    compatible: bool,
    direct_shared: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    deps = ["org/shared#release"] if direct_shared else []
    _write_package(tmp_path, "consumer", [*deps, f"org/parent#{PARENT}"])
    lock = LockFile()
    for name, ref, commit in (("shared", "release", OLD), ("parent", PARENT, PARENT)):
        lock.add_dependency(
            LockedDependency(
                repo_url=f"org/{name}",
                resolved_ref=ref,
                resolved_commit=commit,
                package_type="apm_package",
                depth=1,
            )
        )
        if warm:
            _write_package(
                tmp_path / "apm_modules" / "org" / name,
                name,
                ["org/shared#alternate"] if name == "parent" else [],
            )
    lock.write(tmp_path / "apm.lock.yaml")
    original_lock = (tmp_path / "apm.lock.yaml").read_bytes()

    def commit_for(dep: DependencyReference) -> str:
        if dep.repo_url == "org/parent":
            return PARENT
        return NEW if dep.reference == "alternate" and not compatible else OLD

    def download(
        _self: GitHubPackageDownloader,
        dep: DependencyReference,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> PackageInfo:
        name = dep.repo_url.rsplit("/", 1)[-1]
        _write_package(path, name, ["org/shared#alternate"] if name == "parent" else [])
        sha = commit_for(dep)
        return PackageInfo(
            package=APMPackage.from_apm_yml(path / "apm.yml"),
            install_path=path,
            dependency_ref=dep,
            package_type=PackageType.APM_PACKAGE,
            resolved_reference=ResolvedReference(
                dep.reference or "", GitReferenceType.COMMIT, sha, dep.reference or ""
            ),
        )

    with (
        patch("apm_cli.commands._helpers.check_for_updates"),
        patch.object(
            GitHubPackageDownloader, "download_package", autospec=True, side_effect=download
        ),
        patch.object(
            GitHubPackageDownloader,
            "list_remote_refs",
            return_value=[
                RemoteRef("release", GitReferenceType.TAG, OLD),
                RemoteRef("alternate", GitReferenceType.TAG, OLD if compatible else NEW),
            ],
        ),
        patch.object(
            GitHubPackageDownloader,
            "resolve_git_reference",
            side_effect=lambda dep: ResolvedReference(
                dep.reference or "",
                GitReferenceType.COMMIT,
                commit_for(dep),
                dep.reference or "",
            ),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "--no-policy", "--target", "copilot", *(["--frozen"] if frozen else [])],
        )

    if compatible or (not frozen and not direct_shared):
        assert result.exit_code == 0, result.output
    else:
        assert result.exit_code == 1, result.output
        assert "incompatible immutable requirements" in result.output
        assert "org/shared@alternate" in result.output
        assert "org/shared@release" in result.output
        assert (tmp_path / "apm.lock.yaml").read_bytes() == original_lock
        assert not (tmp_path / ".github" / "skills").exists()


@pytest.mark.parametrize("compatible", [False, True], ids=["wrong-pin", "valid-pin"])
def test_frozen_install_checks_short_pin_without_ref_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    short = OLD[:8]
    _write_package(tmp_path, "consumer", [f"org/parent#{PARENT}"])
    _write_package(tmp_path / "apm_modules/org/parent", "parent", [f"org/shared#{short}"])
    _write_package(tmp_path / "apm_modules/org/shared", "shared", [])
    lock = LockFile()
    for name, ref, commit in (
        ("parent", PARENT, PARENT),
        ("shared", short, OLD if compatible else NEW),
    ):
        lock.add_dependency(
            LockedDependency(
                repo_url=f"org/{name}",
                resolved_ref=ref,
                resolved_commit=commit,
                package_type="apm_package",
                depth=1 if name == "parent" else 2,
            )
        )
    lock.write(tmp_path / "apm.lock.yaml")
    before = {name: (tmp_path / name).read_bytes() for name in ("apm.yml", "apm.lock.yaml")}

    def download(
        _self: GitHubPackageDownloader,
        dep: DependencyReference,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> PackageInfo:
        name = dep.repo_url.rsplit("/", 1)[-1]
        _write_package(path, name, [f"org/shared#{short}"] if name == "parent" else [])
        commit = PARENT if name == "parent" else (OLD if compatible else NEW)
        return PackageInfo(
            package=APMPackage.from_apm_yml(path / "apm.yml"),
            install_path=path,
            dependency_ref=dep,
            package_type=PackageType.APM_PACKAGE,
            resolved_reference=ResolvedReference(
                dep.reference or "", GitReferenceType.COMMIT, commit, dep.reference or ""
            ),
        )

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch.object(
            GitHubPackageDownloader, "download_package", autospec=True, side_effect=download
        ),
        patch.object(GitHubPackageDownloader, "list_remote_refs") as refs,
        patch.object(GitHubPackageDownloader, "resolve_git_reference") as resolve,
    ):
        result = CliRunner().invoke(
            cli, ["install", "--frozen", "--no-policy", "--target", "copilot"]
        )
    assert result.exit_code == (0 if compatible else 1), result.output
    refs.assert_not_called()
    resolve.assert_not_called()
    if not compatible:
        assert "incompatible immutable requirements" in result.output
        assert "org/shared@aaaaaaaa" in result.output
        assert {name: (tmp_path / name).read_bytes() for name in before} == before
        assert not (tmp_path / ".github").exists()
        assert not (tmp_path / ".claude").exists()
