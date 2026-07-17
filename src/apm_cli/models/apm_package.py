"""APM Package data models.

This module contains the core APMPackage and PackageInfo dataclasses.
Dependency and validation types have been extracted to sibling modules
(.dependency and .validation) but are re-exported here for backward
compatibility.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..core.apm_yml import parse_targets_field
from ..core.target_catalog import normalize_target_name
from ..core.target_detection import parse_target_field
from .dependency import (
    DependencyReference,
    GitReferenceType,
    LSPDependency,
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

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile

_log = logging.getLogger(__name__)

# Re-export all moved symbols so `from apm_cli.models.apm_package import X` keeps working
__all__ = [  # noqa: RUF022
    # Backward-compatible re-exports from .dependency
    "DependencyReference",
    "GitReferenceType",
    "LSPDependency",
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
    "build_installed_package_info",
    "clear_apm_yml_cache",
    "surviving_dependency_refs_for_reintegration",
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


def _parse_v01_registries_block(
    data: dict,
    apm_yml_path: Path,
) -> tuple[dict[str, str] | None, str | None]:
    """Parse the normative OpenAPM v0.1 registry map."""
    raw = data.get("registries")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(f"OpenAPM v0.1 'registries:' in {apm_yml_path} must be a mapping")
    registries: dict[str, str] = {}
    default_name = raw.get("default")
    for name, value in raw.items():
        if name == "default":
            continue
        body = {"url": value} if isinstance(value, str) else value
        if not isinstance(name, str) or not isinstance(body, dict):
            raise ValueError("OpenAPM v0.1 registry entries must be named strings or objects")
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError(f"OpenAPM v0.1 registry {name!r} requires an HTTP(S) url")
        registries[name] = url
        aliases = body.get("aliases", [])
        if aliases is not None and not isinstance(aliases, list):
            raise ValueError(f"OpenAPM v0.1 registry {name!r} aliases must be a list")
        for alias in aliases or []:
            if not isinstance(alias, str) or not alias:
                raise ValueError(f"OpenAPM v0.1 registry {name!r} has an invalid alias")
            registries[alias] = url
    if default_name is not None and (
        not isinstance(default_name, str) or default_name not in registries
    ):
        raise ValueError("OpenAPM v0.1 registries.default must name a declared registry")
    return registries or None, default_name


def _parse_registries_block(data: dict, apm_yml_path: Path, manifest_contract):
    """Parse the top-level ``registries:`` block per design §3.1.

    Schema::

        registries:
          corp-main:
            url: https://registry.corp.example.com/apm
          corp-other:
            url: https://other.example.com/apm
          default: corp-main           # optional; routes unscoped deps here

    Returns ``(registries_map, default_name)`` where *registries_map* is
    ``{name: url}`` and *default_name* is the value of ``default:`` (or
    ``None``). Absent block returns ``(None, None)``.
    """
    from .manifest_contract import ManifestContract

    if manifest_contract is ManifestContract.OPENAPM_V01:
        return _parse_v01_registries_block(data, apm_yml_path)

    raw = data.get("registries")
    if raw is None:
        return None, None
    if raw != {}:
        from ..deps.registry.feature_gate import require_package_registry_enabled

        require_package_registry_enabled("Top-level 'registries:' blocks")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Top-level 'registries:' block in {apm_yml_path} must be a "
            f"mapping (name -> {{url: ...}})"
        )

    default_value = raw.get("default")
    registries_map: dict[str, str] = {}
    for name, body in raw.items():
        if name == "default":
            continue
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Registry name in 'registries:' block must be a non-empty string (got {name!r})"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"Registry {name!r} must be a mapping with at least 'url:' "
                f"(got {type(body).__name__})"
            )
        # Token trap: tokens must never appear in repo-tracked YAML files.
        if "token" in body:
            from ..deps.registry.auth import registry_token_env_var

            raise ValueError(
                f"Registry {name!r}: 'token' must not appear in apm.yml. "
                f"Use the {registry_token_env_var(name)} "
                f"environment variable or 'apm config set registry.{name}.token <value>'."
            )
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Registry {name!r} is missing required field 'url:'")
        url = url.strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError(
                f"Registry {name!r} URL must start with https:// or http:// (got {url!r})"
            )
        # Reject any unknown keys to catch typos early.
        unknown = set(body.keys()) - {"url"}
        if unknown:
            raise ValueError(
                f"Registry {name!r} has unknown fields: {sorted(unknown)} (known fields: ['url'])"
            )
        registries_map[name] = url

    default_name: str | None = None
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
                f"unconfigured registry. Configured: {sorted(registries_map.keys())}"
            )

    if not registries_map and default_name is None:
        return None, None

    return registries_map, default_name


def routes_unscoped_to_registry(dep: Any) -> bool:
    """True when *dep* is an unscoped shorthand a default registry would claim.

    Single source of truth for the routing predicate, shared with the install
    CLI's probe-skip gate (``should_skip_github_probe_for_dep``) so the two
    cannot drift apart.

    Objects that lack an ``is_local`` attribute are treated as local (returns
    False) -- fail-closed for unknown dep types.
    """
    return getattr(dep, "source", None) not in {"git", "registry"} and not getattr(
        dep, "is_local", True
    )


def _route_unscoped_to_default_registry(
    dep_list: list,
    default_registry: str,
) -> None:
    """Route unscoped APM deps to *default_registry* in place.

    Two cases:
    * Object-form entries already parsed as ``source="registry"`` but with no
      ``registry_name`` (i.e. ``registry:`` key was omitted, caller relies on
      the project-level ``registries.default``).
    * String-shorthand entries with any ref — when a default registry is
      configured, all shorthands route there regardless of whether the ref
      looks like semver. A semver-shaped but malformed ref (e.g. ``^1.0``) is
      rejected later, at resolve time, so that read-only consumers of the
      manifest (``apm compile``, ``apm outdated``) are not aborted by a load.
      Use the explicit ``- git:`` object form to pin a dependency to Git when a
      default registry is active.
    """
    for dep in dep_list:
        if not isinstance(dep, DependencyReference):
            continue
        if dep.source == "registry" and dep.registry_name is None:
            dep.registry_name = default_registry
        elif routes_unscoped_to_registry(dep):
            if dep.reference:
                dep.source = "registry"
                dep.registry_name = default_registry
            else:
                raise ValueError(
                    f"no version constraint: '{dep.repo_url}' has no '#<version>' "
                    f"but would route to registry '{default_registry}'. "
                    f"Add a version selector (e.g. '{dep.repo_url}#1.0.0') or use "
                    f"'- git:' to keep this dependency on Git."
                )


def _iter_apm_dependency_lists(
    dependencies: dict[str, Any] | None,
    dev_dependencies: dict[str, Any] | None,
) -> Iterator[list[Any]]:
    """Yield each parsed ``dependencies['apm']`` / ``devDependencies['apm']`` list."""
    for bucket in (dependencies, dev_dependencies):
        if not bucket:
            continue
        apm_list = bucket.get("apm") if isinstance(bucket, dict) else None
        if isinstance(apm_list, list):
            yield apm_list


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
    canonical_targets: tuple[str, ...] = ()
    """Validated target names consumed by compile, install, and pack.

    ``target`` and ``targets`` remain compatibility projections of the
    author-facing spellings. Runtime consumers must use this tuple.
    """
    type: PackageContentType | None = (
        None  # Package content type: instructions, skill, hybrid, or prompts
    )
    includes: str | list[str] | None = None  # Include-only manifest: 'auto' or list of repo paths

    # Top-level ``registries:`` block per docs/proposals/registry-api.md §3.1.
    # Maps registry name -> base URL. None when no ``registries:`` block is present.
    registries: dict[str, str] | None = None
    # Value of ``registries.default:`` -- routes unscoped deps to this registry.
    default_registry: str | None = None
    manifest_contract: str = "working-draft"

    # Top-level ``allowExecutables:`` block -- per-package approval for
    # executable primitives (hooks, MCP servers, bin/ executables).
    # Mirrors npm v12's ``allowScripts`` in ``package.json``.
    # Keys are package handles with pinned version; values map exec type
    # to boolean (e.g. ``{"owner/repo#v1.0": {"hooks": true}}``).
    allow_executables: dict[str, dict[str, bool]] | None = None

    def __post_init__(self) -> None:
        """Derive the canonical target projection for compatibility callers."""
        if self.canonical_targets:
            return
        raw_targets: list[str] = []
        if self.targets is not None:
            raw_targets.extend(self.targets)
        elif isinstance(self.target, list):
            raw_targets.extend(self.target)
        elif isinstance(self.target, str):
            raw_targets.append(self.target)
        self.canonical_targets = tuple(
            name if name == "all" else normalize_target_name(name) for name in raw_targets
        )

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
            elif dep_type == "lsp":
                from .dependency.lsp import LSPDependency as LSPDep

                parsed_lsp: list = []
                for dep in dep_list:
                    if isinstance(dep, str):
                        parsed_lsp.append(LSPDep.from_string(dep))
                    elif isinstance(dep, dict):
                        try:
                            parsed_lsp.append(LSPDep.from_dict(dep))
                        except ValueError as e:
                            raise ValueError(f"Invalid {label}LSP dependency: {e}")  # noqa: B904
                parsed[dep_type] = parsed_lsp
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

        from .manifest_contract import negotiate_manifest_contract

        manifest_contract = negotiate_manifest_contract(data)

        # Required fields
        if "name" not in data:
            raise ValueError("Missing required field 'name' in apm.yml")
        if "version" not in data:
            raise ValueError("Missing required field 'version' in apm.yml")
        if not isinstance(data["name"], str) or not data["name"].strip():
            raise ValueError("Invalid apm.yml identity: 'name' must be a non-empty string")
        if not isinstance(data["version"], str) or not data["version"].strip():
            raise ValueError("Invalid apm.yml identity: 'version' must be a non-empty string")

        # Top-level ``registries:`` block per design §3.1.
        registries, default_registry = _parse_registries_block(
            data,
            apm_yml_path,
            manifest_contract,
        )

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

        # Merge user/policy registry URLs and config.json default routing.
        from ..deps.registry.config_loader import resolve_effective_registries

        registries, default_registry = resolve_effective_registries(registries, default_registry)
        if registries or default_registry:
            from ..deps.registry.feature_gate import require_package_registry_enabled

            require_package_registry_enabled("Registry configuration")

        # Route unscoped deps to the effective default registry when configured.
        if default_registry:
            for dep_list in _iter_apm_dependency_lists(dependencies, dev_dependencies):
                _route_unscoped_to_default_registry(dep_list, default_registry)

        # Parse allowExecutables block (npm v12-style approval gate).
        from ..security.executables import parse_allow_executables

        allow_executables = parse_allow_executables(data)

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
        target_value = None
        targets_value: list[str] | None = None
        if "targets" in data and "target" in data:
            target_value = parse_target_field(
                data.get("target"),
                source_path=apm_yml_path,
            )
            raw_targets = data.get("targets")
            targets_value = (
                [str(item).strip() for item in raw_targets if str(item).strip()]
                if isinstance(raw_targets, list)
                else [str(raw_targets).strip()]
            )
            canonical_targets = ()
        elif "targets" in data:
            targets_value = parse_targets_field(data)
            canonical_targets = tuple(targets_value)
        else:
            target_value = parse_target_field(
                data.get("target"),
                source_path=apm_yml_path,
            )
            parsed_target_values = (
                target_value
                if isinstance(target_value, list)
                else [target_value]
                if isinstance(target_value, str)
                else []
            )
            canonical_targets = tuple(
                name if name == "all" else normalize_target_name(name)
                for name in parsed_target_values
            )

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
            canonical_targets=canonical_targets,
            type=pkg_type,
            includes=includes,
            registries=registries,
            default_registry=default_registry,
            manifest_contract=manifest_contract.value,
            allow_executables=allow_executables,
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

    def get_all_apm_dependencies(self) -> list[DependencyReference]:
        """Get production and dev APM dependencies in manifest order."""
        return self.get_apm_dependencies() + self.get_dev_apm_dependencies()

    def has_any_apm_dependencies(self) -> bool:
        """Check if this package has any APM dependencies (prod or dev)."""
        return bool(self.get_apm_dependencies() or self.get_dev_apm_dependencies())

    def get_all_mcp_dependencies(self) -> list["MCPDependency"]:
        """Get production and dev MCP dependencies in manifest order."""
        return self.get_mcp_dependencies() + self.get_dev_mcp_dependencies()

    def get_lsp_dependencies(self) -> list["LSPDependency"]:
        """Get list of LSP dependencies."""
        if not self.dependencies or "lsp" not in self.dependencies:
            return []
        return [
            dep for dep in (self.dependencies.get("lsp") or []) if isinstance(dep, LSPDependency)
        ]

    def get_dev_lsp_dependencies(self) -> list["LSPDependency"]:
        """Get list of dev LSP dependencies."""
        if not self.dev_dependencies or "lsp" not in self.dev_dependencies:
            return []
        return [
            dep
            for dep in (self.dev_dependencies.get("lsp") or [])
            if isinstance(dep, LSPDependency)
        ]


def canonical_package_targets(package: object) -> tuple[str, ...]:
    """Return canonical targets for packages and compatibility stand-ins."""
    canonical = getattr(package, "canonical_targets", ())
    if isinstance(canonical, (tuple, list)) and canonical:
        return tuple(canonical)
    plural = getattr(package, "targets", None)
    if isinstance(plural, list) and plural:
        return tuple(name if name == "all" else normalize_target_name(name) for name in plural)
    singular = getattr(package, "target", None)
    values = singular if isinstance(singular, list) else [singular]
    return tuple(
        name if name == "all" else normalize_target_name(name)
        for name in values
        if isinstance(name, str) and name
    )


def canonical_package_target_config(package: object) -> dict[str, object]:
    """Project canonical targets into the compatibility config shape."""
    canonical = canonical_package_targets(package)
    if not canonical:
        return {}
    if isinstance(package, APMPackage):
        return {"targets": list(canonical)}
    plural = getattr(package, "targets", None)
    if isinstance(plural, list):
        return {"targets": list(canonical)}
    singular = getattr(package, "target", None)
    if isinstance(singular, str) and len(canonical) == 1:
        return {"target": singular}
    return {"targets": list(canonical)}


def package_target_selection(package: APMPackage) -> str | list[str] | None:
    """Return pack-compatible selection from the canonical package owner."""
    if package.targets is not None:
        return list(package.canonical_targets)
    if package.target is not None:
        return package.target
    return list(package.canonical_targets) or None


@dataclass
class PackageInfo:
    """Information about a downloaded/installed package."""

    package: APMPackage
    install_path: Path
    resolved_reference: ResolvedReference | None = None
    installed_at: str | None = None  # ISO timestamp
    dependency_ref: DependencyReference | None = (
        None  # Original dependency reference for canonical string
    )
    package_type: PackageType | None = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID
    root_local_project_root: Path | None = None
    deployment_package_root: Path | None = None  # Source root in the deployment output frame

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
            for primitive_type in [
                "instructions",
                "agents",
                "contexts",
                "prompts",
                "hooks",
            ]:
                primitive_dir = apm_dir / primitive_type
                if primitive_dir.exists() and any(primitive_dir.iterdir()):
                    return True

        # Also check hooks/ at package root (Claude-native convention)
        hooks_dir = self.install_path / "hooks"
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):  # noqa: SIM103
            return True

        return False


def build_installed_package_info(
    dep_ref: DependencyReference, apm_modules_dir: Path
) -> PackageInfo | None:
    """Build a ``PackageInfo`` for a dependency that is still installed on disk.

    Shared by callers that re-integrate primitives from remaining
    dependencies after some other package is removed -- e.g. ``apm
    uninstall``'s post-removal re-sync and ``apm prune``'s hook
    reconciliation (see ``HookIntegrator.reconcile_after_removal``).
    Returns ``None`` when the dependency's install directory is missing
    or fails manifest validation, so callers can skip it rather than
    fail the broader re-integration pass.
    """
    install_path = dep_ref.get_install_path(apm_modules_dir)
    if not install_path.exists():
        return None

    result = validate_apm_package(install_path)
    package = result.package if result and result.package else None
    if not package:
        return None

    return PackageInfo(
        package=package,
        install_path=install_path,
        dependency_ref=dep_ref,
        package_type=result.package_type if result else None,
    )


def surviving_dependency_refs_for_reintegration(
    apm_package: "APMPackage",
    project_root: Path,
    *,
    lockfile: "LockFile | None" = None,
) -> list[DependencyReference]:
    """Return packages still present after removal, for clear+rebuild.

    Prefer lockfile package dependencies (direct *and* transitive,
    excluding the virtual ``"."`` self-entry). Callers that already hold
    a post-removal in-memory lockfile (``apm uninstall``) must pass it
    explicitly -- the on-disk lockfile is still stale until uninstall
    writes it. When *lockfile* is omitted, the on-disk lockfile under
    *project_root* is loaded (safe for ``apm prune``, which writes the
    lockfile before hook reconciliation).

    Falls back to ``apm_package.get_all_apm_dependencies()`` (manifest
    directs only) when no lockfile is available.
    """
    # Deferred: LockFile imports DependencyReference from this module.
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    resolved = lockfile
    lockfile_source = "caller-provided"
    if resolved is None:
        lockfile_source = "on-disk"
        resolved = LockFile.read(get_lockfile_path(project_root))
    if resolved is not None:
        deps = [dep.to_dependency_ref() for dep in resolved.get_package_dependencies()]
        _log.debug(
            "Survivor set for re-integration: %d package(s) from %s lockfile",
            len(deps),
            lockfile_source,
        )
        return deps
    deps = list(apm_package.get_all_apm_dependencies())
    _log.debug(
        "Survivor set for re-integration: %d package(s) from manifest fallback "
        "(lockfile unavailable)",
        len(deps),
    )
    return deps
