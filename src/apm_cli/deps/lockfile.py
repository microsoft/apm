"""Lock file support for APM dependency resolution.

Provides deterministic, reproducible installs by capturing exact resolved versions.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401, UP035

import yaml

from ..models.apm_package import DependencyReference

logger = logging.getLogger(__name__)

_SELF_KEY = "."


@dataclass
class LockedDependency:
    """A resolved dependency with exact commit/version information."""

    repo_url: str
    host: str | None = None
    port: int | None = None  # Non-standard SSH/HTTPS port (e.g. 7999 for Bitbucket DC)
    registry_prefix: str | None = None  # Registry path prefix, e.g. "artifactory/github"
    resolved_commit: str | None = None
    resolved_ref: str | None = None
    version: str | None = None
    virtual_path: str | None = None
    is_virtual: bool = False
    depth: int = 1
    resolved_by: str | None = None
    package_type: str | None = None
    deployed_files: list[str] = field(default_factory=list)
    deployed_file_hashes: dict[str, str] = field(default_factory=dict)
    source: str | None = None  # "local" for local deps, None/absent for remote
    local_path: str | None = None  # Original local path (relative to project root)
    content_hash: str | None = None  # SHA-256 of package file tree
    is_dev: bool = False  # True for devDependencies
    discovered_via: str | None = None  # Marketplace name (provenance)
    marketplace_plugin_name: str | None = None  # Plugin name in marketplace
    is_insecure: bool = False  # True when the locked source was http://
    allow_insecure: bool = False  # True when the manifest explicitly allowed HTTP
    skill_subset: list[str] = field(default_factory=list)  # Sorted skill names for SKILL_BUNDLE

    def get_unique_key(self) -> str:
        """Returns unique key for this dependency."""
        if self.source == "local" and self.local_path:
            return self.local_path
        if self.is_virtual and self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for YAML output."""
        result: dict[str, Any] = {"repo_url": self.repo_url}
        if self.host:
            result["host"] = self.host
        if self.port:
            result["port"] = self.port
        if self.registry_prefix:
            result["registry_prefix"] = self.registry_prefix
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
        if self.package_type:
            result["package_type"] = self.package_type
        if self.deployed_files:
            result["deployed_files"] = sorted(self.deployed_files)
        if self.deployed_file_hashes:
            result["deployed_file_hashes"] = dict(sorted(self.deployed_file_hashes.items()))
        if self.source:
            result["source"] = self.source
        if self.local_path:
            result["local_path"] = self.local_path
        if self.content_hash:
            result["content_hash"] = self.content_hash
        if self.is_dev:
            result["is_dev"] = True
        if self.discovered_via:
            result["discovered_via"] = self.discovered_via
        if self.marketplace_plugin_name:
            result["marketplace_plugin_name"] = self.marketplace_plugin_name
        if self.is_insecure:
            result["is_insecure"] = True
        if self.allow_insecure:
            result["allow_insecure"] = True
        if self.skill_subset:
            result["skill_subset"] = sorted(self.skill_subset)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockedDependency":
        """Deserialize from dict.

        Handles backwards compatibility:
        - Old ``deployed_skills`` lists are migrated to ``deployed_files``
          paths under ``.github/skills/`` and ``.claude/skills/``.
        """
        deployed_files = list(data.get("deployed_files", []))

        # Migrate legacy deployed_skills -> deployed_files
        old_skills = data.get("deployed_skills", [])
        if old_skills and not deployed_files:
            for skill_name in old_skills:
                deployed_files.append(f".github/skills/{skill_name}/")
                deployed_files.append(f".claude/skills/{skill_name}/")

        # Defensive cast: reject non-numeric or out-of-range ports from tampered lockfiles.
        _p_raw = data.get("port")
        port: int | None = None
        if _p_raw is not None:
            try:
                _p_int = int(_p_raw)
            except (TypeError, ValueError):
                _p_int = None
            if _p_int is not None and 1 <= _p_int <= 65535:
                port = _p_int

        return cls(
            repo_url=data["repo_url"],
            host=data.get("host"),
            port=port,
            registry_prefix=data.get("registry_prefix"),
            resolved_commit=data.get("resolved_commit"),
            resolved_ref=data.get("resolved_ref"),
            version=data.get("version"),
            virtual_path=data.get("virtual_path"),
            is_virtual=data.get("is_virtual", False),
            depth=data.get("depth", 1),
            resolved_by=data.get("resolved_by"),
            package_type=data.get("package_type"),
            deployed_files=deployed_files,
            deployed_file_hashes=dict(data.get("deployed_file_hashes") or {}),
            source=data.get("source"),
            local_path=data.get("local_path"),
            content_hash=data.get("content_hash"),
            is_dev=data.get("is_dev", False),
            discovered_via=data.get("discovered_via"),
            marketplace_plugin_name=data.get("marketplace_plugin_name"),
            is_insecure=data.get("is_insecure", False),
            allow_insecure=data.get("allow_insecure", False),
            skill_subset=list(data.get("skill_subset") or []),
        )

    @classmethod
    def from_dependency_ref(
        cls,
        dep_ref: DependencyReference,
        resolved_commit: str | None,
        depth: int,
        resolved_by: str | None,
        is_dev: bool = False,
        registry_config=None,
    ) -> "LockedDependency":
        """Create from a DependencyReference with resolution info.

        Args:
            dep_ref: The resolved dependency reference.
            resolved_commit: Exact commit SHA that was installed, or ``None``.
            depth: Dependency tree depth.
            resolved_by: Parent repo URL, or ``None`` for direct dependencies.
            is_dev: Whether this is a dev-only dependency.
            registry_config: Optional :class:`~apm_cli.deps.registry_proxy.RegistryConfig`
                used for this download.  When provided, ``host`` is set to the
                pure FQDN (e.g. ``"art.example.com"``) and ``registry_prefix``
                is set to the URL path prefix (e.g. ``"artifactory/github"``),
                ensuring correct auth routing on subsequent installs.
        """
        if registry_config is not None:
            host = registry_config.host
            registry_prefix = registry_config.prefix
        else:
            host = dep_ref.host
            registry_prefix = None
        return cls(
            repo_url=dep_ref.repo_url,
            host=host,
            port=dep_ref.port,
            registry_prefix=registry_prefix,
            resolved_commit=resolved_commit,
            resolved_ref=dep_ref.reference,
            virtual_path=dep_ref.virtual_path,
            is_virtual=dep_ref.is_virtual,
            depth=depth,
            resolved_by=resolved_by,
            source="local" if dep_ref.is_local else None,
            local_path=dep_ref.local_path if dep_ref.is_local else None,
            is_dev=is_dev,
            is_insecure=dep_ref.is_insecure,
            allow_insecure=dep_ref.allow_insecure,
            skill_subset=sorted(dep_ref.skill_subset)
            if isinstance(getattr(dep_ref, "skill_subset", None), list)
            else [],
        )

    def to_dependency_ref(self) -> DependencyReference:
        """Reconstruct a DependencyReference from this locked dependency."""
        return DependencyReference(
            repo_url=self.repo_url,
            host=self.host,
            port=self.port,
            reference=self.resolved_ref,
            virtual_path=self.virtual_path,
            is_virtual=self.is_virtual,
            artifactory_prefix=self.registry_prefix,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            is_insecure=self.is_insecure,
            allow_insecure=self.allow_insecure,
        )


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
                dep_ref=dep_ref,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=is_dev,
                registry_config=registry_config,
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
        for key, dep in self.dependencies.items():
            other_dep = other.dependencies[key]
            if dep.to_dict() != other_dep.to_dict():
                return False
        if sorted(self.mcp_servers) != sorted(other.mcp_servers):
            return False
        if self.mcp_configs != other.mcp_configs:
            return False
        if sorted(self.local_deployed_files) != sorted(other.local_deployed_files):
            return False
        # Issue #887: include hash dict in equivalence so post-install
        # hash updates persist even when the file list is unchanged.
        if dict(self.local_deployed_file_hashes) != dict(other.local_deployed_file_hashes):  # noqa: SIM103
            return False
        return True

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
