"""Helper for resolving APM dependency declaration order from apm.yml / apm.lock."""

from pathlib import Path
from typing import Any

from ..deps.lockfile import LockFile
from ..models.apm_package import APMPackage


def get_dependency_declaration_order(
    base_dir: str,
    *,
    apm_package_cls: type[APMPackage] = APMPackage,
    lockfile_cls: type[LockFile] = LockFile,
) -> list[str]:
    """Get APM dependency installed paths in their declaration order.

    The returned list contains the actual installed path for each dependency,
    combining:
    1. Direct dependencies from apm.yml (highest priority, declaration order)
    2. Transitive dependencies from apm.lock (appended after direct deps)

    This ensures transitive dependencies are included in primitive discovery
    and compilation, not just direct dependencies. The installed path differs for:
    - Regular packages: owner/repo (GitHub) or org/project/repo (ADO)
    - Virtual packages: owner/virtual-pkg-name (GitHub) or org/project/virtual-pkg-name (ADO)

    Args:
        base_dir (str): Base directory containing apm.yml.

    Returns:
        List[str]: List of dependency installed paths in declaration order.
    """
    try:
        apm_yml_path = Path(base_dir) / "apm.yml"
        if not apm_yml_path.exists():
            return []

        package = apm_package_cls.from_apm_yml(apm_yml_path)
        apm_dependencies = package.get_apm_dependencies()

        dependency_names = []
        for dep in apm_dependencies:
            path = _extract_installed_path_from_dep(dep)
            dependency_names.append(path)

        lockfile_paths = lockfile_cls.installed_paths_for_project(Path(base_dir))
        direct_set = set(dependency_names)
        for path in lockfile_paths:
            if path not in direct_set:
                dependency_names.append(path)

        return dependency_names

    except Exception as e:
        print(f"Warning: Failed to parse dependency order from apm.yml: {e}")
        return []


def _extract_installed_path_from_dep(dep: Any) -> str:
    """Extract the installed path for a dependency based on its type."""
    if dep.alias:
        return dep.alias
    if dep.is_virtual:
        return _extract_virtual_dependency_path(dep)
    return dep.repo_url


def _extract_virtual_dependency_path(dep: Any) -> str:
    """Extract installed path for a virtual dependency."""
    repo_parts = dep.repo_url.split("/")

    if dep.is_virtual_subdirectory() and dep.virtual_path:
        return _build_virtual_subdirectory_path(repo_parts, dep)
    return _build_virtual_package_path(repo_parts, dep)


def _build_virtual_subdirectory_path(repo_parts: list[str], dep: Any) -> str:
    """Build path for virtual subdirectory packages."""
    if dep.is_azure_devops() and len(repo_parts) >= 3:
        return f"{repo_parts[0]}/{repo_parts[1]}/{repo_parts[2]}/{dep.virtual_path}"
    if len(repo_parts) >= 2:
        return f"{repo_parts[0]}/{repo_parts[1]}/{dep.virtual_path}"
    return dep.virtual_path


def _build_virtual_package_path(repo_parts: list[str], dep: Any) -> str:
    """Build path for virtual file/collection packages."""
    virtual_name = dep.get_virtual_package_name()
    if dep.is_azure_devops() and len(repo_parts) >= 3:
        return f"{repo_parts[0]}/{repo_parts[1]}/{virtual_name}"
    if len(repo_parts) >= 2:
        return f"{repo_parts[0]}/{virtual_name}"
    return virtual_name
