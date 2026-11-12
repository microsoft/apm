"""Dependencies management package for APM."""

from .apm_resolver import APMDependencyResolver
from .dependency_graph import (
    CircularRef,
    ConflictInfo,
    DependencyGraph,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)
from .github_downloader import GitHubPackageDownloader
from .lockfile import LockedDependency, LockFile, get_lockfile_path
from .package_validator import PackageValidator
from .verifier import install_missing_dependencies, load_apm_config, verify_dependencies

__all__ = [
    "APMDependencyResolver",
    "CircularRef",
    "ConflictInfo",
    "DependencyGraph",
    "DependencyNode",
    "DependencyTree",
    "FlatDependencyMap",
    "GitHubPackageDownloader",
    "LockFile",
    "LockedDependency",
    "PackageValidator",
    "get_lockfile_path",
    "install_missing_dependencies",
    "load_apm_config",
    "verify_dependencies",
]
