"""Lock file support for APM dependency resolution.

Provides deterministic, reproducible installs by capturing exact resolved versions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..models.apm_package import DependencyReference


@dataclass
class LockedDependency:
    """A resolved dependency with exact commit/version information."""

    repo_url: str
    host: Optional[str] = None
    resolved_commit: Optional[str] = None
    resolved_ref: Optional[str] = None
    version: Optional[str] = None
    virtual_path: Optional[str] = None
    is_virtual: bool = False
    depth: int = 1
    resolved_by: Optional[str] = None
    deployed_files: List[str] = field(default_factory=list)

    def get_unique_key(self) -> str:
        """Returns unique key for this dependency."""
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for YAML output."""
        result: Dict[str, Any] = {"repo_url": self.repo_url}
        if self.host:
            result["host"] = self.host
        if self.resolved_commit:
            result["resolved_commit"] = self.resolved_commit
        if self.resolved_ref:
            result["resolved_ref"] = self.resolved_ref
        if self.version:
            result["version"] = self.version
        if self.virtual_path:
            result["virtual_path"] = self.virtual_path
        if self.is_virtual:
            result["is_virtual"] = self.is_virtual
        if self.depth != 1:
            result["depth"] = self.depth
        if self.resolved_by:
            result["resolved_by"] = self.resolved_by
        if self.deployed_files:
            result["deployed_files"] = sorted(self.deployed_files)
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockedDependency":
        """Deserialize from dict.

        Handles backwards compatibility:
        - Old ``deployed_skills`` lists are migrated to ``deployed_files``
          paths under ``.github/skills/`` and ``.claude/skills/``.
        """
        deployed_files = list(data.get("deployed_files", []))

        # Migrate legacy deployed_skills → deployed_files
        old_skills = data.get("deployed_skills", [])
        if old_skills and not deployed_files:
            for skill_name in old_skills:
                deployed_files.append(f".github/skills/{skill_name}/")
                deployed_files.append(f".claude/skills/{skill_name}/")

        return cls(
            repo_url=data["repo_url"],
            host=data.get("host"),
            resolved_commit=data.get("resolved_commit"),
            resolved_ref=data.get("resolved_ref"),
            version=data.get("version"),
            virtual_path=data.get("virtual_path"),
            is_virtual=data.get("is_virtual", False),
            depth=data.get("depth", 1),
            resolved_by=data.get("resolved_by"),
            deployed_files=deployed_files,
        )

    @classmethod
    def from_dependency_ref(
        cls,
        dep_ref: DependencyReference,
        resolved_commit: Optional[str],
        depth: int,
        resolved_by: Optional[str],
    ) -> "LockedDependency":
        """Create from a DependencyReference with resolution info."""
        return cls(
            repo_url=dep_ref.repo_url,
            host=dep_ref.host,
            resolved_commit=resolved_commit,
            resolved_ref=dep_ref.reference,
            virtual_path=dep_ref.virtual_path,
            is_virtual=dep_ref.is_virtual,
            depth=depth,
            resolved_by=resolved_by,
        )


@dataclass
class LockFile:
    """APM lock file for reproducible dependency resolution."""

    lockfile_version: str = "1"
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    apm_version: Optional[str] = None
    dependencies: Dict[str, LockedDependency] = field(default_factory=dict)
    mcp_servers: List[str] = field(default_factory=list)

    def add_dependency(self, dep: LockedDependency) -> None:
        """Add a dependency to the lock file."""
        self.dependencies[dep.get_unique_key()] = dep

    def get_dependency(self, key: str) -> Optional[LockedDependency]:
        """Get a dependency by its unique key."""
        return self.dependencies.get(key)

    def has_dependency(self, key: str) -> bool:
        """Check if a dependency exists."""
        return key in self.dependencies

    def get_all_dependencies(self) -> List[LockedDependency]:
        """Get all dependencies sorted by depth then repo_url."""
        return sorted(
            self.dependencies.values(), key=lambda d: (d.depth, d.repo_url)
        )

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        data: Dict[str, Any] = {
            "lockfile_version": self.lockfile_version,
            "generated_at": self.generated_at,
        }
        if self.apm_version:
            data["apm_version"] = self.apm_version
        data["dependencies"] = [dep.to_dict() for dep in self.get_all_dependencies()]
        if self.mcp_servers:
            data["mcp_servers"] = sorted(self.mcp_servers)
        return yaml.dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True
        )

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
        installed_packages: List[tuple],
        dependency_graph,
    ) -> "LockFile":
        """Create a lock file from installed packages.
        
        Args:
            installed_packages: List of (dep_ref, resolved_commit, depth, resolved_by) tuples
            dependency_graph: The resolved DependencyGraph for additional metadata
        """
        # Get APM version
        try:
            from importlib.metadata import version
            apm_version = version("apm-cli")
        except Exception:
            apm_version = "unknown"
        
        lock = cls(apm_version=apm_version)
        
        for dep_ref, resolved_commit, depth, resolved_by in installed_packages:
            locked_dep = LockedDependency.from_dependency_ref(
                dep_ref=dep_ref,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
            )
            lock.add_dependency(locked_dep)
        
        return lock

    def get_installed_paths(self, apm_modules_dir: Path) -> List[str]:
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
        paths: List[str] = []
        for dep in self.get_all_dependencies():
            dep_ref = DependencyReference(
                repo_url=dep.repo_url,
                host=dep.host,
                virtual_path=dep.virtual_path,
                is_virtual=dep.is_virtual,
            )
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

    @classmethod
    def installed_paths_for_project(cls, project_root: Path) -> List[str]:
        """Load apm.lock from project_root and return installed paths.

        Returns an empty list if the lockfile is missing, corrupt, or
        unreadable.

        Args:
            project_root: Path to project root containing apm.lock.

        Returns:
            List[str]: Relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        try:
            lockfile = cls.read(project_root / "apm.lock")
            if not lockfile:
                return []
            return lockfile.get_installed_paths(project_root / "apm_modules")
        except (FileNotFoundError, yaml.YAMLError, ValueError, KeyError):
            return []


def get_lockfile_path(project_root: Path) -> Path:
    """Get the path to the lock file for a project."""
    return project_root / "apm.lock"


def get_lockfile_installed_paths(project_root: Path) -> List[str]:
    """Deprecated: use LockFile.installed_paths_for_project() instead."""
    return LockFile.installed_paths_for_project(project_root)
