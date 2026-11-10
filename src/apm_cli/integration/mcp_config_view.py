"""Canonical read-only view of current MCP source configuration.

The view derives current MCP declarations from the root manifest and only the
package manifests bounded by the current lockfile. It owns first-wins
projection and the symmetric comparison with the stored lockfile baseline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from apm_cli.deps.path_anchoring import (
    LocalResolutionError,
    build_local_parent_index,
    resolve_local_dep_dir,
)
from apm_cli.integration._shared import deduplicate_deps
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.validation import PackageType, detect_package_type

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.utils.diagnostics import DiagnosticCollector


class _McpViewLogger(Protocol):
    """Output methods used while deriving the current MCP view."""

    def progress(self, message: str) -> None:
        """Report an install-time trust decision."""

    def warning(self, message: str) -> None:
        """Report a trust decision that needs user attention."""

    def verbose_detail(self, message: str) -> None:
        """Report a non-actionable derivation detail in verbose mode."""


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
    declaration_fingerprints: Mapping[str, str] = field(default_factory=dict)
    omissions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    refusable_names: frozenset[str] = frozenset()
    pending_packages: frozenset[str] = frozenset()

    @classmethod
    def derive(
        cls,
        root: APMPackage,
        lockfile: LockFile | None,
        modules_root: Path,
        *,
        trust_transitive_self_defined: bool,
        diagnostics: DiagnosticCollector | None = None,
        logger: _McpViewLogger | None = None,
        trusted_transitive_configs: (Mapping[str, tuple[str, Mapping[str, Any]]] | None) = None,
        include_dependency_mcp: bool = True,
        allow_pending: bool | None = None,
        active_package_keys: frozenset[str] | None = None,
    ) -> CurrentMcpConfigView:
        """Derive MCP truth for the resolver's complete active graph, when supplied.

        ``allow_pending`` selects pending (True), strict (False), or legacy
        compatibility (None) evidence. Active membership is not a materialized
        package selection: unavailable active sources still require evidence.
        """
        project_root = root.package_path or Path.cwd()
        with ExitStack() as scratch:
            package_deps, problems, fingerprints, omissions, refusable, pending_packages = (
                _collect_locked_dependencies(
                    lockfile,
                    modules_root,
                    project_root,
                    scratch=scratch,
                    trust_transitive_self_defined=trust_transitive_self_defined,
                    diagnostics=diagnostics,
                    logger=logger,
                    trusted_transitive_configs=trusted_transitive_configs,
                    include_dependency_mcp=include_dependency_mcp,
                    allow_pending=allow_pending,
                    active_package_keys=active_package_keys,
                )
            )
        # Root is the authoring project: both prod and dev MCP are intentionally
        # included here (behaviour introduced in #1780). Transitive package deps
        # are collected prod-only (see _collect_locked_dependencies and
        # _collect_unlocked_compat).
        root_deps = root.get_all_mcp_dependencies()
        root_names = set(_get_server_configs(root_deps))
        dependencies = tuple(_deduplicate(root_deps + package_deps))
        configs = _get_server_configs(dependencies)
        return cls(
            dependencies=dependencies,
            configs=configs,
            provenance=_get_server_provenance(dependencies),
            problems=tuple(problems),
            pending_packages=frozenset(pending_packages),
            declaration_fingerprints={
                name: fingerprint
                for name, fingerprint in fingerprints.items()
                if name not in root_names
            },
            omissions={
                name: evidence for name, evidence in omissions.items() if name not in configs
            },
            refusable_names=frozenset(refusable - root_names),
        )

    def omission_evidence(self, lockfile: LockFile) -> dict[str, dict[str, str]]:
        """Record explicit omissions without erasing a previously admitted baseline."""
        admitted = _admitted_names(lockfile)
        return {
            name: dict(evidence)
            for name, evidence in self.omissions.items()
            if name not in admitted
        }

    def with_recorded_omissions(self, lockfile: LockFile) -> CurrentMcpConfigView:
        """Apply exact install evidence for audit, never infer intent from empty state."""
        configs = dict(self.configs)
        problems = list(self.problems)
        admitted = _admitted_names(lockfile)
        candidates = (
            (set(lockfile.mcp_omissions) & set(configs)) | set(self.declaration_fingerprints)
        ) - admitted
        for name in sorted(candidates):
            evidence = lockfile.mcp_omissions.get(name)
            fingerprint = self.declaration_fingerprints.get(name)
            valid = (
                fingerprint is not None
                and isinstance(evidence, dict)
                and evidence.get("reason") in ("only-apm", "untrusted-transitive")
                and evidence
                == {
                    "declaration": fingerprint,
                    "reason": evidence.get("reason"),
                }
                and (evidence["reason"] == "only-apm" or name in self.refusable_names)
            )
            if valid and name not in admitted:
                configs.pop(name, None)
            elif name not in admitted or evidence is not None:
                problems.append(
                    McpSourceProblem(
                        package_key=name,
                        manifest_path=Path("apm.lock.yaml"),
                        message=(
                            "MCP selection is unknown: omission evidence is missing, damaged "
                            "or stale. Repeat the intended install selection "
                            "('--only apm' for skills-only); do not enable MCP just to pass audit"
                        ),
                    )
                )
        return replace(self, configs=configs, problems=tuple(problems))

    def pending_servers(self, lockfile: LockFile) -> frozenset[str]:
        """Defer only unambiguous recorded declarers whose sources are unavailable."""
        declarers: dict[str, list[str]] = {}
        for key, dependency in lockfile.dependencies.items():
            if key != "." and dependency.name:
                declarers.setdefault(dependency.name, []).append(key)
        pending_names = {
            name
            for name, keys in declarers.items()
            if len(keys) == 1 and keys[0] in self.pending_packages
        }
        pending = set()
        for server, provenance in lockfile.mcp_config_provenance.items():
            owners = {provenance} if isinstance(provenance, str) else set(provenance)
            if owners and owners <= pending_names and server not in self.configs:
                pending.add(server)
        return frozenset(pending)

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


def _admitted_names(lockfile: LockFile) -> set[str]:
    """Retain every surviving indication of a previously admitted MCP baseline."""
    return (
        set(lockfile.mcp_servers)
        | set(lockfile.mcp_configs)
        | set(lockfile.mcp_config_provenance)
        | {name for names in lockfile.mcp_target_servers.values() for name in names}
    )


def _declaration_fingerprint(dependency: LockedDependency, declaration: MCPDependency) -> str:
    """Bind intent to canonical package identity, immutable source and exact declaration."""
    payload = {
        "dependency": dependency.get_unique_key(),
        "commit": dependency.resolved_commit,
        "digest": dependency.resolved_hash,
        "port": dependency.port,
        "source": dependency.source,
        "depth": dependency.depth,
        "declaration": declaration.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _authored_mcp_declarations(manifest: dict[str, Any]) -> dict[str, MCPDependency]:
    """Parse portable authored declarations through the canonical MCP model."""
    dependencies = manifest.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        raise ValueError("package dependencies must be a mapping")
    entries = dependencies.get("mcp")
    if not isinstance(entries, list):
        return {}
    declarations: dict[str, MCPDependency] = {}
    for entry in entries:
        if isinstance(entry, str):
            declaration = MCPDependency.from_string(entry)
        elif isinstance(entry, dict):
            declaration = MCPDependency.from_dict(entry)
        else:
            continue
        declarations.setdefault(declaration.name, declaration)
    return declarations


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


def _log_skipped_dev_mcp(
    package: APMPackage,
    declarer: str,
    logger: _McpViewLogger | None,
) -> None:
    """Explain a dependency-scope exclusion without logging server names."""
    skipped_count = len(package.get_dev_mcp_dependencies())
    if skipped_count and logger is not None:
        logger.verbose_detail(
            f"Skipping {skipped_count} author-only MCP server(s) from "
            f"'{declarer}'; transitive devDependencies.mcp do not propagate"
        )


def _matches_trusted_transitive_config(
    dependency: MCPDependency,
    declarer: str,
    trusted_configs: Mapping[str, tuple[str, Mapping[str, Any]]] | None,
) -> bool:
    """Return whether this package's unchanged config was trusted before."""
    if trusted_configs is None:
        return False
    trusted = trusted_configs.get(dependency.name)
    if trusted is None:
        return False
    stored_declarer, stored_config = trusted
    return stored_declarer == declarer and dependency.to_dict() == stored_config


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
    *,
    scratch: ExitStack,
    parent_index: dict[str, tuple[LockedDependency, ...]],
    resolved_cache: dict[str, Path],
) -> Path:
    """Resolve a locked package's canonical current manifest path."""
    if dependency.source == "local":
        package_dir = resolve_local_dep_dir(
            dependency,
            lockfile,
            project_root,
            parent_index=parent_index,
            resolved_cache=resolved_cache,
        )
        if package_dir.is_file():
            from tempfile import TemporaryDirectory

            from apm_cli.config import get_apm_temp_dir
            from apm_cli.deps.virtual_file import materialize_virtual_file_package
            from apm_cli.install.phases.local_content import read_local_package_source

            source_base = resolve_local_dep_dir(
                replace(dependency, local_path="."),
                lockfile,
                project_root,
                parent_index=parent_index,
            )
            dep_ref = dependency.to_dependency_ref()
            source = read_local_package_source(dep_ref, source_base)
            if source.content is None:
                raise ValueError(
                    f"Local single-file source changed kind during MCP inspection: {package_dir}"
                )
            stage = scratch.enter_context(
                TemporaryDirectory(prefix="apm-local-mcp-", dir=get_apm_temp_dir())
            )
            package_dir = Path(stage) / "package"
            materialize_virtual_file_package(dep_ref, package_dir, source.path.name, source.content)
    else:
        package_dir = dependency.to_dependency_ref().get_install_path(modules_root)
    return (package_dir / "apm.yml").resolve()


def _allows_missing_manifest(
    dependency: LockedDependency,
    package_dir: Path,
    logger: _McpViewLogger | None = None,
    *,
    modules_root: Path | None = None,
    native_ownership_cache: dict[Path, frozenset[str]] | None = None,
    strict: bool = False,
) -> bool:
    """Return whether the package contract permits an absent apm.yml."""
    from apm_cli.codex_plugins.manifest import is_codex_compatibility_manifest

    if not strict:
        if dependency.package_type == PackageType.SKILL_BUNDLE.value:
            return True
        if not package_dir.exists():
            if dependency.source == "local":
                return False
            if dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value:
                from apm_cli.codex_plugins.registrar import owns_codex_dependency

                return modules_root is not None and owns_codex_dependency(
                    modules_root,
                    dependency.get_unique_key(),
                    ownership_cache=native_ownership_cache,
                )
            accepted_types = {
                PackageType.APM_PACKAGE.value: "APM package",
                PackageType.CLAUDE_SKILL.value: "Claude skill",
                PackageType.AGENT_PLUGIN.value: "Agent plugin",
            }
            accepted_kind = accepted_types.get(dependency.package_type)
            if accepted_kind is None:
                return False
            if logger is not None:
                dep_label = dependency.name or package_dir.name
                logger.verbose_detail(
                    f"Skipping MCP check for '{dep_label}' -- locked {accepted_kind}; "
                    "package dir absent (cold cache; will hydrate from lock pins)"
                )
            return True
    elif not package_dir.exists():
        return False

    # A materialized Claude skill legitimately has no apm.yml, whether it was
    # declared at the repository root or in a virtual subdirectory. The lock
    # type and installed shape must agree before the manifest is waived.

    package_type, selected_manifest = detect_package_type(package_dir)
    if package_type is PackageType.SKILL_BUNDLE:
        return dependency.package_type == PackageType.SKILL_BUNDLE.value
    if package_type is PackageType.HOOK_PACKAGE:
        return (
            dependency.to_dependency_ref().is_virtual_subdirectory()
            and dependency.package_type == PackageType.HOOK_PACKAGE.value
        )
    if package_type is PackageType.AGENT_PLUGIN:
        return dependency.package_type == PackageType.AGENT_PLUGIN.value
    if is_codex_compatibility_manifest(package_dir, selected_manifest):
        return dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value
    return (
        package_type is PackageType.CLAUDE_SKILL
        and dependency.package_type == PackageType.CLAUDE_SKILL.value
    )


def _collect_locked_dependencies(
    lockfile: LockFile | None,
    modules_root: Path,
    project_root: Path,
    *,
    scratch: ExitStack,
    trust_transitive_self_defined: bool,
    diagnostics: DiagnosticCollector | None,
    logger: _McpViewLogger | None = None,
    trusted_transitive_configs: (Mapping[str, tuple[str, Mapping[str, Any]]] | None) = None,
    include_dependency_mcp: bool = True,
    allow_pending: bool | None = None,
    active_package_keys: frozenset[str] | None = None,
) -> tuple[
    list[MCPDependency],
    list[McpSourceProblem],
    dict[str, str],
    dict[str, dict[str, str]],
    set[str],
    set[str],
]:
    """Collect MCP declarations from only package manifests named by the lockfile."""
    if lockfile is None:
        return [], [], {}, {}, set(), set()

    collected: list[MCPDependency] = []
    problems: list[McpSourceProblem] = []
    fingerprints: dict[str, str] = {}
    omissions: dict[str, dict[str, str]] = {}
    refusable: set[str] = set()
    pending_packages: set[str] = set()
    parent_index = build_local_parent_index(lockfile)
    resolved_cache: dict[str, Path] = {}
    native_ownership_cache: dict[Path, frozenset[str]] = {}
    for package_key, dependency in lockfile.dependencies.items():
        if package_key == "." or (
            active_package_keys is not None and package_key not in active_package_keys
        ):
            continue
        fallback_path = _fallback_manifest_path(dependency, modules_root, project_root)
        try:
            manifest_path = _package_manifest_path(
                dependency,
                lockfile,
                modules_root,
                project_root,
                scratch=scratch,
                parent_index=parent_index,
                resolved_cache=resolved_cache,
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

        if allow_pending and not manifest_path.parent.exists() and dependency.source != "local":
            from apm_cli.drift import build_download_ref

            try:
                package_type = PackageType(dependency.package_type)
                if package_type is PackageType.INVALID:
                    raise ValueError("locked package shape is invalid")
                build_download_ref(
                    dependency.to_dependency_ref(),
                    lockfile,
                    update_refs=False,
                    ref_changed=False,
                    require_locked=True,
                )
            except ValueError as exc:
                problems.append(
                    McpSourceProblem(
                        package_key,
                        manifest_path,
                        f"cannot restore missing package from lock evidence: {exc}",
                    )
                )
            else:
                pending_packages.add(package_key)
                if logger is not None:
                    logger.verbose_detail(
                        f"Deferring cold-cache MCP verification for '{package_key}' "
                        "until its locked source is restored."
                    )
            continue

        if not manifest_path.exists():
            if _allows_missing_manifest(
                dependency,
                manifest_path.parent,
                logger,
                modules_root=modules_root,
                native_ownership_cache=native_ownership_cache,
                strict=allow_pending is not None,
            ):
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
            from apm_cli.utils.yaml_io import load_yaml

            manifest = load_yaml(manifest_path)
            if not isinstance(manifest, dict):
                raise ValueError("package manifest must be a mapping")
            package = APMPackage.from_mapping(
                manifest,
                package_path=manifest_path.parent,
                source_path=manifest_path.parent,
                manifest_path=manifest_path,
            )
            authored_declarations = _authored_mcp_declarations(manifest)
        except (OSError, ValueError, UnicodeError, yaml.YAMLError) as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=manifest_path,
                    message=f"cannot parse package manifest at {manifest_path}: {exc}",
                )
            )
            continue

        declarer = package.name or dependency.name or manifest_path.parent.name
        _log_skipped_dev_mcp(package, declarer, logger)
        # Only collect prod MCP from transitive packages; devDependencies.mcp
        # are scoped to the package author's own environment (#2340).
        for mcp_dependency in package.get_mcp_dependencies():
            fingerprint = _declaration_fingerprint(
                dependency, authored_declarations[mcp_dependency.name]
            )
            fingerprints.setdefault(mcp_dependency.name, fingerprint)
            if mcp_dependency.is_self_defined and dependency.depth > 1:
                refusable.add(mcp_dependency.name)
            if not include_dependency_mcp:
                omissions.setdefault(
                    mcp_dependency.name, {"declaration": fingerprint, "reason": "only-apm"}
                )
                continue
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
                elif _matches_trusted_transitive_config(
                    mcp_dependency,
                    declarer,
                    trusted_transitive_configs,
                ):
                    if logger is not None:
                        logger.verbose_detail(
                            f"Preserving unchanged trusted MCP server "
                            f"'{mcp_dependency.name}' from '{declarer}'"
                        )
                else:
                    omissions.setdefault(
                        mcp_dependency.name,
                        {"declaration": fingerprint, "reason": "untrusted-transitive"},
                    )
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
    return collected, problems, fingerprints, omissions, refusable, pending_packages


def _collect_unlocked_compat(
    modules_root: Path,
    *,
    trust_transitive_self_defined: bool,
    diagnostics: DiagnosticCollector | None,
    logger: _McpViewLogger | None,
) -> list[MCPDependency]:
    """Preserve the legacy no-lock fallback for the compatibility wrapper only."""
    collected: list[MCPDependency] = []
    for manifest_path in modules_root.rglob("apm.yml"):
        try:
            package = APMPackage.from_apm_yml(manifest_path, source_path=manifest_path.parent)
        except (OSError, ValueError, UnicodeError):
            continue
        declarer = package.name or manifest_path.parent.name
        _log_skipped_dev_mcp(package, declarer, logger)
        # Only collect prod MCP from packages; devDependencies.mcp are scoped to
        # the package author's own environment (#2340).
        for dependency in package.get_mcp_dependencies():
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
    logger: _McpViewLogger | None,
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
    with ExitStack() as scratch:
        dependencies, _, _, _, _, _ = _collect_locked_dependencies(
            lockfile,
            modules_root,
            project_root,
            scratch=scratch,
            trust_transitive_self_defined=trust_private,
            diagnostics=diagnostics,
            logger=logger,
        )
    return dependencies
