"""Canonical read-only view of current MCP source configuration.

The view derives current MCP declarations from the root manifest and only the
package manifests bounded by the current lockfile. It owns first-wins
projection and the symmetric comparison with the stored lockfile baseline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.deps.path_anchoring import LocalResolutionError, resolve_local_dep_dir
from apm_cli.integration._shared import deduplicate_deps
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.validation import PackageType, detect_package_type

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.utils.diagnostics import DiagnosticCollector


@dataclass(frozen=True)
class McpSourceProblem:
    """A locked package manifest that could not provide current MCP truth."""

    package_key: str
    manifest_path: Path
    message: str


@dataclass(frozen=True)
class CurrentMcpConfigView:
    """Current root plus lockfile-bounded MCP declarations and projections."""

    dependencies: tuple[MCPDependency, ...]
    configs: Mapping[str, Mapping[str, Any]]
    provenance: Mapping[str, str]
    problems: tuple[McpSourceProblem, ...]

    @classmethod
    def derive(
        cls,
        root: APMPackage,
        lockfile: LockFile | None,
        modules_root: Path,
        *,
        trust_transitive_self_defined: bool,
        diagnostics: DiagnosticCollector | None = None,
    ) -> CurrentMcpConfigView:
        """Derive current MCP truth without mutating manifests or the lockfile."""
        project_root = root.package_path or Path.cwd()
        package_deps, problems = _collect_locked_dependencies(
            lockfile,
            modules_root,
            project_root,
            trust_transitive_self_defined=trust_transitive_self_defined,
            diagnostics=diagnostics,
        )
        dependencies = tuple(_deduplicate(root.get_all_mcp_dependencies() + package_deps))
        return cls(
            dependencies=dependencies,
            configs=_get_server_configs(dependencies),
            provenance=_get_server_provenance(dependencies),
            problems=tuple(problems),
        )

    def diff(self, stored_configs: Mapping[str, Mapping[str, Any]]) -> McpConfigDiff:
        """Compare this current view with a stored lockfile baseline."""
        return McpConfigDiff.between(self.configs, stored_configs)


@dataclass(frozen=True)
class McpConfigDiff:
    """Symmetric name and value differences between current and stored MCP config."""

    changed: frozenset[str]
    source_only: frozenset[str]
    lock_only: frozenset[str]

    @classmethod
    def between(
        cls,
        current: Mapping[str, Mapping[str, Any]],
        stored: Mapping[str, Mapping[str, Any]],
    ) -> McpConfigDiff:
        """Partition changed, source-only, and lock-only server names."""
        current_names = frozenset(current)
        stored_names = frozenset(stored)
        shared = current_names & stored_names
        return cls(
            changed=frozenset(name for name in shared if current[name] != stored[name]),
            source_only=current_names - stored_names,
            lock_only=stored_names - current_names,
        )

    @property
    def is_empty(self) -> bool:
        """Return whether current and stored configurations are identical."""
        return not (self.changed or self.source_only or self.lock_only)


def _deduplicate(dependencies: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Return first-wins dependencies using the shared MCP/LSP semantics."""
    return deduplicate_deps(list(dependencies))


def _get_server_configs(dependencies: list[Any] | tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    """Project dependencies to their serialized server configurations."""
    configs: dict[str, dict[str, Any]] = {}
    for dependency in dependencies:
        if hasattr(dependency, "to_dict") and hasattr(dependency, "name"):
            configs[dependency.name] = dependency.to_dict()
        elif isinstance(dependency, str):
            configs[dependency] = {"name": dependency}
    return configs


def _get_server_provenance(dependencies: list[Any] | tuple[Any, ...]) -> dict[str, str]:
    """Project surviving transitive dependencies to their declaring package."""
    provenance: dict[str, str] = {}
    for dependency in dependencies:
        resolved_by = getattr(dependency, "resolved_by", None)
        if resolved_by and hasattr(dependency, "name"):
            provenance[dependency.name] = resolved_by
    return provenance


def _fallback_manifest_path(
    dependency: LockedDependency,
    modules_root: Path,
    project_root: Path,
) -> Path:
    """Return a useful manifest location when canonical path resolution fails."""
    if dependency.source == "local" and dependency.local_path:
        raw = Path(dependency.local_path).expanduser()
        package_dir = raw if raw.is_absolute() else project_root / raw
        return (package_dir / "apm.yml").resolve()
    return (modules_root / dependency.repo_url / "apm.yml").resolve()


def _package_manifest_path(
    dependency: LockedDependency,
    lockfile: LockFile,
    modules_root: Path,
    project_root: Path,
) -> Path:
    """Resolve a locked package's canonical current manifest path."""
    if dependency.source == "local":
        package_dir = resolve_local_dep_dir(dependency, lockfile, project_root)
    else:
        package_dir = dependency.to_dependency_ref().get_install_path(modules_root)
    return (package_dir / "apm.yml").resolve()


def _allows_missing_manifest(
    dependency: LockedDependency,
    package_dir: Path,
) -> bool:
    """Return whether the package contract permits an absent apm.yml."""
    if dependency.package_type == PackageType.SKILL_BUNDLE.value:
        return True

    dependency_ref = dependency.to_dependency_ref()
    if not dependency_ref.is_virtual_subdirectory():
        return False

    # ``apm audit --ci`` runs without materialising ``apm_modules/``: the drift
    # replay uses a throwaway scratch dir, and a setup-only CI job (install the
    # CLI, then audit -- no ``apm install``) never populates the real modules
    # tree.  When the package directory is absent we cannot probe the on-disk
    # shape, so fall back to the frozen lockfile classification -- a virtual
    # ``claude_skill`` legitimately ships no ``apm.yml`` by design.  Once the
    # modules ARE materialised the strict shape probe below still runs and
    # guards against a mislabeled or malformed installed package.
    if not package_dir.exists():
        return dependency.package_type == PackageType.CLAUDE_SKILL.value

    package_type, _ = detect_package_type(package_dir)
    return package_type is PackageType.CLAUDE_SKILL


def _collect_locked_dependencies(
    lockfile: LockFile | None,
    modules_root: Path,
    project_root: Path,
    *,
    trust_transitive_self_defined: bool,
    diagnostics: DiagnosticCollector | None,
    logger: Any | None = None,
) -> tuple[list[MCPDependency], list[McpSourceProblem]]:
    """Collect MCP declarations from only package manifests named by the lockfile."""
    if lockfile is None:
        return [], []

    collected: list[MCPDependency] = []
    problems: list[McpSourceProblem] = []
    for package_key, dependency in lockfile.dependencies.items():
        if package_key == ".":
            continue
        fallback_path = _fallback_manifest_path(dependency, modules_root, project_root)
        try:
            manifest_path = _package_manifest_path(
                dependency,
                lockfile,
                modules_root,
                project_root,
            )
        except LocalResolutionError as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=fallback_path,
                    message=(
                        f"cannot resolve local package: {exc}; "
                        "re-run 'apm install' to rebuild the lockfile"
                    ),
                )
            )
            continue
        except (OSError, ValueError) as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=fallback_path,
                    message=f"cannot locate package manifest: {exc}",
                )
            )
            continue

        if not manifest_path.exists():
            if _allows_missing_manifest(dependency, manifest_path.parent):
                continue
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=manifest_path,
                    message=(
                        f"package manifest not found at {manifest_path}; "
                        "re-run 'apm install' to restore it"
                    ),
                )
            )
            continue

        try:
            package = APMPackage.from_apm_yml(
                manifest_path,
                source_path=manifest_path.parent,
            )
        except (OSError, ValueError, UnicodeError) as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=manifest_path,
                    message=f"cannot parse package manifest at {manifest_path}: {exc}",
                )
            )
            continue

        declarer = package.name or dependency.name or manifest_path.parent.name
        for mcp_dependency in package.get_all_mcp_dependencies():
            if mcp_dependency.is_self_defined:
                if dependency.depth == 1:
                    if logger is not None:
                        logger.progress(
                            f"Trusting direct dependency MCP '{mcp_dependency.name}' "
                            f"from '{declarer}'"
                        )
                elif trust_transitive_self_defined:
                    if logger is not None:
                        logger.progress(
                            f"Trusting self-defined MCP server '{mcp_dependency.name}' "
                            f"from transitive package '{declarer}' (--trust-transitive-mcp)"
                        )
                else:
                    message = (
                        f"Transitive package '{declarer}' declares self-defined MCP server "
                        f"'{mcp_dependency.name}' (registry: false). Re-declare it in your "
                        "apm.yml or use --trust-transitive-mcp."
                    )
                    if diagnostics is not None:
                        diagnostics.warn(message)
                    elif logger is not None:
                        logger.warning(message)
                    continue
            collected.append(replace(mcp_dependency, resolved_by=declarer))
    return collected, problems


def _collect_unlocked_compat(
    modules_root: Path,
    *,
    trust_transitive_self_defined: bool,
    diagnostics: DiagnosticCollector | None,
    logger: Any | None,
) -> list[MCPDependency]:
    """Preserve the legacy no-lock fallback for the compatibility wrapper only."""
    collected: list[MCPDependency] = []
    for manifest_path in modules_root.rglob("apm.yml"):
        try:
            package = APMPackage.from_apm_yml(manifest_path, source_path=manifest_path.parent)
        except (OSError, ValueError, UnicodeError):
            continue
        declarer = package.name or manifest_path.parent.name
        for dependency in package.get_all_mcp_dependencies():
            if dependency.is_self_defined and not trust_transitive_self_defined:
                message = (
                    f"Transitive package '{declarer}' declares self-defined MCP server "
                    f"'{dependency.name}' (registry: false). Re-declare it in your apm.yml "
                    "or use --trust-transitive-mcp."
                )
                if diagnostics is not None:
                    diagnostics.warn(message)
                elif logger is not None:
                    logger.warning(message)
                continue
            collected.append(replace(dependency, resolved_by=declarer))
    return collected


def _collect_transitive_compat(
    modules_root: Path,
    lock_path: Path | None,
    trust_private: bool,
    *,
    logger: Any | None,
    diagnostics: DiagnosticCollector | None,
) -> list[MCPDependency]:
    """Implement the legacy MCPIntegrator traversal through this owner."""
    from apm_cli.deps.lockfile import LockFile

    if not modules_root.exists():
        return []
    lockfile = LockFile.read(lock_path) if lock_path is not None and lock_path.exists() else None
    if lockfile is None:
        return _collect_unlocked_compat(
            modules_root,
            trust_transitive_self_defined=trust_private,
            diagnostics=diagnostics,
            logger=logger,
        )
    project_root = lock_path.parent if lock_path is not None else Path.cwd()
    dependencies, _ = _collect_locked_dependencies(
        lockfile,
        modules_root,
        project_root,
        trust_transitive_self_defined=trust_private,
        diagnostics=diagnostics,
        logger=logger,
    )
    return dependencies
