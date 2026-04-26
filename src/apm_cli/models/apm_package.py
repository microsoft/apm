"""APM Package data models.

This module contains the core APMPackage and PackageInfo dataclasses.
Dependency and validation types have been extracted to sibling modules
(.dependency and .validation) but are re-exported here for backward
compatibility.
"""

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Union

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

# Module-level parse cache: resolved path -> APMPackage (#171)
_apm_yml_cache: Dict[Path, "APMPackage"] = {}


def clear_apm_yml_cache() -> None:
    """Clear the from_apm_yml parse cache. Call in tests for isolation."""
    _apm_yml_cache.clear()


def _parse_registries_block(data: dict, apm_yml_path: Path):
    """Parse the top-level ``registries:`` block per design §3.1.

    Schema::

        registries:
          corp-main:
            url: https://registry.corp.example.com/apm
          corp-other:
            url: https://other.example.com/apm
          default: corp-main           # optional; name of one of the entries

    Returns ``(registries_map, default_name)`` where:
    - ``registries_map`` is ``{name: url}`` excluding the special ``default`` key.
    - ``default_name`` is either the ``default`` value (when it's a registered
      name) OR ``None``.

    Absent block returns ``(None, None)`` — the default-routing pass is a
    no-op and a project sees zero behavior change (invariant §2.1.1).
    """
    raw = data.get('registries')
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(
            f"Top-level 'registries:' block in {apm_yml_path} must be a "
            f"mapping (name -> {{url: ...}})"
        )

    default_value = raw.get('default')
    registries_map: Dict[str, str] = {}
    for name, body in raw.items():
        if name == 'default':
            continue
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Registry name in 'registries:' block must be a non-empty "
                f"string (got {name!r})"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"Registry {name!r} must be a mapping with at least 'url:' "
                f"(got {type(body).__name__})"
            )
        url = body.get('url')
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"Registry {name!r} is missing required field 'url:'"
            )
        url = url.strip()
        if not url.startswith(('https://', 'http://')):
            raise ValueError(
                f"Registry {name!r} URL must start with https:// or http:// "
                f"(got {url!r})"
            )
        # Reject any unknown keys to catch typos early.
        unknown = set(body.keys()) - {'url'}
        if unknown:
            raise ValueError(
                f"Registry {name!r} has unknown fields: {sorted(unknown)} "
                f"(known fields: ['url'])"
            )
        registries_map[name] = url

    default_name: Optional[str] = None
    if default_value is not None:
        if not isinstance(default_value, str) or not default_value.strip():
            raise ValueError(
                f"'registries.default' in {apm_yml_path} must be a non-empty "
                f"string naming one of the configured registries"
            )
        default_name = default_value.strip()
        if default_name not in registries_map:
            raise ValueError(
                f"'registries.default: {default_name}' refers to an "
                f"unconfigured registry. Configured: "
                f"{sorted(registries_map.keys())}"
            )

    if not registries_map and default_name is None:
        # An empty ``registries: {}`` block is harmless but suspicious.
        return None, None

    return registries_map, default_name


def _route_unscoped_to_default_registry(
    apm_dep_list: list,
    default_name: str,
    apm_yml_path: Path,
) -> None:
    """Flip the resolver of every still-unrouted shorthand APM dep to the default registry.

    Per design §3.2: when ``registries.default`` is set, every shorthand
    string entry that doesn't already specify a resolver routes through the
    default registry. Object forms (``- git:`` / ``- path:`` / ``- registry:``)
    and the ``@<name>`` shorthand have already set ``source`` explicitly and
    are left alone here.

    Modifies the list in place. Raises ``ValueError`` with a clear remediation
    if any candidate has a non-semver ref or no version constraint at all.
    """
    from ..deps.registry.semver import is_semver_range
    from .dependency.reference import DependencyReference

    for dep in apm_dep_list:
        if not isinstance(dep, DependencyReference):
            continue
        # Already routed (registry / local / explicit git via @<name> or
        # object form). The only thing we change is "source unset, looks like
        # plain shorthand" candidates.
        if dep.source is not None:
            continue
        if dep.is_local or dep.is_virtual:
            # Virtual packages MUST use object form to route through a
            # registry; shorthand virtuals stay on Git.
            continue
        if dep.reference is None or not dep.reference.strip():
            raise ValueError(
                f"{apm_yml_path}: dep {dep.repo_url!r} has no version "
                f"constraint, but 'registries.default' is set. A registry-"
                f"routed entry must specify a #<semver>. Either add a version "
                f"(e.g. '{dep.repo_url}#^1.0') or pin to Git with the "
                f"'- git:' object form."
            )
        if not is_semver_range(dep.reference):
            raise ValueError(
                f"{apm_yml_path}: dep '{dep.repo_url}#{dep.reference}' "
                f"routes through the default registry {default_name!r} but "
                f"'{dep.reference}' is not a semver version or range. Use "
                f"an explicit '- git:' entry for branch or commit-SHA "
                f"pinning, or change the ref to a semver version/range "
                f"(e.g. ^1.0, ~1.2.3, 1.x)."
            )
        dep.source = "registry"
        dep.registry_name = default_name


@dataclass
class APMPackage:
    """Represents an APM package with metadata."""
    name: str
    version: str
    description: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    source: Optional[str] = None  # Source location (for dependencies)
    resolved_commit: Optional[str] = None  # Resolved commit SHA (for dependencies)
    dependencies: Optional[Dict[str, List[Union[DependencyReference, str, dict]]]] = None  # Mixed types for APM/MCP/inline
    dev_dependencies: Optional[Dict[str, List[Union[DependencyReference, str, dict]]]] = None
    scripts: Optional[Dict[str, str]] = None
    package_path: Optional[Path] = None  # Local path to package
    target: Optional[Union[str, List[str]]] = None  # Target agent(s): single string or list (applies to compile and install)
    type: Optional[PackageContentType] = None  # Package content type: instructions, skill, hybrid, or prompts
    includes: Optional[Union[str, List[str]]] = None  # Include-only manifest: 'auto' or list of repo paths

    # Top-level ``registries:`` block per docs/proposals/registry-api.md §3.1.
    # ``registries`` maps name -> base URL; ``default_registry`` is the name to
    # which string-shorthand entries route when they don't specify a scope.
    # Both default to None — a project without a ``registries:`` block sees
    # zero behavior change (invariant §2.1.1).
    registries: Optional[Dict[str, str]] = None
    default_registry: Optional[str] = None

    @classmethod
    def from_apm_yml(cls, apm_yml_path: Path) -> "APMPackage":
        """Load APM package from apm.yml file.
        
        Results are cached by resolved path for the lifetime of the process.
        
        Args:
            apm_yml_path: Path to the apm.yml file
            
        Returns:
            APMPackage: Loaded package instance
            
        Raises:
            ValueError: If the file is invalid or missing required fields
            FileNotFoundError: If the file doesn't exist
        """
        if not apm_yml_path.exists():
            raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")
        
        resolved = apm_yml_path.resolve()
        cached = _apm_yml_cache.get(resolved)
        if cached is not None:
            return cached
        
        try:
            from ..utils.yaml_io import load_yaml
            data = load_yaml(apm_yml_path)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {apm_yml_path}: {e}")
        
        if not isinstance(data, dict):
            raise ValueError(f"apm.yml must contain a YAML object, got {type(data)}")
        
        # Required fields
        if 'name' not in data:
            raise ValueError("Missing required field 'name' in apm.yml")
        if 'version' not in data:
            raise ValueError("Missing required field 'version' in apm.yml")

        # Top-level ``registries:`` block per design §3.1.
        registries, default_registry = _parse_registries_block(data, apm_yml_path)

        # Parse dependencies
        dependencies = None
        if 'dependencies' in data and isinstance(data['dependencies'], dict):
            dependencies = {}
            for dep_type, dep_list in data['dependencies'].items():
                if isinstance(dep_list, list):
                    if dep_type == 'apm':
                        # APM dependencies need to be parsed as DependencyReference objects
                        parsed_deps = []
                        for dep_entry in dep_list:
                            if isinstance(dep_entry, str):
                                try:
                                    parsed_deps.append(DependencyReference.parse(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid APM dependency '{dep_entry}': {e}")
                            elif isinstance(dep_entry, dict):
                                try:
                                    parsed_deps.append(DependencyReference.parse_from_dict(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid APM dependency {dep_entry}: {e}")
                        dependencies[dep_type] = parsed_deps
                    elif dep_type == 'mcp':
                        parsed_mcp = []
                        for dep in dep_list:
                            if isinstance(dep, str):
                                parsed_mcp.append(MCPDependency.from_string(dep))
                            elif isinstance(dep, dict):
                                try:
                                    parsed_mcp.append(MCPDependency.from_dict(dep))
                                except ValueError as e:
                                    raise ValueError(f"Invalid MCP dependency: {e}")
                        dependencies[dep_type] = parsed_mcp
                    else:
                        # Other dependency types: keep as-is
                        dependencies[dep_type] = [dep for dep in dep_list if isinstance(dep, (str, dict))]
        
        # Parse devDependencies (same structure as dependencies)
        dev_dependencies = None
        if 'devDependencies' in data and isinstance(data['devDependencies'], dict):
            dev_dependencies = {}
            for dep_type, dep_list in data['devDependencies'].items():
                if isinstance(dep_list, list):
                    if dep_type == 'apm':
                        parsed_deps = []
                        for dep_entry in dep_list:
                            if isinstance(dep_entry, str):
                                try:
                                    parsed_deps.append(DependencyReference.parse(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid dev APM dependency '{dep_entry}': {e}")
                            elif isinstance(dep_entry, dict):
                                try:
                                    parsed_deps.append(DependencyReference.parse_from_dict(dep_entry))
                                except ValueError as e:
                                    raise ValueError(f"Invalid dev APM dependency {dep_entry}: {e}")
                        dev_dependencies[dep_type] = parsed_deps
                    elif dep_type == 'mcp':
                        parsed_mcp = []
                        for dep in dep_list:
                            if isinstance(dep, str):
                                parsed_mcp.append(MCPDependency.from_string(dep))
                            elif isinstance(dep, dict):
                                try:
                                    parsed_mcp.append(MCPDependency.from_dict(dep))
                                except ValueError as e:
                                    raise ValueError(f"Invalid dev MCP dependency: {e}")
                        dev_dependencies[dep_type] = parsed_mcp
                    else:
                        dev_dependencies[dep_type] = [dep for dep in dep_list if isinstance(dep, (str, dict))]

        # Parse package content type
        pkg_type = None
        if 'type' in data and data['type'] is not None:
            type_value = data['type']
            if not isinstance(type_value, str):
                raise ValueError(f"Invalid 'type' field: expected string, got {type(type_value).__name__}")
            try:
                pkg_type = PackageContentType.from_string(type_value)
            except ValueError as e:
                raise ValueError(f"Invalid 'type' field in apm.yml: {e}")
        
        # Parse includes (auto-publish opt-in): either the literal "auto" or a list of repo paths
        includes = None
        if 'includes' in data and data['includes'] is not None:
            includes_value = data['includes']
            if isinstance(includes_value, str):
                if includes_value != 'auto':
                    raise ValueError("'includes' must be 'auto' or a list of strings")
                includes = 'auto'
            elif isinstance(includes_value, list):
                if not all(isinstance(item, str) for item in includes_value):
                    raise ValueError("'includes' must be 'auto' or a list of strings")
                includes = list(includes_value)
            else:
                raise ValueError("'includes' must be 'auto' or a list of strings")

        # Default-registry routing (design §3.2): once the registries: block
        # has been parsed, walk every still-unrouted shorthand APM dep and,
        # if a default registry is set, flip its source to "registry". Object
        # forms (``- git:`` / ``- path:`` / ``- registry:``) and the
        # ``@<name>`` shorthand have already set source explicitly and are
        # left alone here.
        if default_registry is not None:
            for bucket in (dependencies, dev_dependencies):
                if not bucket:
                    continue
                apm_list = bucket.get('apm') if isinstance(bucket, dict) else None
                if isinstance(apm_list, list):
                    _route_unscoped_to_default_registry(
                        apm_list, default_registry, apm_yml_path
                    )

        result = cls(
            name=data['name'],
            version=data['version'],
            description=data.get('description'),
            author=data.get('author'),
            license=data.get('license'),
            dependencies=dependencies,
            dev_dependencies=dev_dependencies,
            scripts=data.get('scripts'),
            package_path=apm_yml_path.parent,
            target=data.get('target'),
            type=pkg_type,
            includes=includes,
            registries=registries,
            default_registry=default_registry,
        )
        _apm_yml_cache[resolved] = result
        return result
    
    def get_apm_dependencies(self) -> List[DependencyReference]:
        """Get list of APM dependencies."""
        if not self.dependencies or 'apm' not in self.dependencies:
            return []
        # Filter to only return DependencyReference objects
        return [dep for dep in self.dependencies['apm'] if isinstance(dep, DependencyReference)]
    
    def get_mcp_dependencies(self) -> List["MCPDependency"]:
        """Get list of MCP dependencies."""
        if not self.dependencies or 'mcp' not in self.dependencies:
            return []
        return [dep for dep in (self.dependencies.get('mcp') or [])
                if isinstance(dep, MCPDependency)]
    
    def has_apm_dependencies(self) -> bool:
        """Check if this package has APM dependencies."""
        return bool(self.get_apm_dependencies())

    def get_dev_apm_dependencies(self) -> List[DependencyReference]:
        """Get list of dev APM dependencies."""
        if not self.dev_dependencies or 'apm' not in self.dev_dependencies:
            return []
        return [dep for dep in self.dev_dependencies['apm'] if isinstance(dep, DependencyReference)]

    def get_dev_mcp_dependencies(self) -> List["MCPDependency"]:
        """Get list of dev MCP dependencies."""
        if not self.dev_dependencies or 'mcp' not in self.dev_dependencies:
            return []
        return [dep for dep in (self.dev_dependencies.get('mcp') or [])
                if isinstance(dep, MCPDependency)]


@dataclass
class PackageInfo:
    """Information about a downloaded/installed package."""
    package: APMPackage
    install_path: Path
    resolved_reference: Optional[ResolvedReference] = None
    installed_at: Optional[str] = None  # ISO timestamp
    dependency_ref: Optional["DependencyReference"] = None  # Original dependency reference for canonical string
    package_type: Optional[PackageType] = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID
    
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
            for primitive_type in ['instructions', 'chatmodes', 'contexts', 'prompts', 'hooks']:
                primitive_dir = apm_dir / primitive_type
                if primitive_dir.exists() and any(primitive_dir.iterdir()):
                    return True
        
        # Also check hooks/ at package root (Claude-native convention)
        hooks_dir = self.install_path / "hooks"
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            return True
        
        return False