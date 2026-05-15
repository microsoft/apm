"""APM Package data models.

This module contains the core APMPackage and PackageInfo dataclasses.
Dependency and validation types have been extracted to sibling modules
(.dependency and .validation) but are re-exported here for backward
compatibility.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union  # noqa: F401, UP035

import yaml

from ..core.target_detection import parse_target_field
from .dependency import (
    DependencyReference,
    GitReferenceType,
    MCPDependency,
    RemoteRef,
    ResolvedReference,
    parse_git_reference,
)
from .validation import (
    InvalidVirtualPackageExtensionError,
    PackageContentType,
    PackageType,
    ValidationError,
    ValidationResult,
    validate_apm_package,
)

# Re-export all moved symbols so `from apm_cli.models.apm_package import X` keeps working
__all__ = [  # noqa: RUF022
    # Backward-compatible re-exports from .dependency
    "DependencyReference",
    "GitReferenceType",
    "MCPDependency",
    "RemoteRef",
    "ResolvedReference",
    "parse_git_reference",
    # Backward-compatible re-exports from .validation
    "InvalidVirtualPackageExtensionError",
    "PackageContentType",
    "PackageType",
    "ValidationError",
    "ValidationResult",
    "validate_apm_package",
    # Defined in this module
    "APMPackage",
    "PackageInfo",
    "clear_apm_yml_cache",
]

# Module-level parse cache: (resolved apm.yml path, resolved source dir) ->
# APMPackage. The source-dir half of the key is part of cache identity (#940)
# because two logical loads of the same apm.yml file can declare different
# anchors for relative ``local_path`` deps depending on which parent package
# declared them. Sharing one APMPackage instance across both would let the
# resolver mutate ``source_path`` and poison the cache for the other consumer.
_apm_yml_cache: dict[tuple[Path, Path | None], "APMPackage"] = {}


def clear_apm_yml_cache() -> None:
    """Clear the from_apm_yml parse cache. Call in tests for isolation."""
    _apm_yml_cache.clear()


@dataclass
class APMPackage:
    """Represents an APM package with metadata."""

    name: str
    version: str
    description: str | None = None
    author: str | None = None
    license: str | None = None
    source: str | None = None  # Source location (for dependencies)
    resolved_commit: str | None = None  # Resolved commit SHA (for dependencies)
    dependencies: dict[str, list[DependencyReference | str | dict]] | None = (
        None  # Mixed types for APM/MCP/inline
    )
    dev_dependencies: dict[str, list[DependencyReference | str | dict]] | None = None
    scripts: dict[str, str] | None = None
    package_path: Path | None = None  # Local path to package
    # Absolute on-disk directory used to anchor relative ``local_path``
    # dependencies declared in this package's apm.yml (#857). For LOCAL deps
    # this is the *original* user source directory, not the apm_modules copy
    # -- so a transitive ``../sibling`` declared inside the original means
    # what a developer reading the file expects. For REMOTE deps it is the
    # clone location under apm_modules. For the root project it is the
    # project root.
    source_path: Path | None = None
    target: str | list[str] | None = (
        None  # Singular 'target:' field (legacy/CSV form). May coexist with `targets`
        # being None in apm.yml, but never both populated -- ConflictingTargetsError
        # is raised at install time. Read by callers that only need a single value.
    )
    targets: list[str] | None = (
        None  # Plural 'targets:' field (canonical YAML-list form, #1335). Stored raw
        # so the install gate (mcp_integrator._gate_project_scoped_runtimes) can
        # re-validate via parse_targets_field with the same dict shape it sees from
        # raw apm.yml. None means the user did not declare 'targets:' at all.
    )
    type: PackageContentType | None = (
        None  # Package content type: instructions, skill, hybrid, or prompts
    )
    includes: str | list[str] | None = None  # Include-only manifest: 'auto' or list of repo paths

    @classmethod
    def _parse_dependency_dict(cls, raw_deps: dict, label: str = "") -> dict:
        """Parse a dependencies or devDependencies dict from apm.yml.

        Args:
            raw_deps: Raw dict mapping dep type -> list of entries.
            label: Prefix for error messages (e.g. "dev " for devDependencies).
        """
        from .dependency.mcp import MCPDependency
        from .dependency.reference import DependencyReference

        parsed: dict = {}
        for dep_type, dep_list in raw_deps.items():
            if not isinstance(dep_list, list):
                continue
            if dep_type == "apm":
                parsed_deps: list = []
                for dep_entry in dep_list:
                    if isinstance(dep_entry, str):
                        try:
                            parsed_deps.append(DependencyReference.parse(dep_entry))
                        except ValueError as e:
                            raise ValueError(f"Invalid {label}APM dependency '{dep_entry}': {e}")  # noqa: B904
                    elif isinstance(dep_entry, dict):
                        try:
                            parsed_deps.append(DependencyReference.parse_from_dict(dep_entry))
                        except ValueError as e:
                            raise ValueError(f"Invalid {label}APM dependency {dep_entry}: {e}")  # noqa: B904
                parsed[dep_type] = parsed_deps
            elif dep_type == "mcp":
                parsed_mcp: list = []
                for dep in dep_list:
                    if isinstance(dep, str):
                        parsed_mcp.append(MCPDependency.from_string(dep))
                    elif isinstance(dep, dict):
                        try:
                            parsed_mcp.append(MCPDependency.from_dict(dep))
                        except ValueError as e:
                            raise ValueError(f"Invalid {label}MCP dependency: {e}")  # noqa: B904
                parsed[dep_type] = parsed_mcp
            else:
                parsed[dep_type] = [dep for dep in dep_list if isinstance(dep, (str, dict))]
        return parsed

    @classmethod
    def from_apm_yml(
        cls,
        apm_yml_path: Path,
        source_path: Path | None = None,
    ) -> "APMPackage":
        """Load APM package from apm.yml file.

        Results are cached by ``(resolved apm.yml path, resolved source_path)``
        for the lifetime of the process. ``source_path`` is part of the cache
        identity so two logical loads of the same file with different anchors
        for relative ``local_path`` deps each get their own immutable
        APMPackage instance (#940 -- prevents cache poisoning).

        Args:
            apm_yml_path: Path to the apm.yml file.
            source_path: Optional absolute directory used to anchor relative
                ``local_path`` dependencies declared in this apm.yml. The
                resolver passes the *original* user source directory for
                local deps (not the apm_modules copy) so transitive
                ``../sibling`` references resolve as a developer reading the
                file expects. Callers that don't care about this anchoring
                may omit the argument and get the legacy behavior.

        Returns:
            APMPackage: Loaded package instance with ``source_path`` set.

        Raises:
            ValueError: If the file is invalid or missing required fields
            FileNotFoundError: If the file doesn't exist
        """
        if not apm_yml_path.exists():
            raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")

        resolved = apm_yml_path.resolve()
        resolved_source = source_path.resolve() if source_path is not None else None
        cache_key = (resolved, resolved_source)
        cached = _apm_yml_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml_path)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {apm_yml_path}: {e}")  # noqa: B904

        if not isinstance(data, dict):
            raise ValueError(f"apm.yml must contain a YAML object, got {type(data)}")

        # Required fields
        if "name" not in data:
            raise ValueError("Missing required field 'name' in apm.yml")
        if "version" not in data:
            raise ValueError("Missing required field 'version' in apm.yml")

        # Parse dependencies
        dependencies = None
        raw_deps = data.get("dependencies")
        if raw_deps is not None:
            if not isinstance(raw_deps, dict):
                raise ValueError(
                    f"Invalid 'dependencies' in {apm_yml_path}: expected a mapping "
                    f"with 'apm:' and/or 'mcp:' keys, got {type(raw_deps).__name__}. "
                    "Use the structured format:\n"
                    "  dependencies:\n"
                    "    apm:\n"
                    "      - owner/repo"
                )
            dependencies = cls._parse_dependency_dict(raw_deps, label="")

        # Parse devDependencies (same structure as dependencies)
        dev_dependencies = None
        raw_dev_deps = data.get("devDependencies")
        if raw_dev_deps is not None:
            if not isinstance(raw_dev_deps, dict):
                raise ValueError(
                    f"Invalid 'devDependencies' in {apm_yml_path}: expected a mapping "
                    f"with 'apm:' and/or 'mcp:' keys, got {type(raw_dev_deps).__name__}. "
                    "Use the structured format:\n"
                    "  devDependencies:\n"
                    "    apm:\n"
                    "      - owner/repo"
                )
            dev_dependencies = cls._parse_dependency_dict(raw_dev_deps, label="dev ")

        # Parse package content type
        pkg_type = None
        if "type" in data and data["type"] is not None:
            type_value = data["type"]
            if not isinstance(type_value, str):
                raise ValueError(
                    f"Invalid 'type' field: expected string, got {type(type_value).__name__}"
                )
            try:
                pkg_type = PackageContentType.from_string(type_value)
            except ValueError as e:
                raise ValueError(f"Invalid 'type' field in apm.yml: {e}")  # noqa: B904

        # Parse includes (auto-publish opt-in): either the literal "auto" or a list of repo paths
        includes = None
        if "includes" in data and data["includes"] is not None:
            includes_value = data["includes"]
            if isinstance(includes_value, str):
                if includes_value != "auto":
                    raise ValueError("'includes' must be 'auto' or a list of strings")
                includes = "auto"
            elif isinstance(includes_value, list):
                if not all(isinstance(item, str) for item in includes_value):
                    raise ValueError("'includes' must be 'auto' or a list of strings")
                includes = list(includes_value)
            else:
                raise ValueError("'includes' must be 'auto' or a list of strings")

        # Parse target field through the same validator as --target so a CSV
        # string like ``target: "claude,copilot"`` resolves identically to
        # ``--target claude,copilot`` and unknown tokens fail at parse time
        # (see apm_cli.core.target_detection.parse_target_field).
        target_value = parse_target_field(
            data.get("target"),
            source_path=apm_yml_path,
        )

        # Plural 'targets:' field is stored raw (no canonical validation here)
        # so the MCP install gate at mcp_integrator._gate_project_scoped_runtimes
        # can re-run parse_targets_field on a dict that mirrors apm.yml shape
        # and surface the same conflict / empty-list errors uniformly. Without
        # this passthrough, the call site at commands/install.py would silently
        # bypass the targets whitelist for any user on the modern plural form
        # (#1335 regression caught in PR #1336 audit).
        targets_value: list[str] | None = None
        if "targets" in data and data["targets"] is not None:
            raw_targets = data["targets"]
            if isinstance(raw_targets, list):
                targets_value = [str(t).strip() for t in raw_targets if str(t).strip()]
            else:
                targets_value = [str(raw_targets).strip()]

        result = cls(
            name=data["name"],
            version=data["version"],
            description=data.get("description"),
            author=data.get("author"),
            license=data.get("license"),
            dependencies=dependencies,
            dev_dependencies=dev_dependencies,
            scripts=data.get("scripts"),
            package_path=apm_yml_path.parent,
            source_path=resolved_source,
            target=target_value,
            targets=targets_value,
            type=pkg_type,
            includes=includes,
        )
        _apm_yml_cache[cache_key] = result
        return result

    def get_apm_dependencies(self) -> list[DependencyReference]:
        """Get list of APM dependencies."""
        if not self.dependencies or "apm" not in self.dependencies:
            return []
        # Filter to only return DependencyReference objects
        return [dep for dep in self.dependencies["apm"] if isinstance(dep, DependencyReference)]

    def get_mcp_dependencies(self) -> list["MCPDependency"]:
        """Get list of MCP dependencies."""
        if not self.dependencies or "mcp" not in self.dependencies:
            return []
        return [
            dep for dep in (self.dependencies.get("mcp") or []) if isinstance(dep, MCPDependency)
        ]

    def has_apm_dependencies(self) -> bool:
        """Check if this package has APM dependencies."""
        return bool(self.get_apm_dependencies())

    def get_dev_apm_dependencies(self) -> list[DependencyReference]:
        """Get list of dev APM dependencies."""
        if not self.dev_dependencies or "apm" not in self.dev_dependencies:
            return []
        return [dep for dep in self.dev_dependencies["apm"] if isinstance(dep, DependencyReference)]

    def get_dev_mcp_dependencies(self) -> list["MCPDependency"]:
        """Get list of dev MCP dependencies."""
        if not self.dev_dependencies or "mcp" not in self.dev_dependencies:
            return []
        return [
            dep
            for dep in (self.dev_dependencies.get("mcp") or [])
            if isinstance(dep, MCPDependency)
        ]


@dataclass
class PackageInfo:
    """Information about a downloaded/installed package."""

    package: APMPackage
    install_path: Path
    resolved_reference: ResolvedReference | None = None
    installed_at: str | None = None  # ISO timestamp
    dependency_ref: Optional["DependencyReference"] = (
        None  # Original dependency reference for canonical string
    )
    package_type: PackageType | None = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID

    def get_canonical_dependency_string(self) -> str:
        """Get the canonical dependency string for this package.

        Used for orphan detection - this is the unique identifier as stored in apm.yml.
        For virtual packages, includes the full path (e.g., owner/repo/collections/name).
        For regular packages, just the repo URL (e.g., owner/repo).

        Returns:
            str: Canonical dependency string, or package source/name as fallback
        """
        if self.dependency_ref:
            return self.dependency_ref.get_canonical_dependency_string()
        # Fallback to package source or name
        return self.package.source or self.package.name or "unknown"

    def get_primitives_path(self) -> Path:
        """Get path to the .apm directory for this package."""
        return self.install_path / ".apm"

    def has_primitives(self) -> bool:
        """Check if the package has any primitives."""
        apm_dir = self.get_primitives_path()
        if apm_dir.exists():
            # Check for any primitive files in .apm/ subdirectories
            for primitive_type in ["instructions", "chatmodes", "contexts", "prompts", "hooks"]:
                primitive_dir = apm_dir / primitive_type
                if primitive_dir.exists() and any(primitive_dir.iterdir()):
                    return True

        # Also check hooks/ at package root (Claude-native convention)
        hooks_dir = self.install_path / "hooks"
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):  # noqa: SIM103
            return True

        return False
