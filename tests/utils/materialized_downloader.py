"""Downloader seam for tests with package-specific source trees."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference


def stub_materialized_downloader(
    downloader_class: MagicMock,
    modules: Path,
    *,
    commit: str = "0" * 40,
) -> None:
    """Copy each authored package to the requested path, including staging."""

    def resolve(dep_ref: DependencyReference, **_kwargs: object) -> ResolvedReference:
        return ResolvedReference(
            original_ref=dep_ref.reference or "main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=commit,
            ref_name=dep_ref.reference or "main",
        )

    def download(
        dep_ref: DependencyReference, install_path: Path, **_kwargs: object
    ) -> PackageInfo:
        source = dep_ref.get_install_path(modules)
        assert (source / "apm.yml").is_file(), f"Missing authored fixture: {source}"
        if source.resolve() != install_path.resolve():
            shutil.copytree(source, install_path, dirs_exist_ok=True)
        return PackageInfo(
            package=APMPackage.from_apm_yml(install_path / "apm.yml"),
            install_path=install_path,
            resolved_reference=resolve(dep_ref),
            dependency_ref=dep_ref,
            package_type=PackageType.APM_PACKAGE,
        )

    downloader = downloader_class.return_value
    downloader.resolve_git_reference.side_effect = resolve
    downloader.download_package.side_effect = download
