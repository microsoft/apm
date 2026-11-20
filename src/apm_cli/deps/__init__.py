"""Dependencies management package for APM.

Public names resolve lazily (PEP 562) so importing a light submodule such as
``apm_cli.deps.lockfile`` does not import the downloader stack and GitPython.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_EXPORTS: dict[str, str] = {
    "APMDependencyResolver": ".apm_resolver",
    "CircularRef": ".dependency_graph",
    "ConflictInfo": ".dependency_graph",
    "DependencyGraph": ".dependency_graph",
    "DependencyNode": ".dependency_graph",
    "DependencyTree": ".dependency_graph",
    "FlatDependencyMap": ".dependency_graph",
    "GitHubPackageDownloader": ".github_downloader",
    "LockFile": ".lockfile",
    "LockedDependency": ".lockfile",
    "PackageValidator": ".package_validator",
    "get_lockfile_path": ".lockfile",
    "install_missing_dependencies": ".verifier",
    "load_apm_config": ".verifier",
    "verify_dependencies": ".verifier",
}

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

if TYPE_CHECKING:
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


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
