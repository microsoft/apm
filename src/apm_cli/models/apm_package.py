"""APM Package data models.

This module contains the core APMPackage and PackageInfo dataclasses.
Dependency and validation types have been extracted to sibling modules
(.dependency and .validation) but are re-exported here for backward
compatibility.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
__all__ = [
    # Defined in this module
    "APMPackage",
    # Backward-compatible re-exports from .dependency
    "DependencyReference",
    "GitReferenceType",
    # Backward-compatible re-exports from .validation
    "InvalidVirtualPackageExtensionError",
    "MCPDependency",
    "PackageContentType",
    "PackageInfo",
    "PackageType",
    "RemoteRef",
    "ResolvedReference",
    "ValidationError",
    "ValidationResult",
    "clear_apm_yml_cache",
    "parse_git_reference",
    "validate_apm_package",
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
    def _parse_apm_dependencies(cls, dep_list: list, label: str) -> list:
        """Parse APM dependency entries."""
        from .dependency.reference import DependencyReference

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
        return parsed_deps

    @classmethod
    def _parse_mcp_dependencies(cls, dep_list: list, label: str) -> list:
        """Parse MCP dependency entries."""
        from .dependency.mcp import MCPDependency

        parsed_mcp: list = []
        for dep in dep_list:
            if isinstance(dep, str):
                parsed_mcp.append(MCPDependency.from_string(dep))
            elif isinstance(dep, dict):
                try:
                    parsed_mcp.append(MCPDependency.from_dict(dep))
                except ValueError as e:
                    raise ValueError(f"Invalid {label}MCP dependency: {e}")  # noqa: B904
        return parsed_mcp

    @classmethod
    def _parse_dependency_dict(cls, raw_deps: dict, label: str = "") -> dict:
        """Parse a dependencies or devDependencies dict from apm.yml.

        Args:
            raw_deps: Raw dict mapping dep type -> list of entries.
            label: Prefix for error messages (e.g. "dev " for devDependencies).
        """
        parsed: dict = {}
        for dep_type, dep_list in raw_deps.items():
            if not isinstance(dep_list, list):
                continue
            if dep_type == "apm":
                parsed[dep_type] = cls._parse_apm_dependencies(dep_list, label)
            elif dep_type == "mcp":
                parsed[dep_type] = cls._parse_mcp_dependencies(dep_list, label)
            else:
                parsed[dep_type] = [dep for dep in dep_list if isinstance(dep, (str, dict))]
        return parsed

    @classmethod
    def _parse_includes(cls, includes_value) -> "str | list[str]":
        """Parse and validate the ``includes`` field from ``apm.yml``.

        Accepts the literal string ``"auto"`` or a list of strings.
        Raises :class:`ValueError` on any other type or value.  Extracted from
        :meth:`from_apm_yml` to reduce its McCabe complexity within the
        configured Ruff thresholds.
        """
        if isinstance(includes_value, str):
            if includes_value != "auto":
                raise ValueError("'includes' must be 'auto' or a list of strings")
            return "auto"
        if isinstance(includes_value, list):
            if not all(isinstance(item, str) for item in includes_value):
                raise ValueError("'includes' must be 'auto' or a list of strings")
            return list(includes_value)
        raise ValueError("'includes' must be 'auto' or a list of strings")

    @classmethod
    def _load_and_validate_yaml(cls, apm_yml_path: Path) -> dict:
        """Load and perform basic validation on apm.yml content."""
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml_path)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {apm_yml_path}: {e}")  # noqa: B904

        if not isinstance(data, dict):
            raise ValueError(f"apm.yml must contain a YAML object, got {type(data)}")
        if "name" not in data:
            raise ValueError("Missing required field 'name' in apm.yml")
        if "version" not in data:
            raise ValueError("Missing required field 'version' in apm.yml")
        return data

    @classmethod
    def _parse_dependencies_field(cls, data: dict, apm_yml_path: Path) -> dict | None:
        """Parse the dependencies field from apm.yml data."""
        raw_deps = data.get("dependencies")
        if raw_deps is None:
            return None
        if not isinstance(raw_deps, dict):
            raise ValueError(
                f"Invalid 'dependencies' in {apm_yml_path}: expected a mapping "
                f"with 'apm:' and/or 'mcp:' keys, got {type(raw_deps).__name__}. "
                "Use the structured format:\n"
                "  dependencies:\n"
                "    apm:\n"
                "      - owner/repo"
            )
        return cls._parse_dependency_dict(raw_deps, label="")

    @classmethod
    def _parse_dev_dependencies_field(cls, data: dict, apm_yml_path: Path) -> dict | None:
        """Parse the devDependencies field from apm.yml data."""
        raw_dev_deps = data.get("devDependencies")
        if raw_dev_deps is None:
            return None
        if not isinstance(raw_dev_deps, dict):
            raise ValueError(
                f"Invalid 'devDependencies' in {apm_yml_path}: expected a mapping "
                f"with 'apm:' and/or 'mcp:' keys, got {type(raw_dev_deps).__name__}. "
                "Use the structured format:\n"
                "  devDependencies:\n"
                "    apm:\n"
                "      - owner/repo"
            )
        return cls._parse_dependency_dict(raw_dev_deps, label="dev ")

    @classmethod
    def _parse_type_field(cls, data: dict) -> PackageContentType | None:
        """Parse the type field from apm.yml data."""
        if "type" not in data or data["type"] is None:
            return None
        type_value = data["type"]
        if not isinstance(type_value, str):
            raise ValueError(
                f"Invalid 'type' field: expected string, got {type(type_value).__name__}"
            )
        try:
            return PackageContentType.from_string(type_value)
        except ValueError as e:
            raise ValueError(f"Invalid 'type' field in apm.yml: {e}")  # noqa: B904

    @classmethod
    def _parse_targets_field(cls, data: dict) -> list[str] | None:
        """Parse the targets field from apm.yml data."""
        if "targets" not in data or data["targets"] is None:
            return None
        raw_targets = data["targets"]
        if isinstance(raw_targets, list):
            return [str(t).strip() for t in raw_targets if str(t).strip()]
        return [str(raw_targets).strip()]

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

        data = cls._load_and_validate_yaml(apm_yml_path)
        dependencies = cls._parse_dependencies_field(data, apm_yml_path)
        dev_dependencies = cls._parse_dev_dependencies_field(data, apm_yml_path)
        pkg_type = cls._parse_type_field(data)

        includes = None
        if "includes" in data and data["includes"] is not None:
            includes = cls._parse_includes(data["includes"])

        target_value = parse_target_field(
            data.get("target"),
            source_path=apm_yml_path,
        )
        targets_value = cls._parse_targets_field(data)

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


from ._package_info import PackageInfo as PackageInfo
