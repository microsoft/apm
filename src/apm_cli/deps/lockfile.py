"""Lock file support for APM dependency resolution.

Provides deterministic, reproducible installs by capturing exact resolved versions.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from ._locked_dependency import (  # noqa: F401 – re-exported public symbols
    LockedDependency,
    _DepResolutionInfo,
)

logger = logging.getLogger(__name__)

_SELF_KEY = "."


@dataclass
class LockFile:
    """APM lock file for reproducible dependency resolution."""

    lockfile_version: str = "1"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    apm_version: str | None = None
    dependencies: dict[str, LockedDependency] = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    mcp_configs: dict[str, dict] = field(default_factory=dict)
    local_deployed_files: list[str] = field(default_factory=list)
    local_deployed_file_hashes: dict[str, str] = field(default_factory=dict)

    def add_dependency(self, dep: LockedDependency) -> None:
        """Add a dependency to the lock file."""
        self.dependencies[dep.get_unique_key()] = dep

    def get_dependency(self, key: str) -> LockedDependency | None:
        """Get a dependency by its unique key."""
        return self.dependencies.get(key)

    def has_dependency(self, key: str) -> bool:
        """Check if a dependency exists."""
        return key in self.dependencies

    def get_all_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies sorted by depth then repo_url."""
        return sorted(self.dependencies.values(), key=lambda d: (d.depth, d.repo_url))

    def get_package_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies excluding the virtual self-entry."""
        return [d for d in self.get_all_dependencies() if d.local_path != "."]

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        # The synthesized self-entry (key ".") is an in-memory normalization
        # of the flat local_deployed_files / local_deployed_file_hashes
        # fields. It must not be written back into the dependencies list,
        # since the flat fields remain the source of truth in YAML.
        _self_dep = self.dependencies.pop(_SELF_KEY, None)
        try:
            data: dict[str, Any] = {
                "lockfile_version": self.lockfile_version,
                "generated_at": self.generated_at,
            }
            if self.apm_version:
                data["apm_version"] = self.apm_version
            data["dependencies"] = [dep.to_dict() for dep in self.get_all_dependencies()]
            if self.mcp_servers:
                data["mcp_servers"] = sorted(self.mcp_servers)
            if self.mcp_configs:
                data["mcp_configs"] = dict(sorted(self.mcp_configs.items()))
            if self.local_deployed_files:
                data["local_deployed_files"] = sorted(self.local_deployed_files)
            if self.local_deployed_file_hashes:
                data["local_deployed_file_hashes"] = dict(
                    sorted(self.local_deployed_file_hashes.items())
                )
            from ..utils.yaml_io import yaml_to_str

            return yaml_to_str(data)
        finally:
            if _self_dep is not None:
                self.dependencies[_SELF_KEY] = _self_dep

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "LockFile":
        """Deserialize from YAML string."""
        data = yaml.safe_load(yaml_str)
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        lock = cls(
            lockfile_version=data.get("lockfile_version", "1"),
            generated_at=data.get("generated_at", ""),
            apm_version=data.get("apm_version"),
        )
        for dep_data in data.get("dependencies", []):
            lock.add_dependency(LockedDependency.from_dict(dep_data))
        lock.mcp_servers = list(data.get("mcp_servers", []))
        lock.mcp_configs = dict(data.get("mcp_configs") or {})
        lock.local_deployed_files = list(data.get("local_deployed_files", []))
        lock.local_deployed_file_hashes = dict(data.get("local_deployed_file_hashes") or {})
        # Synthesize a virtual self-entry representing the project's own
        # local content. This unifies traversal across "real" dependencies
        # and the local package, without changing the on-disk YAML shape.
        if lock.local_deployed_files:
            lock.dependencies[_SELF_KEY] = LockedDependency(
                repo_url="<self>",
                source="local",
                local_path=".",
                is_dev=True,
                depth=0,
                deployed_files=list(lock.local_deployed_files),
                deployed_file_hashes=dict(lock.local_deployed_file_hashes),
            )
        return lock

    def write(self, path: Path) -> None:
        """Write lock file to disk."""
        path.write_text(self.to_yaml(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Optional["LockFile"]:
        """Read lock file from disk. Returns None if not exists or corrupt."""
        if not path.exists():
            return None
        try:
            return cls.from_yaml(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, ValueError, KeyError):
            return None

    @classmethod
    def load_or_create(cls, path: Path) -> "LockFile":
        """Load existing lock file or create a new one."""
        return cls.read(path) or cls()

    @classmethod
    def from_installed_packages(
        cls,
        installed_packages,
        dependency_graph,
    ) -> "LockFile":
        """Create a lock file from installed packages.

        Args:
            installed_packages: List of
                :class:`~apm_cli.deps.installed_package.InstalledPackage`
                objects **or** legacy tuples of the form
                ``(dep_ref, resolved_commit, depth, resolved_by[, is_dev])``.
                The 5th tuple element is optional for backward compatibility.
            dependency_graph: The resolved DependencyGraph for additional metadata.
        """
        from .installed_package import InstalledPackage

        # Get APM version
        try:
            from importlib.metadata import version

            apm_version = version("apm-cli")
        except Exception:
            apm_version = "unknown"

        lock = cls(apm_version=apm_version)

        for entry in installed_packages:
            if isinstance(entry, InstalledPackage):
                dep_ref = entry.dep_ref
                resolved_commit = entry.resolved_commit
                depth = entry.depth
                resolved_by = entry.resolved_by
                is_dev = entry.is_dev
                registry_config = getattr(entry, "registry_config", None)
            elif len(entry) >= 5:
                dep_ref, resolved_commit, depth, resolved_by, is_dev = entry[:5]
                registry_config = None
            else:
                dep_ref, resolved_commit, depth, resolved_by = entry[:4]
                is_dev = False
                registry_config = None

            locked_dep = LockedDependency.from_dependency_ref(
                dep_ref,
                _DepResolutionInfo(
                    resolved_commit=resolved_commit,
                    depth=depth,
                    resolved_by=resolved_by,
                    is_dev=is_dev,
                    registry_config=registry_config,
                ),
            )
            lock.add_dependency(locked_dep)

        return lock

    def get_installed_paths(self, apm_modules_dir: Path) -> list[str]:
        """Get relative installed paths for all dependencies in this lockfile.

        Computes expected installed paths for all dependencies, including
        transitive ones. Used by:
        - Primitive discovery to find all dependency primitives
        - Orphan detection to avoid false positives for transitive deps

        Args:
            apm_modules_dir: Path to the apm_modules directory.

        Returns:
            List[str]: POSIX-style relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        seen: set = set()
        paths: list[str] = []
        for dep in self.get_all_dependencies():
            if dep.local_path == _SELF_KEY:
                continue
            dep_ref = dep.to_dependency_ref()
            install_path = dep_ref.get_install_path(apm_modules_dir)
            try:
                rel_path = install_path.relative_to(apm_modules_dir).as_posix()
            except ValueError:
                rel_path = Path(install_path).as_posix()
            if rel_path not in seen:
                seen.add(rel_path)
                paths.append(rel_path)
        return paths

    def save(self, path: Path) -> None:
        """Save lock file to disk (alias for write)."""
        self.write(path)

    def is_semantically_equivalent(self, other: "LockFile") -> bool:
        """Return True if *other* has the same deps, MCP servers, and configs.

        Ignores ``generated_at`` and ``apm_version`` so that a no-change
        install does not dirty the lockfile.
        """
        if self.lockfile_version != other.lockfile_version:
            return False
        if set(self.dependencies.keys()) != set(other.dependencies.keys()):
            return False
        if any(
            dep.to_dict() != other.dependencies[key].to_dict()
            for key, dep in self.dependencies.items()
        ):
            return False
        # Issue #887: include hash dict in equivalence so post-install
        # hash updates persist even when the file list is unchanged.
        return (
            sorted(self.mcp_servers) == sorted(other.mcp_servers)
            and self.mcp_configs == other.mcp_configs
            and sorted(self.local_deployed_files) == sorted(other.local_deployed_files)
            and dict(self.local_deployed_file_hashes) == dict(other.local_deployed_file_hashes)
        )

    @classmethod
    def installed_paths_for_project(cls, project_root: Path) -> list[str]:
        """Load apm.lock.yaml from project_root and return installed paths.

        Returns an empty list if the lockfile is missing, corrupt, or
        unreadable.

        Args:
            project_root: Path to project root containing apm.lock.yaml.

        Returns:
            List[str]: Relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        try:
            lockfile_path = get_lockfile_path(project_root)
            if not lockfile_path.exists():
                # Fallback to legacy lockfile for pre-migration reads
                legacy_path = project_root / LEGACY_LOCKFILE_NAME
                if legacy_path.exists():
                    lockfile_path = legacy_path
            lockfile = cls.read(lockfile_path)
            if not lockfile:
                return []
            return lockfile.get_installed_paths(project_root / "apm_modules")
        except (FileNotFoundError, yaml.YAMLError, ValueError, KeyError):
            return []


# Current lockfile filename (with .yaml extension for IDE syntax highlighting)
LOCKFILE_NAME = "apm.lock.yaml"
# Legacy lockfile filename used in older APM versions
LEGACY_LOCKFILE_NAME = "apm.lock"


def get_lockfile_path(project_root: Path) -> Path:
    """Get the path to the lock file for a project."""
    return project_root / LOCKFILE_NAME


def migrate_lockfile_if_needed(project_root: Path) -> bool:
    """Migrate legacy apm.lock to apm.lock.yaml if needed.

    Renames ``apm.lock`` to ``apm.lock.yaml`` when the new file does not yet
    exist.  This is a one-time, transparent migration for users upgrading from
    older APM versions.

    Args:
        project_root: Path to the project root directory.

    Returns:
        True if a migration was performed, False otherwise.
    """
    new_path = get_lockfile_path(project_root)
    legacy_path = project_root / LEGACY_LOCKFILE_NAME
    if not new_path.exists() and legacy_path.exists():
        try:
            legacy_path.rename(new_path)
        except OSError:
            logger.debug("Could not rename %s to %s", legacy_path, new_path, exc_info=True)
            return False
        return True
    return False


def get_lockfile_installed_paths(project_root: Path) -> list[str]:
    """Deprecated: use LockFile.installed_paths_for_project() instead."""
    return LockFile.installed_paths_for_project(project_root)
