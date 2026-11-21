"""APM uninstall engine  -- validation, removal, and cleanup helpers."""

import builtins
import contextlib
import shlex
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING
from uuid import uuid4

from apm_cli.core.remediation import (
    INTEGRATED_FILES_STALE,
    LOCAL_DEPENDENCY_UNMAPPED,
    LOCAL_DEPENDENCY_UNRESOLVED,
    LOCKFILE_RECORDS_INCONSISTENT,
    MARKETPLACE_REF_UNRESOLVED,
    MCP_DEPENDENCY_DECLARED,
    REGISTRY_CANONICAL_UNRECORDED,
)

from ...bundle.local_bundle import looks_like_local_bundle
from ...constants import APM_MODULES_DIR
from ...core.command_logger import CommandLogger
from ...deps.lockfile import LockFile
from ...install.orphans import OrphanOperation, ReachabilityProbe, decide_orphan_removal
from ...integration.mcp_config_view import BUNDLE_MCP_OWNER_PREFIX
from ...integration.mcp_integrator import MCPIntegrator
from ...models.apm_package import DependencyReference
from ...models.dependency.selection import (
    DependencySelection,
    DependencySelectionStatus,
    manifest_declarations_of,
    select_manifest_dependency,
    select_marketplace_declaration,
)
from ...models.dependency.selection import (
    parse_dependency_entry as _parse_dependency_entry,
)
from ...utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree
from ...utils.paths import portable_relpath
from .local_survivors import _surviving_local_refs_at_install_path

if TYPE_CHECKING:
    from ...core.deployment_ledger import DeploymentMaterializationSession
    from ...integration.cleanup import DeploymentEvidence


class MCPUninstallCleanupError(RuntimeError):
    """Aggregate target-specific MCP cleanup failures."""

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        noun = "target" if len(failures) == 1 else "targets"
        details = "; ".join(f"{runtime}: {error}" for runtime, error in failures)
        super().__init__(f"MCP cleanup failed for {len(failures)} {noun}: {details}")


def _is_marketplace_ref(package: str) -> bool:
    """Check if *package* is marketplace notation using the public API."""
    from ...marketplace.resolver import parse_marketplace_ref

    return parse_marketplace_ref(package) is not None


def _removal_target(dep_entry: object, lockfile: LockFile | None) -> object:
    """Return what removing a manifest declaration acts on: lock row, checkout, edges.

    An object-form ``{name, marketplace}`` declaration is identified as
    ``_marketplace/<alias>/<plugin>``; its lock row, ``apm_modules`` checkout
    and transitive edges belong to the one locked row recorded for that
    alias/plugin pair, matched case-insensitively like the selection (#3928).
    With no such row the entry is returned unchanged (nothing was locked); an
    ambiguous pair is refused earlier by
    :func:`_refuse_unsafe_marketplace_removals`.
    """
    rows = _locked_marketplace_rows(dep_entry, lockfile)
    return rows[0].to_dependency_ref() if len(rows) == 1 else dep_entry


def _locked_marketplace_rows(dep_entry: object, lockfile: LockFile | None) -> list:
    """Return the lock rows an object-form declaration's alias/plugin pair records."""
    if lockfile is None:
        return []
    try:
        dependency = _parse_dependency_entry(dep_entry)
    except (ValueError, TypeError, AttributeError, KeyError):
        return []
    if not dependency.is_marketplace:
        return []
    return lockfile.marketplace_rows(
        dependency.marketplace_name or "", dependency.marketplace_plugin_name or ""
    )


def _removal_targets(packages: list, lockfile: LockFile | None) -> list:
    """Map removed manifest declarations to their lock and disk targets."""
    return [_removal_target(package, lockfile) for package in packages]


def _marketplace_declaration_label(dep_entry: object) -> str | None:
    try:
        dependency = _parse_dependency_entry(dep_entry)
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    if not dependency.is_marketplace:
        return None
    return f"{dependency.marketplace_plugin_name}@{dependency.marketplace_name}"


def _refuse_unsafe_marketplace_removals(
    packages_to_remove: list, current_deps: list, lockfile: LockFile | None, logger
) -> list[str]:
    """Refuse marketplace removals that would leave package state behind (#3928).

    An object-form ``{name, marketplace}`` entry whose alias/plugin pair is
    recorded on several lock rows cannot be mapped to one package, so removing
    it would drop the declaration but keep its lock row and files. And an
    object-form entry plus another declaration (for example the same package's
    Git URL) can own one locked coordinate; removing only one would delete the
    checkout, files and lock row the survivor still needs. Both fail closed.
    """
    if lockfile is None or not packages_to_remove:
        return []
    removed = manifest_declarations_of(current_deps, packages_to_remove)
    refused: list[str] = []
    for entry in removed:
        rows = _locked_marketplace_rows(entry, lockfile)
        if len(rows) > 1:
            label = _marketplace_declaration_label(entry)
            logger.error(
                f"apm.lock.yaml records {len(rows)} packages for {label}, so uninstall "
                "cannot tell which one to remove. Nothing was changed. "
                f"{LOCKFILE_RECORDS_INCONSISTENT.sentence()}"
            )
            refused.append(label)
    if refused:
        return refused
    removed_ids = {id(entry) for entry in removed}
    removed_by_key = {_removed_lock_key(entry, lockfile): entry for entry in removed}
    for survivor in current_deps:
        if id(survivor) in removed_ids:
            continue
        removed_entry = removed_by_key.get(_removed_lock_key(survivor, lockfile))
        if removed_entry is None:
            continue
        removed_label = _marketplace_declaration_label(removed_entry)
        survivor_label = _marketplace_declaration_label(survivor)
        if removed_label is None and survivor_label is None:
            continue
        removed_label = removed_label or _dependency_public_label(removed_entry)
        survivor_label = survivor_label or _dependency_public_label(survivor)
        logger.error(
            f"{removed_label} and {survivor_label} are the same installed package, and "
            f"{survivor_label} would stay declared in apm.yml. Remove both together: "
            f"apm uninstall {shlex.quote(removed_label)} {shlex.quote(survivor_label)}"
        )
        refused.append(removed_label)
    return refused


def _removed_lock_key(dep_entry: object, lockfile: LockFile | None) -> str:
    """Return the lock key a removed manifest declaration owns."""
    target = _removal_target(dep_entry, lockfile)
    try:
        return _parse_dependency_entry(target).get_unique_key()
    except (ValueError, TypeError, AttributeError, KeyError):
        return target if isinstance(target, str) else str(target)


def _count_noun(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``count`` with a correctly pluralized noun (``1 file``, ``2 files``)."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _is_host_local_spelling(local_path: str) -> bool:
    """Return whether a declared local path names a machine-specific location."""
    return local_path.startswith(("/", "\\", "~")) or bool(PureWindowsPath(local_path).drive)


def _dependency_lifecycle_label(dep_entry: object) -> str:
    """Return the portable package name passed to uninstall lifecycle scripts."""
    try:
        dependency = _parse_dependency_entry(dep_entry)
    except (ValueError, TypeError, AttributeError, KeyError):
        return dep_entry if isinstance(dep_entry, str) else str(dep_entry)
    if dependency.is_local:
        return dependency.repo_url
    return dependency.get_identity()


def _progress_once(logger, printed: set[str], message: str, **kwargs) -> None:
    """Print a removal line once per run (#4331).

    Two declarations of one package share a public label, so their removal
    lines are identical; each distinct line prints once.
    """
    if message in printed:
        return
    printed.add(message)
    logger.progress(message, **kwargs)


def _dependency_public_label(dep_entry: object) -> str:
    """Return a path-safe dependency label for user-facing uninstall output.

    Relative local declarations keep the manifest spelling (``./pkg``); a
    declaration naming an absolute or home path falls back to the portable
    ``_local/<name>`` key so diagnostics never print machine-specific paths.
    """
    try:
        dependency = _parse_dependency_entry(dep_entry)
        local_path = dependency.local_path if dependency.is_local else None
    except (ValueError, TypeError, AttributeError, KeyError):
        local_path = None
    if local_path and not _is_host_local_spelling(local_path):
        return local_path
    return _dependency_lifecycle_label(dep_entry)


def _is_private_local_identity(value: str) -> bool:
    """Return whether a diagnostic identity can expose a declared local path."""
    return value.startswith(("local:", "_local/")) or DependencyReference.is_local_path(value)


def _reintegration_error_detail(
    dependency: DependencyReference,
    error: Exception,
) -> str:
    """Return verbose failure detail without exposing a local declaration."""
    error_type = type(error).__name__
    if dependency.is_local:
        return error_type
    return f"{error_type}: {error}"


class _LocalRefreshLogger:
    """Redact declared source paths from local re-materialization failures."""

    def __init__(self, logger: CommandLogger, package_label: str) -> None:
        self._logger = logger
        self._package_label = package_label
        self.failed = False

    def error(self, _message: str) -> None:
        """Report a safe, actionable local refresh failure."""
        self.failed = True
        self._logger.error(
            f"Failed to refresh {self._package_label} from its declared local path. "
            f"{LOCAL_DEPENDENCY_UNRESOLVED.sentence()}"
        )


class LocalSurvivorRefreshError(RuntimeError):
    """Raised when a shared local install slot cannot preserve its survivors."""


@dataclass(frozen=True)
class LocalSlotRefresh:
    """Validated staged content for one shared local materialization slot."""

    install_path: Path
    staged_path: Path
    survivor_keys: tuple[str, ...]


_LOCAL_REFRESH_BACKUP_SUFFIX = ".apm-uninstall-backup"


def _cleanup_staged_local_refreshes(
    refreshes: dict[Path, LocalSlotRefresh],
    apm_modules_dir: Path,
) -> None:
    """Best-effort cleanup for hidden local refresh staging directories."""
    for refresh in refreshes.values():
        if not refresh.staged_path.exists():
            continue
        try:
            safe_rmtree(refresh.staged_path, apm_modules_dir)
        except OSError:
            continue


def _recover_local_refresh_backups(
    apm_modules_dir: Path,
    logger: CommandLogger,
) -> None:
    """Recover or remove deterministic backups left by interrupted activation."""
    local_root = apm_modules_dir / "_local"
    if not local_root.is_dir():
        return
    for backup_path in local_root.glob(f".*{_LOCAL_REFRESH_BACKUP_SUFFIX}"):
        target_name = backup_path.name[1 : -len(_LOCAL_REFRESH_BACKUP_SUFFIX)]
        if not target_name:
            continue
        install_path = local_root / target_name
        ensure_path_within(backup_path, apm_modules_dir)
        ensure_path_within(install_path, apm_modules_dir)
        if install_path.exists():
            safe_rmtree(backup_path, apm_modules_dir)
            continue
        backup_path.rename(install_path)
        logger.progress(f"Recovered interrupted local materialization for _local/{target_name}")


def _stage_shared_local_survivors(
    packages_to_remove: list[object],
    surviving_dependencies: list[object],
    apm_modules_dir: Path,
    project_root: Path,
    logger: CommandLogger,
) -> dict[Path, LocalSlotRefresh]:
    """Validate and stage every surviving shared-slot local package."""
    from ...install.phases.local_content import _copy_local_package

    _recover_local_refresh_backups(apm_modules_dir, logger)
    refreshes: dict[Path, LocalSlotRefresh] = {}
    for package in packages_to_remove:
        try:
            install_path = _parse_dependency_entry(package).get_install_path(apm_modules_dir)
        except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
            continue
        if install_path in refreshes:
            continue
        survivors = _surviving_local_refs_at_install_path(
            package,
            surviving_dependencies,
            apm_modules_dir,
        )
        if not survivors:
            continue
        if len(survivors) > 1:
            logger.error(
                f"Cannot preserve {_dependency_public_label(package)} because "
                f"{len(survivors)} declarations would still share one install slot. "
                "Use exact paths from apm.yml in one uninstall command so at most "
                "one declaration remains."
            )
            _cleanup_staged_local_refreshes(refreshes, apm_modules_dir)
            raise LocalSurvivorRefreshError(
                "Multiple local declarations would remain in one shared install slot. "
                "No manifest changes were made."
            )

        staged_path = apm_modules_dir / "_local" / f".apm-uninstall-refresh-{uuid4().hex}"
        refresh_logger = _LocalRefreshLogger(
            logger,
            _dependency_public_label(package),
        )
        try:
            for survivor in survivors:
                staged = _copy_local_package(
                    survivor,
                    staged_path,
                    project_root,
                    project_root=project_root,
                    logger=refresh_logger,
                )
                if staged is None:
                    raise LocalSurvivorRefreshError(
                        "A surviving local dependency could not be staged. "
                        "No manifest changes were made."
                    )
            from ...models.validation import validate_apm_package

            validation = validate_apm_package(staged_path)
            if not validation.is_valid or validation.package is None:
                refresh_logger.error("staged local package validation failed")
                raise LocalSurvivorRefreshError(
                    "A surviving local dependency could not be staged. "
                    "No manifest changes were made."
                )
        except Exception as exc:
            if staged_path.exists():
                safe_rmtree(staged_path, apm_modules_dir)
            _cleanup_staged_local_refreshes(refreshes, apm_modules_dir)
            if isinstance(exc, LocalSurvivorRefreshError):
                raise
            logger.verbose_detail(f"    Local refresh staging error type: {type(exc).__name__}")
            refresh_logger.error("local refresh staging failed")
            raise LocalSurvivorRefreshError(
                "A surviving local dependency could not be staged. No manifest changes were made."
            ) from None

        refreshes[install_path] = LocalSlotRefresh(
            install_path=install_path,
            staged_path=staged_path,
            survivor_keys=tuple(survivor.get_unique_key() for survivor in survivors),
        )
    return refreshes


def _activate_staged_local_refresh(
    refresh: LocalSlotRefresh,
    apm_modules_dir: Path,
) -> None:
    """Replace one materialization with staged survivor content or restore it."""
    install_path = refresh.install_path
    ensure_path_within(install_path, apm_modules_dir)
    ensure_path_within(refresh.staged_path, apm_modules_dir)
    backup_path = install_path.parent / (f".{install_path.name}{_LOCAL_REFRESH_BACKUP_SUFFIX}")
    ensure_path_within(backup_path, apm_modules_dir)
    if backup_path.exists():
        raise LocalSurvivorRefreshError(
            "A stale local refresh backup must be recovered before activation."
        )
    if install_path.exists():
        install_path.rename(backup_path)
    try:
        refresh.staged_path.rename(install_path)
    except OSError as exc:
        if backup_path.exists() and not install_path.exists():
            backup_path.rename(install_path)
        raise LocalSurvivorRefreshError(
            "A staged local dependency could not replace its shared install slot."
        ) from exc
    if backup_path.exists():
        safe_rmtree(backup_path, apm_modules_dir)


def _read_survivor_direct_refs(apm_yml_path, packages_to_remove):
    """Parse apm.yml's direct APM dependencies, excluding *packages_to_remove*.

    Seeds the forward reachability walk (see
    :func:`apm_cli.deps.reachability.compute_forward_reachable_keys`) with
    the project's SURVIVING direct dependencies. For the real uninstall
    path and ``--dry-run`` preview, *apm_yml_path* is still pre-edit.
    Identity subtraction turns the pre-edit dependency list into the
    correct post-removal survivor set, mirroring the identity
    matching ``_validate_uninstall_packages`` already uses.

    Returns ``(production_refs, dev_refs)`` so the walk can apply install's
    ``is_dev`` rule. Returns empty lists -- never raises -- if *apm_yml_path* is ``None``,
    missing, or malformed; the project's own manifest is a Step-1/3
    concern here, not part of this walk's fail-closed contract, which
    covers the manifests visited DURING the walk, not this seed step.
    """
    if apm_yml_path is None:
        return [], []

    removed_identities = builtins.set()
    for pkg in packages_to_remove:
        try:
            removed_identities.add(_parse_dependency_entry(pkg).get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    import yaml

    from ...utils.yaml_io import load_yaml

    try:
        data = load_yaml(apm_yml_path) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return [], []

    refs: dict[str, list] = {"dependencies": [], "devDependencies": []}
    for section_name, section_refs in refs.items():
        for dep_entry in data.get(section_name, {}).get("apm", []) or []:
            try:
                ref = _parse_dependency_entry(dep_entry)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            if ref.get_identity() not in removed_identities:
                section_refs.append(ref)
    return refs["dependencies"], refs["devDependencies"]


def _warn_reachability_incomplete(reachability, logger):
    """Emit exactly one bounded warning when reachability could not be verified.

    Fires at most once per ``_uninstall_orphan_decision`` call regardless of
    how many manifests were unverifiable during the walk -- the per-node
    detail is confined to ``--verbose`` so the always-shown message stays
    a single, bounded line.
    """
    logger.warning(
        f"Skipped transitive dependency cleanup -- {len(reachability.unverifiable)} "
        "package manifest(s) could not be verified. Keeping all transitive "
        "dependencies as a precaution."
    )
    if not logger.verbose:
        logger.info("Run with --verbose to see which manifest(s) failed.")
    logger.info("Fix or restore the affected manifest(s), then re-run to complete cleanup.")
    for pkg_id, reason in reachability.unverifiable:
        if _is_private_local_identity(pkg_id):
            logger.verbose_detail("    local dependency: package manifest could not be verified")
        else:
            logger.verbose_detail(f"    {pkg_id}: {reason}")


def _uninstall_orphan_decision(
    lockfile,
    packages_to_remove,
    apm_modules_dir,
    apm_yml_path,
    logger,
    *,
    warn_on_incomplete: bool = True,
):
    """Ask the orphan owner which removed packages' descendants go too.

    ``apm uninstall`` and its ``--dry-run`` preview share this one decision,
    so a preview and the real run never disagree about what is kept. The
    owner consumes :mod:`apm_cli.deps.reachability` to rescue a candidate a
    SURVIVING package's own manifest still reaches, and keeps every
    candidate when that walk is incomplete. Only the real removal path may
    persist ``decision.repairs``; ``--dry-run`` stays read-only.
    """
    direct_refs, dev_refs = _read_survivor_direct_refs(apm_yml_path, packages_to_remove)
    decision = decide_orphan_removal(
        operation=OrphanOperation.UNINSTALL,
        roots=_removal_targets(packages_to_remove, lockfile) if lockfile else (),
        candidate_universe=lockfile or None,
        installed_paths=None,
        resolved_keys=None,
        intended=None,
        materialized=None,
        reachability=ReachabilityProbe(
            project_root=apm_yml_path.parent if apm_yml_path else Path("."),
            apm_modules_dir=apm_modules_dir,
            direct_refs=tuple(direct_refs),
            dev_refs=tuple(dev_refs),
        ),
        deployment_claims=None,
    )
    if not decision.reachability_complete and warn_on_incomplete:
        _warn_reachability_incomplete(decision.reachability, logger)
    return decision


def _select_marketplace_declarations(
    marketplace_refs: set[str], current_deps: list
) -> dict[str, DependencySelection]:
    """Map each ``plugin@marketplace`` ref that names an object-form declaration."""
    from ...marketplace.resolver import parse_marketplace_ref

    selections: dict[str, DependencySelection] = {}
    for package in marketplace_refs:
        parsed = parse_marketplace_ref(package)
        if parsed is None:
            continue
        plugin_name, marketplace_name, _ref = parsed
        selection = select_marketplace_declaration(plugin_name, marketplace_name, current_deps)
        if selection.status is not DependencySelectionStatus.NOT_FOUND:
            selections[package] = selection
    return selections


def _resolve_marketplace_packages(
    packages: list[str],
    lockfile: "LockFile | None",
    logger: "CommandLogger",
    auth_resolver=None,
    dry_run: bool = False,
) -> dict[str, str | None]:
    """Resolve marketplace refs (NAME@MARKETPLACE[#REF]) to canonical owner/repo strings.

    Resolution proceeds in three stages for each marketplace-formatted package:

    1. **Lockfile lookup (offline)**: scan ``lockfile.dependencies`` for entries
       where ``discovered_via == marketplace_name`` and
       ``marketplace_plugin_name == plugin_name``.  When found, use the
       dependency's unique key as the canonical identity.  If an entry for the
       same plugin name exists under a *different* marketplace, a provenance-
       mismatch warning is emitted and that entry is used.
    2. **Registry fallback (silent)**: call :func:`parse_marketplace_ref` then
       :func:`resolve_marketplace_plugin` to obtain the canonical ``owner/repo``
       from the marketplace registry.  Skipped when *dry_run* is ``True``.
       A supply-chain guard refuses any canonical that is not already present
       in the lockfile (prevents a poisoned registry from removing an unrelated
       installed package).  Network errors fail only the affected package;
       remaining packages in the batch continue.
    3. **Unresolvable**: an error is logged with marketplace-specific wording
       and the package maps to ``None`` in the returned dict.

    Args:
        packages: List of marketplace-formatted package strings to resolve.
        lockfile: Current :class:`~apm_cli.deps.lockfile.LockFile` object, or
            ``None`` when no lockfile exists.
        logger: :class:`~apm_cli.core.command_logger.CommandLogger` for output.
        auth_resolver: Optional auth resolver forwarded to the registry call.
        dry_run: When ``True``, skip the network registry call (Stage 2).

    Returns:
        A dict mapping each original marketplace ref to its resolved canonical
        string, or ``None`` when resolution failed.
    """
    from ...marketplace.resolver import parse_marketplace_ref, resolve_marketplace_plugin

    resolved: dict[str, str | None] = {}

    # Pre-index lockfile dependencies once for the entire batch.
    # Exact matches preserve the first canonical entry for each provenance pair.
    # Plugin matches retain every entry in insertion order so fallback can select
    # the first entry whose provenance differs from the requested marketplace.
    exact_index: dict[tuple[str, str], str] = {}
    plugin_index: dict[str, list[tuple[str, str]]] = {}
    # Case-insensitive rows for the origin check, matched like marketplace_rows.
    origin_rows: dict[tuple[str, str], list] = {}
    if lockfile is not None:
        for dep in lockfile.dependencies.values():
            mp = dep.marketplace_plugin_name
            via = dep.discovered_via
            if not mp or not via:
                continue
            canonical_key = dep.get_unique_key()
            exact_key = (via, mp)
            exact_index.setdefault(exact_key, canonical_key)
            plugin_index.setdefault(mp, []).append((via, canonical_key))
            origin_rows.setdefault((via.casefold(), mp.casefold()), []).append(dep)

    for package in packages:
        parsed = parse_marketplace_ref(package)
        if parsed is None:
            continue  # Not a marketplace ref; skipped silently

        plugin_name, marketplace_name, _ref = parsed
        canonical: str | None = None

        # Stage 1: Lockfile-first lookup (offline, zero network calls)
        if lockfile is not None:
            # Exact match: both discovered_via and marketplace_plugin_name match
            canonical = exact_index.get((marketplace_name, plugin_name))

            # Provenance-mismatch fallback: first same-plugin entry via a different marketplace
            if canonical is None:
                for via, candidate_key in plugin_index.get(plugin_name, []):
                    if via != marketplace_name:
                        canonical = candidate_key
                        logger.warning(
                            f"{plugin_name}@{marketplace_name} not found; "
                            f"package was installed via {via}. "
                            f"Proceeding with uninstall of {canonical}."
                        )
                        break

        # Stage 2: Registry fallback (silent, mirrors install behaviour)
        if canonical is None:
            if dry_run:
                logger.verbose_detail(
                    f"Skipping registry fallback for {plugin_name}@{marketplace_name} "
                    "(dry-run mode)"
                )
            else:
                logger.progress(
                    f"Resolving {plugin_name}@{marketplace_name} via registry...",
                    symbol="search",
                )
                try:
                    from ...marketplace.catalog_origin import require_locked_row_origin

                    # #3959: a rebound alias fails closed before any catalog I/O.
                    for locked in origin_rows.get(
                        (marketplace_name.casefold(), plugin_name.casefold()), ()
                    ):
                        require_locked_row_origin(locked, marketplace_name=marketplace_name)
                    resolution = resolve_marketplace_plugin(
                        plugin_name,
                        marketplace_name,
                        auth_resolver=auth_resolver,
                        enforce_ref_pins=False,
                    )
                    canonical = resolution.canonical
                    # Supply-chain guard: refuse registry canonicals not present in lockfile
                    if lockfile is not None and canonical not in lockfile.dependencies:
                        logger.warning(
                            f"Registry resolved {plugin_name}@{marketplace_name} to "
                            f"{canonical}, but it is not recorded in apm.lock.yaml. "
                            "Refusing as a supply-chain precaution. "
                            f"{REGISTRY_CANONICAL_UNRECORDED.bind(canonical=canonical).sentence()}"
                        )
                        canonical = None
                    elif lockfile is None:
                        # No lockfile means no offline integrity anchor; behaviour is
                        # accepted today but tracked as a supply-chain follow-up.
                        logger.verbose_detail(
                            f"No lockfile present; trusting registry canonical "
                            f"{canonical} for {plugin_name}@{marketplace_name}."
                        )
                except Exception as exc:
                    logger.warning(
                        f"Registry lookup for {plugin_name}@{marketplace_name} failed: "
                        f"{exc}. Falling back to apm.yml match."
                    )

        # Stage 3: Not found in either source -- surface a clear error
        if canonical is None:
            if dry_run:
                logger.warning(
                    f"{plugin_name}@{marketplace_name} could not be resolved in dry-run "
                    "(registry fallback skipped). Re-run without --dry-run, or use "
                    "owner/repo notation to preview directly."
                )
            else:
                logger.error(
                    f"{plugin_name}@{marketplace_name} could not be resolved -- "
                    "find its owner/repo canonical name with `apm list`, then "
                    f"{MARKETPLACE_REF_UNRESOLVED.clause()}."
                )

        resolved[package] = canonical

    return resolved


def _mcp_server_hint(name: str, lockfile: "LockFile | None") -> str | None:
    """Route an MCP server name to the command that removes it (#3994).

    ``apm uninstall`` selects packages; an MCP server leaves with its
    declaration: the root manifest, the package that brings it in, or the
    local bundle that installed it.
    """
    if lockfile is None or name not in lockfile.mcp_servers:
        return None
    owner = lockfile.mcp_config_provenance.get(name)
    if isinstance(owner, str) and owner.startswith(BUNDLE_MCP_OWNER_PREFIX):
        return (
            f"'{name}' is an MCP server from a local bundle; "
            "'apm uninstall' cannot remove local-bundle content yet (tracked in #3887)."
        )
    if isinstance(owner, str) and owner:
        return (
            f"'{name}' is an MCP server brought in by package '{owner}'. "
            "Uninstall that package to remove it."
        )
    return (
        f"'{name}' is an MCP server, not a package. "
        f"{MCP_DEPENDENCY_DECLARED.bind(name=name).sentence()}"
    )


def _validate_uninstall_packages(
    packages: list[str],
    current_deps: list,
    logger: "CommandLogger",
    lockfile: "LockFile | None" = None,
    auth_resolver=None,
    dry_run: bool = False,
    log_matches: bool = True,
) -> tuple[list, list]:
    """Validate which packages can be removed and return matched/unmatched lists.

    Accepts both canonical ``owner/repo`` strings and marketplace refs of the
    form ``NAME@MARKETPLACE[#REF]``.  Marketplace refs are resolved to their
    canonical form before being matched against the ``current_deps`` list from
    ``apm.yml``.

    Args:
        packages: Package identifiers supplied by the user.
        current_deps: Current dependency list read from ``apm.yml``.
        logger: :class:`~apm_cli.core.command_logger.CommandLogger` for output.
        lockfile: Optional :class:`~apm_cli.deps.lockfile.LockFile` used for
            offline marketplace resolution.  When ``None`` the registry fallback
            is attempted instead.
        auth_resolver: Optional auth resolver forwarded to the registry call.
        dry_run: When ``True``, skip the network registry call in Stage 2.
        log_matches: Emit success diagnostics for matched identifiers.

    Returns:
        A two-tuple ``(packages_to_remove, packages_not_found)`` where
        *packages_to_remove* contains matched dep entries and
        *packages_not_found* contains unresolved or unmatched package strings.
    """
    # Pre-resolve any marketplace refs before the main validation loop. A ref
    # that names an object-form ``{name, marketplace}`` declaration is selected
    # from apm.yml directly, with no lock or registry lookup (#3928).
    mkt_refs_set = {p for p in packages if _is_marketplace_ref(p)}
    declared_selections = _select_marketplace_declarations(mkt_refs_set, current_deps)
    mkt_resolved: dict[str, str | None] = {}
    if unresolved := mkt_refs_set - declared_selections.keys():
        mkt_resolved = _resolve_marketplace_packages(
            sorted(unresolved),
            lockfile,
            logger,
            auth_resolver=auth_resolver,
            dry_run=dry_run,
        )

    packages_to_remove = []
    seen_entry_ids: set[int] = set()
    printed: set[str] = set()
    packages_not_found = []

    for package in packages:
        # A package arg is either: (a) a marketplace ref in
        # `name@marketplace[#ref]` form (no slash), (b) an `owner/repo`
        # slug, or (c) a local filesystem path. The legacy guard below
        # only handled (a) when there is no `/`, but Windows absolute
        # paths use backslashes (e.g. `C:\Users\...\my-pkg`) and have
        # no `/` either -- they were wrongly rejected as "Invalid
        # package format" and the DB row for any deployed copilot-app
        # workflow would leak. Use the canonical local-path detector
        # so paths fall through to DependencyReference parsing on
        # every platform.
        is_local = DependencyReference.is_local_path(package)
        selection: DependencySelection | None = declared_selections.get(package)
        if selection is not None:
            canonical_for_match = display_label = package
        elif "/" not in package and not is_local:
            if package in mkt_refs_set:
                canonical = mkt_resolved.get(package)
                if canonical is None:
                    # Error already logged by _resolve_marketplace_packages;
                    # surface in packages_not_found so caller counts are accurate.
                    packages_not_found.append(package)
                    continue
                canonical_for_match = canonical
                display_label = package
            else:
                logger.error(
                    _mcp_server_hint(package, lockfile)
                    or f"Invalid package format: {package}. "
                    "Use 'owner/repo', 'plugin-name@marketplace', a local path, "
                    "or a key copied from 'apm deps list'."
                )
                packages_not_found.append(package)
                continue
        else:
            canonical_for_match = package
            display_label = package

        if selection is None:
            selection = select_manifest_dependency(canonical_for_match, current_deps, lockfile)
        if selection.status is DependencySelectionStatus.MATCHED:
            matched_dep = selection.manifest_entry
            # Selection returns the original manifest object, including unhashable mappings.
            entry_id = id(matched_dep)
            if entry_id not in seen_entry_ids:
                seen_entry_ids.add(entry_id)
                packages_to_remove.append(matched_dep)
            if not log_matches:
                continue
            if canonical_for_match != display_label:
                _progress_once(
                    logger,
                    printed,
                    f"{display_label} - found in apm.yml (as {canonical_for_match})",
                    symbol="check",
                )
            else:
                _progress_once(
                    logger,
                    printed,
                    f"{_dependency_public_label(matched_dep)} - found in apm.yml",
                    symbol="check",
                )
        elif selection.status is DependencySelectionStatus.AMBIGUOUS:
            packages_not_found.append(package)
            if is_local:
                safe_label = _dependency_public_label(package)
                logger.error(
                    f"Local dependency '{safe_label}' is ambiguous: it matches "
                    f"{selection.match_count} declarations. Use one exact path from "
                    "apm.yml to uninstall a single dependency."
                )
            else:
                logger.error(
                    f"Ambiguous dependency '{display_label}': it matches "
                    f"{selection.match_count} declarations. Use one exact path from apm.yml "
                    "to uninstall a single dependency."
                )
        else:
            packages_not_found.append(package)
            if canonical_for_match.startswith("_local/"):
                logger.error(
                    f"{display_label} could not be mapped to one declared local dependency. "
                    f"{LOCAL_DEPENDENCY_UNMAPPED.sentence()[:-1]}, or use one exact "
                    "path from apm.yml."
                )
            elif is_local and looks_like_local_bundle(Path(package)):
                logger.error(
                    f"'{Path(package).name}' is a local bundle. Bundles "
                    "installed with 'apm install <bundle>' are not recorded in apm.yml, "
                    "and 'apm uninstall' cannot remove local-bundle content yet "
                    "(tracked in #3887)."
                )
            elif is_local:
                logger.error(
                    "The requested local path was not found in apm.yml. "
                    "Copy a local key from 'apm deps list', or use one exact "
                    "path already declared in apm.yml."
                )
            elif canonical_for_match != display_label:
                logger.error(
                    f"{display_label} ({canonical_for_match}) was not found in apm.yml. "
                    "Run 'apm deps list' and retry with an installed package identifier."
                )
            else:
                logger.error(
                    f"{display_label} was not found in apm.yml. "
                    "Run 'apm deps list' and retry with an installed package identifier."
                )

    packages_not_found.extend(
        _refuse_unsafe_marketplace_removals(packages_to_remove, current_deps, lockfile, logger)
    )
    return packages_to_remove, packages_not_found


class _DiscardSelectionLogger:
    """Drop uninstall-worded selection diagnostics; read-only callers word their own."""

    verbose = False

    def error(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def progress(self, *_args, **_kwargs) -> None:
        return None

    def verbose_detail(self, *_args, **_kwargs) -> None:
        return None


def manifest_dependency_entries(manifest: object) -> list:
    """Return every ``dependencies.apm`` then ``devDependencies.apm`` entry of *manifest*.

    The one candidate list both ``apm uninstall`` and read-only selection hand
    to :func:`_validate_uninstall_packages`, so they always agree (#3953).
    """
    entries: list = []
    if not isinstance(manifest, dict):
        return entries
    for section in ("dependencies", "devDependencies"):
        block = manifest.get(section)
        apm_entries = block.get("apm") if isinstance(block, dict) else None
        if isinstance(apm_entries, list):
            entries.extend(apm_entries)
    return entries


@dataclass(frozen=True)
class InstalledLockSelection:
    """The lock row a read-only identifier selects.

    ``installed_via`` names the marketplace the row was installed from when a
    ``plugin@marketplace`` identifier matched it through uninstall's
    provenance-mismatch fallback, so the caller can say so instead of
    auditing a different marketplace's plugin silently.
    """

    key: str
    installed_via: str | None = None


def select_installed_lock_key(
    package: str, manifest: object, lockfile: "LockFile | None"
) -> InstalledLockSelection | None:
    """Select the ``apm.lock.yaml`` row the installed package *package* names (#3953).

    Read-only commands such as ``apm audit PACKAGE`` select through the uninstall
    selector, so every identifier ``apm uninstall`` accepts (``owner/repo``, a
    URL, ``plugin@marketplace``, the ``_local/<name>`` key ``apm deps list``
    prints, a declared local path) maps to the lock row a removal would act on.
    Selection is offline: the marketplace registry is never consulted. A lock
    key, or an identifier that normalizes to one, also selects a locked
    transitive package apm.yml does not declare. Callers enter the manifest's
    ``default_host`` scope first. Returns ``None`` when nothing installed matches.
    """
    from ...marketplace.resolver import parse_marketplace_ref

    installed = lockfile.dependencies if lockfile is not None else {}
    if package in installed:
        return InstalledLockSelection(key=package)
    selected, _refused = _validate_uninstall_packages(
        [package],
        manifest_dependency_entries(manifest),
        _DiscardSelectionLogger(),
        lockfile,
        dry_run=True,
        log_matches=False,
    )
    for candidate in [*selected, package]:
        key = _removed_lock_key(candidate, lockfile)
        if key not in installed:
            continue
        parsed = parse_marketplace_ref(package)
        via = installed[key].discovered_via
        if parsed is not None and via and via.casefold() != parsed[1].casefold():
            return InstalledLockSelection(key=key, installed_via=via)
        return InstalledLockSelection(key=key)
    return None


def _dry_run_uninstall(
    packages_to_remove,
    apm_modules_dir,
    logger,
    apm_yml_path=None,
    *,
    surviving_dependencies=None,
    lockfile=None,
):
    """Show what would be removed without making changes.

    *apm_yml_path* is the project's manifest, read here BEFORE it is
    rewritten (unlike the real uninstall path, this preview runs before
    ``cli.py``'s Step 3 edits apm.yml). It defaults to ``None`` for
    direct-unit-test callers that don't set up a diamond-dependency
    scenario; production call sites always pass the real path so the
    reachability rescue (see ``_uninstall_orphan_decision``) is exercised
    for every real ``--dry-run`` invocation.
    """
    if lockfile is None:
        from ...deps.lockfile import get_lockfile_path

        lockfile = LockFile.read(get_lockfile_path(Path(".")))
    logger.progress(f"Dry run: Would remove {_count_noun(len(packages_to_remove), 'package')}:")
    printed: set[str] = set()
    for pkg in packages_to_remove:
        package_label = _dependency_public_label(pkg)
        _progress_once(logger, printed, f"  - {package_label} from apm.yml")
        try:
            dep_ref = _parse_dependency_entry(_removal_target(pkg, lockfile))
            package_path = dep_ref.get_install_path(apm_modules_dir)
        except (ValueError, TypeError, AttributeError, KeyError):
            pkg_str = pkg if isinstance(pkg, str) else str(pkg)
            package_path = apm_modules_dir / pkg_str.split("/")[-1]
        if apm_modules_dir.exists() and package_path.exists():
            shared_survivors = _surviving_local_refs_at_install_path(
                pkg,
                surviving_dependencies or [],
                apm_modules_dir,
            )
            if shared_survivors:
                _progress_once(
                    logger,
                    printed,
                    f"  - refresh {package_label} in apm_modules/ for "
                    f"{_count_noun(len(shared_survivors), 'surviving declaration')}",
                )
            else:
                _progress_once(logger, printed, f"  - {package_label} from apm_modules/")

    if lockfile:
        # Resolved-by repairs are intentionally discarded: --dry-run's preview
        # stays strictly read-only.
        actual_orphans, _resolved_by_repairs = _project_transitive_orphans(
            lockfile,
            packages_to_remove,
            apm_modules_dir,
            apm_yml_path,
            logger,
        )
        if actual_orphans:
            logger.progress(f"  Transitive dependencies that would be removed:")  # noqa: F541
            for orphan_key in sorted(actual_orphans):
                logger.progress(f"    - {orphan_key}")

    logger.success("Dry run complete - no changes made")


def _remove_packages_from_disk(
    packages_to_remove,
    apm_modules_dir,
    logger,
    *,
    staged_refreshes=None,
    refreshed_survivor_keys=None,
    lockfile=None,
):
    """Remove direct packages, stopping on failure before ownership is released."""
    removed = 0
    if not apm_modules_dir.exists():
        return removed

    deleted_pkg_paths = []
    activated_refresh_paths: set[Path] = set()
    from ...integration.base_integrator import BaseIntegrator

    try:
        for package in packages_to_remove:
            package_label = _dependency_public_label(package)
            try:
                dep_ref = _parse_dependency_entry(_removal_target(package, lockfile))
                package_path = dep_ref.get_install_path(apm_modules_dir)
            except PathTraversalError as e:
                logger.error(f"Refusing to remove {package_label}: {e}")
                raise
            except (ValueError, TypeError, AttributeError, KeyError):
                package_str = package if isinstance(package, str) else str(package)
                repo_parts = package_str.split("/")
                if len(repo_parts) >= 2:
                    package_path = apm_modules_dir.joinpath(*repo_parts)
                else:
                    package_path = apm_modules_dir / package_str

            refresh = (staged_refreshes or {}).get(package_path)
            if refresh is not None:
                if package_path in activated_refresh_paths:
                    continue
                _activate_staged_local_refresh(refresh, apm_modules_dir)
                activated_refresh_paths.add(package_path)
                if refreshed_survivor_keys is not None:
                    refreshed_survivor_keys.update(refresh.survivor_keys)
                logger.progress(
                    f"Refreshed {package_label} in apm_modules/ for "
                    f"{_count_noun(len(refresh.survivor_keys), 'surviving declaration')}"
                )
                continue

            if package_path in deleted_pkg_paths:
                # Another declaration of the same package already removed it (#4331).
                continue
            if package_path.exists():
                try:
                    safe_rmtree(package_path, apm_modules_dir)
                    logger.progress(f"Removed {package_label} from apm_modules/")
                    logger.verbose_detail(
                        f"    Path: {portable_relpath(package_path, apm_modules_dir)}"
                    )
                    removed += 1
                    deleted_pkg_paths.append(package_path)
                except PathTraversalError as e:
                    logger.error(f"Refusing to remove {package_label} from apm_modules/: {e}")
                    raise
                except OSError as e:
                    logger.error(f"Failed to remove {package_label} from apm_modules/: {e}")
                    raise
            else:
                logger.warning(f"Package {package_label} not found in apm_modules/")
    finally:
        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)
    return removed


def _cleanup_transitive_orphans(
    lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger
):
    """Remove orphaned transitive deps and return (removed_count, actual_orphan_keys)."""

    if not lockfile or not apm_modules_dir.exists():
        return 0, builtins.set()

    actual_orphans, resolved_by_repairs = _project_transitive_orphans(
        lockfile,
        packages_to_remove,
        apm_modules_dir,
        apm_yml_path,
        logger,
    )

    # Repair rescued candidates' resolved_by/local_path BEFORE deleting
    # the true orphans below. Without this, a rescued entry keeps pointing
    # at a parent that may no longer be in the lockfile, which silently
    # defeats the NEXT uninstall's backward orphan-candidate scan
    # (_build_children_index is keyed on resolved_by) and permanently
    # fail-closes any later reachability walk needing to re-resolve this
    # same entry's on-disk directory (see install/orphans.py's
    # docstring). Real removal path only -- --dry-run never reaches here.
    for rescued_key, (new_parent, new_local_path, dev_only) in resolved_by_repairs.items():
        rescued_dep = lockfile.get_dependency(rescued_key)
        if rescued_dep is None:
            continue
        rescued_dep.resolved_by = new_parent
        rescued_dep.is_dev = dev_only
        if new_local_path is not None:
            rescued_dep.local_path = new_local_path

    if not actual_orphans:
        return 0, builtins.set()

    removed = 0
    deleted_orphan_paths = []
    from ...integration.base_integrator import BaseIntegrator

    try:
        for orphan_key in actual_orphans:
            orphan_dep = lockfile.get_dependency(orphan_key)
            if not orphan_dep:
                continue
            try:
                orphan_ref = orphan_dep.to_dependency_ref()
                orphan_path = orphan_ref.get_install_path(apm_modules_dir)
            except PathTraversalError as e:
                logger.error(f"Refusing to remove transitive dep {orphan_key}: {e}")
                raise
            except ValueError:
                parts = orphan_key.split("/")
                orphan_path = (
                    apm_modules_dir.joinpath(*parts)
                    if len(parts) >= 2
                    else apm_modules_dir / orphan_key
                )

            if orphan_path.exists():
                try:
                    safe_rmtree(orphan_path, apm_modules_dir)
                    logger.progress(f"Removed transitive dependency {orphan_key} from apm_modules/")
                    logger.verbose_detail(
                        f"    Path: {portable_relpath(orphan_path, apm_modules_dir)}"
                    )
                    removed += 1
                    deleted_orphan_paths.append(orphan_path)
                except PathTraversalError as e:
                    logger.error(f"Refusing to remove transitive dep {orphan_key}: {e}")
                    raise
                except OSError as e:
                    logger.error(f"Failed to remove transitive dep {orphan_key}: {e}")
                    raise
    finally:
        BaseIntegrator.cleanup_empty_parents(deleted_orphan_paths, stop_at=apm_modules_dir)
    return removed, actual_orphans


def _shrink_surviving_selections(
    lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger
) -> builtins.set[str]:
    """Narrow survivors' locked selections to what surviving edges still declare.

    A node's ``skill_subset``/``target_subset`` is the merge over every edge
    that declares it (#3735). Once a parent is uninstalled its contribution
    must leave the union. The merge is recomputed from the surviving
    manifests through the canonical owner; an incomplete walk keeps the
    locked union, because missing evidence is not proof a selection is unused.

    Returns the lock keys whose selection changed; their deployed files must
    be removed and re-integrated with the new selection.
    """
    if not lockfile or apm_yml_path is None or not apm_modules_dir.exists():
        return builtins.set()
    from ...deps.reachability import declared_node_selections

    direct_refs, dev_refs = _read_survivor_direct_refs(apm_yml_path, packages_to_remove)
    declared = declared_node_selections(
        lockfile, apm_yml_path.parent, apm_modules_dir, direct_refs, dev_refs=dev_refs
    )
    if not declared.complete:
        logger.verbose_detail(
            "    Kept recorded skill and target selections: "
            f"{len(declared.unverifiable)} package manifest(s) could not be verified"
        )
        return builtins.set()
    changed = builtins.set()
    for key, selection in declared.selections.items():
        locked = lockfile.get_dependency(key)
        if locked is None:
            continue
        try:
            unchanged = locked.selection() == selection.lock_selection()
        except ValueError:
            unchanged = False  # invalid recorded pairing: rewrite it from the survivors
        if unchanged:
            continue
        locked.skill_subset = list(selection.skill_subset or ())
        locked.target_subset = list(selection.target_subset or ())
        exact = selection.subset_pairings
        locked.subset_pairings = exact.to_lock_records() if exact is not None else None
        changed.add(key)
        label = "local dependency" if _is_private_local_identity(key) else key
        logger.verbose_detail(f"    Updated selection of {label} to its surviving declarations")
    return changed


def _project_transitive_orphans(
    lockfile,
    packages_to_remove,
    apm_modules_dir,
    apm_yml_path,
    logger,
    *,
    warn_on_incomplete: bool = True,
):
    """Return the post-uninstall ``(orphan keys, repairs)`` without mutating state."""
    if not lockfile:
        return builtins.frozenset(), {}
    decision = _uninstall_orphan_decision(
        lockfile,
        packages_to_remove,
        apm_modules_dir,
        apm_yml_path,
        logger,
        warn_on_incomplete=warn_on_incomplete,
    )
    return decision.removed_keys, dict(decision.repairs)


def _preflight_uninstall_survivors(
    surviving_dependencies: list[object],
    modules_dir: Path,
    *,
    lockfile: LockFile | None = None,
    excluded_keys: set[str] | None = None,
    require_valid_installed: bool = False,
    source_root: Path | None = None,
    logger: CommandLogger | None = None,
) -> list[tuple[DependencyReference, object]]:
    """Validate every reintegration survivor before uninstall can mutate state."""
    from ...agent_plugins.errors import (
        AgentPluginTargetExcludedError,
        enforce_agent_plugin_deployment_boundary,
        preflight_reintegration_survivors,
    )
    from ...models.apm_package import PackageInfo
    from ...models.validation import validate_apm_package

    del logger  # Nothing to warn about: target exclusion is routine and silent.
    excluded = excluded_keys or set()
    if source_root is not None:
        for entry in surviving_dependencies:
            dep_ref = _parse_dependency_entry(entry)
            if not dep_ref.is_local or not dep_ref.local_path:
                continue
            source_path = Path(dep_ref.local_path).expanduser()
            if not source_path.is_absolute():
                source_path = (source_root / source_path).resolve()
            validation = validate_apm_package(source_path, source_path=source_path)
            if validation.is_valid and validation.package is not None:
                with contextlib.suppress(AgentPluginTargetExcludedError):
                    enforce_agent_plugin_deployment_boundary(
                        PackageInfo(
                            package=validation.package,
                            install_path=source_path,
                            dependency_ref=dep_ref,
                            package_type=validation.package_type,
                        )
                    )
    if lockfile is not None:
        refs = [
            dependency.to_dependency_ref()
            for dependency in lockfile.get_package_dependencies()
            if dependency.get_unique_key() not in excluded
        ]
    else:
        refs = [_parse_dependency_entry(entry) for entry in surviving_dependencies]

    installed_refs = [
        dep_ref
        for dep_ref in refs
        if dep_ref.get_unique_key() not in excluded
        and not (source_root is not None and dep_ref.is_local and dep_ref.replacement is None)
    ]
    return preflight_reintegration_survivors(
        installed_refs,
        modules_dir,
        require_valid_installed=require_valid_installed,
    )


@dataclass(frozen=True)
class IntegrationCleanupOutcome:
    """Complete result of the post-uninstall integration cleanup."""

    counts: dict[str, int]
    deployed_files: dict[str, list[str]]
    failed_paths: list[str]
    error_count: int
    materialization_session: "DeploymentMaterializationSession | None" = None

    @property
    def complete(self) -> bool:
        """Return whether every integration cleanup operation succeeded."""
        return self.error_count == 0


def _native_hook_state_exists(project_root: Path, targets: list[object]) -> bool:
    """Return whether cleanup may rewrite a merged native-hook config."""
    from ...integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS

    for target in targets:
        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is None:
            continue
        target_dir = project_root / target.root_dir
        if (target_dir / config.config_filename).exists() or (
            target_dir / _APM_HOOKS_SIDECAR
        ).exists():
            return True
    return False


class _SurvivorRebuildLogger:
    """Logger for the survivor rebuild after an uninstall (#4160, #4219).

    Survivors are redeployed from their recorded selection, so their
    per-primitive integration lines carry no news and no package header:
    they are dropped. Warnings and errors pass through unchanged.
    """

    def __init__(self, logger: CommandLogger) -> None:
        self._logger = logger

    def tree_item(self, _message: str) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._logger, name)


def _sync_integrations_after_uninstall(
    apm_package: object,
    project_root: Path,
    all_deployed_files: set[str],
    logger: CommandLogger,
    user_scope: bool = False,
    lockfile: LockFile | None = None,
    modules_dir: Path | None = None,
    survivor_plan: list[tuple[DependencyReference, object]] | None = None,
    deployed_file_hashes: dict[str, str] | None = None,
    excluded_survivor_keys: frozenset[str] = frozenset(),
    *,
    evidence: "DeploymentEvidence | None",
    executable_context: tuple[dict[str, dict[str, bool]] | None, bool] = (None, False),
) -> "IntegrationCleanupOutcome":
    """Remove deployed files and re-integrate from remaining packages.

    When *user_scope* is ``True``, targets are resolved for user-level
    deployment so cleanup and re-integration use the correct paths.

    *lockfile*, when provided, must be the post-removal in-memory
    lockfile (orphans already deleted). The on-disk lockfile is still
    stale at this point in the uninstall pipeline, so Phase 2 must not
    re-read it from disk.

    Survivors in *excluded_survivor_keys* are not re-integrated, such as
    packages just removed from the selected profile only.
    """
    from ...core.deployment_ledger import DeploymentMaterializationSession
    from ...install.package_resolution import effective_dependency_selection
    from ...install.services import (
        IntegratorBundle,
        integrate_package_primitives,
    )
    from ...install.target_filter import resolve_effective_package_targets
    from ...integration.base_integrator import BaseIntegrator
    from ...integration.dispatch import get_dispatch_table
    from ...integration.hook_reintegration import preflight_survivor_hooks
    from ...integration.skill_layout import (
        recorded_deployed_paths,
        resolve_reintegration_targets,
        resolve_skill_layout,
        with_every_skill_layout,
    )
    from ...primitives.discovery import clear_discovery_cache

    installed_modules_dir = modules_dir or Path(APM_MODULES_DIR)
    config_target = list(apm_package.canonical_targets)
    _explicit = config_target or None
    # Survivors are rebuilt in the skill layout they recorded; cleanup of the
    # removed packages' paths recognises both layouts.
    _base_targets = resolve_reintegration_targets(
        project_root, user_scope=user_scope, explicit_target=_explicit
    )
    _resolved_targets = resolve_skill_layout(
        _base_targets, recorded_paths=recorded_deployed_paths(lockfile)
    )
    _skill_cleanup_targets = with_every_skill_layout(_base_targets)
    require_valid_survivors = bool(all_deployed_files) or _native_hook_state_exists(
        project_root,
        _resolved_targets,
    )
    validated_survivors = (
        survivor_plan
        if survivor_plan is not None
        else _preflight_uninstall_survivors(
            list(apm_package.get_all_apm_dependencies()),
            installed_modules_dir,
            lockfile=lockfile,
            require_valid_installed=require_valid_survivors,
            logger=logger,
        )
    )

    # Phase 2 re-integration walks the on-disk primitive set after Phase 1
    # has removed the uninstalled package's files. The process-scoped
    # discovery memo populated earlier in this CLI run would otherwise
    # serve the pre-removal snapshot, causing deleted primitives to be
    # re-integrated. See #1533 follow-up.
    clear_discovery_cache()

    _dispatch = get_dispatch_table()
    _integrators = {name: entry.integrator_class() for name, entry in _dispatch.items()}
    _integrator_bundle = IntegratorBundle(
        prompt=_integrators["prompts"],
        agent=_integrators["agents"],
        skill=_integrators["skills"],
        instruction=_integrators["instructions"],
        command=_integrators["commands"],
        hook=_integrators["hooks"],
        canvas=_integrators["canvas"],
    )

    # Resolve targets once -- used for both Phase 1 removal and Phase 2 re-integration.
    target_survivor_plan = []
    for dep_ref, pkg_info in validated_survivors:
        if dep_ref.get_unique_key() in excluded_survivor_keys:
            continue
        target_selection = resolve_effective_package_targets(
            _resolved_targets,
            effective_dependency_selection(dep_ref).target_subset,
            pkg_info,
            None,
            dep_ref.get_identity(),
        )
        authorized_targets = []
        for target in target_selection.targets:
            authorized_targets.append(target)
        target_survivor_plan.append((dep_ref, pkg_info, authorized_targets))

    # Refuse survivors whose hooks Phase 2 would reject before Phase 1 wipes
    # their known-good merged settings (#3226).
    from ...security.executables import closed_exec_map, resolve_project_exec_allow_map

    # Trust is resolved only when survivors are rebuilt; nothing else consumes it.
    # The command resolved survivor trust before its first write (#4185).
    _allow_executables = (
        resolve_project_exec_allow_map(
            project_root,
            apm_package,
            user_scope=user_scope,
            executable_context=executable_context,
            logger=logger,
        )
        if target_survivor_plan
        else closed_exec_map(None)
    )
    preflight_survivor_hooks(target_survivor_plan, _allow_executables)

    # An empty lockfile inventory still authorizes no file removal. Only
    # installations without a lockfile may need legacy orphan detection.
    sync_managed = all_deployed_files if lockfile is not None or all_deployed_files else None
    if sync_managed is not None:
        # Partition against default KNOWN_TARGETS for legacy/project-scope
        # paths, then merge with resolved targets in both skill layouts for
        # user-scope paths and --legacy-skill-paths roots (.github/skills/).
        _buckets = BaseIntegrator.partition_managed_files(sync_managed)
        if _resolved_targets:
            _scope_buckets = BaseIntegrator.partition_managed_files(
                sync_managed, targets=_skill_cleanup_targets
            )
            for _bname, _bpaths in _scope_buckets.items():
                _existing = _buckets.get(_bname)
                if _existing is not None:
                    _existing.update(_bpaths)
                else:
                    _buckets[_bname] = _bpaths
    else:
        _buckets = None

    counts = {entry.counter_key: 0 for entry in _dispatch.values()}
    package_deployed_files: dict[str, list[str]] = {}

    # Phase 1: Remove all APM-deployed files
    # Per-target sync for primitives with sync_for_target
    for _target in _resolved_targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.sync_method != "sync_for_target":
                continue
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = project_root / _effective_root / _mapping.subdir
            # Dynamic-root targets (e.g. copilot-app) have no filesystem
            # deploy dir; their managed files are URIs that the integrator
            # resolves internally.  Skip the dir-exists guard for them.
            _is_dynamic = _target.resolved_deploy_root is not None
            if not _is_dynamic and not _deploy_dir.exists():
                continue
            _managed_subset = None
            if _buckets is not None:
                _bucket_key = BaseIntegrator.partition_bucket_key(_prim_name, _target.name)
                _managed_subset = _buckets.get(_bucket_key, set())
            result = _integrators[_prim_name].sync_for_target(
                _target,
                apm_package,
                project_root,
                managed_files=_managed_subset,
                evidence=evidence,
            )
            counts[_entry.counter_key] += result.get("files_removed", 0)

    # Skills (multi-target, handled by SkillIntegrator)
    # Check the target's effective skills root (deploy_root + skills subdir)
    _skill_dirs_exist = False
    for t in _skill_cleanup_targets:
        if t.supports("skills"):
            if (project_root / t.skills_rel_root).exists():
                _skill_dirs_exist = True
                break

    # Scan sync_managed DIRECTLY for cowork:// entries.
    # partition_managed_files() uses resolved_deploy_root to detect
    # dynamic-root targets, but the static KNOWN_TARGETS["copilot-cowork"]
    # always has resolved_deploy_root=None (it is only set after for_scope()
    # resolves the OneDrive path at install time).  As a result, cowork://
    # paths are never routed into _buckets["skills"] by the partition, so
    # the bucket-based _has_cowork_skills check in the previous fix always
    # returned False.  Bypassing the bucket and scanning sync_managed
    # directly is the correct approach: no partition logic is involved.
    _cowork_skill_files: set = set()
    if sync_managed:
        from ...integration.copilot_cowork_paths import COWORK_URI_SCHEME

        _cowork_skill_files = {p for p in sync_managed if p.startswith(COWORK_URI_SCHEME)}
    _has_cowork_skills = bool(_cowork_skill_files)

    if _skill_dirs_exist or _has_cowork_skills:
        # Merge cowork entries into the skills bucket so sync_integration
        # receives them via managed_files.
        if _has_cowork_skills and _buckets is not None:
            _buckets.setdefault("skills", set()).update(_cowork_skill_files)
        elif _has_cowork_skills:
            _buckets = {"skills": _cowork_skill_files, "hooks": set()}

        # When cowork entries are present, pass targets=None so
        # sync_integration builds skill_prefix_tuple from KNOWN_TARGETS
        # (which includes the copilot-cowork target with user_root_resolver
        # set).  Using _resolved_targets alone would yield only the local
        # prefix (.copilot/skills/) and cowork:// paths would be silently
        # skipped by the startswith() guard inside sync_integration.
        _sync_targets = None if _has_cowork_skills else _skill_cleanup_targets
        removed_skill_paths: list[Path] = []
        try:
            result = _integrators["skills"].sync_integration(
                apm_package,
                project_root,
                managed_files=_buckets["skills"] if _buckets else None,
                targets=_sync_targets,
                removed_paths=removed_skill_paths,
                evidence=evidence,
            )
        finally:
            DeploymentMaterializationSession.retire_removed(
                project_root, removed_skill_paths, user_scope=user_scope
            )
        counts["skills"] = result.get("files_removed", 0)

    # Scan sync_managed DIRECTLY for copilot-app-db:// entries.
    # The copilot-app target is opt-in: resolve_targets() excludes it from the
    # default user-scope set unless --target copilot-app was passed at install
    # time and recorded on the package's canonical target list. Without this scan, prompts
    # deployed to ~/.copilot/data.db would never be deleted on uninstall
    # because the per-target loop above does not iterate copilot-app.
    if sync_managed:
        from ...integration.copilot_app_db import COPILOT_APP_LOCKFILE_PREFIX

        _copilot_app_files = {p for p in sync_managed if p.startswith(COPILOT_APP_LOCKFILE_PREFIX)}
        if _copilot_app_files:
            # Find or synthesise a user-scope copilot-app TargetProfile.
            from ...integration.targets import KNOWN_TARGETS

            _ca_target = next(
                (t for t in _resolved_targets if t.name == "copilot-app"),
                None,
            )
            if _ca_target is None:
                _ca_static = KNOWN_TARGETS.get("copilot-app")
                if _ca_static is not None:
                    _ca_target = _ca_static.for_scope(user_scope=True)
            if _ca_target is not None:
                result = _integrators["prompts"].sync_for_target(
                    _ca_target,
                    apm_package,
                    project_root,
                    managed_files=_copilot_app_files,
                )
                counts["prompts"] += result.get("files_removed", 0)

    # Hooks: managed-file removal always scans every KNOWN_TARGETS prefix
    # (safe -- it only ever deletes files already tracked as this
    # package's own deployed_files). The merged-hook JSON wipe, in
    # contrast, is scoped to `_resolved_targets` -- the SAME set the
    # Phase 2 rebuild loop below iterates -- so a harness dropped from
    # this project's `targets:` list is never wiped for packages that
    # remain declared and installed (#2250).
    result = _integrators["hooks"].sync_integration(
        apm_package,
        project_root,
        managed_files=_buckets["hooks"] if _buckets else None,
        managed_file_hashes=deployed_file_hashes,
        targets=_resolved_targets,
        evidence=evidence,
    )
    counts["hooks"] = result.get("files_removed", 0)
    unsafe_hook_paths = result.get("unsafe_paths", [])
    if unsafe_hook_paths:
        noun = "path" if len(unsafe_hook_paths) == 1 else "paths"
        logger.warning(
            f"Skipped {len(unsafe_hook_paths)} managed hook {noun} that failed "
            "containment validation. Inspect or repair symlinked parents before "
            "removing anything."
        )
    failed_hook_paths = sorted(set(result.get("failed_paths", [])).union(unsafe_hook_paths))
    for failed_path in failed_hook_paths:
        path = Path(failed_path)
        if not path.is_absolute():
            path = project_root / path
        if user_scope:
            try:
                display_path = f"~/{path.relative_to(project_root).as_posix()}"
            except ValueError:
                display_path = path.as_posix()
        else:
            display_path = path.as_posix()
        logger.warning(f"Preserved managed hook path: {display_path}")

    # Phase 2: Re-integrate from remaining installed packages.
    #
    # Route every primitive through the install service so its post-authorization
    # DeployableSourcePlan and pre-deploy SecurityGate scan protect this lifecycle
    # path too.  Calling individual integrators here would create a second,
    # unscanned materialization path after the cleanup pass.
    # Re-clear the discovery memo: Phase 1 mutated the on-disk primitive
    # set (removed files), so any cache snapshot taken between entry and
    # here is stale. Integrator dispatch below walks discovery internally.
    clear_discovery_cache()
    _targets = _resolved_targets

    # The complete survivor plan was validated before Phase 1 mutated disk.
    from ...core.scope import InstallScope
    from ...utils.diagnostics import DiagnosticCollector

    _rebuild_scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    reintegration_diagnostics = DiagnosticCollector()
    materialization_session = (
        DeploymentMaterializationSession(project_root, _rebuild_scope, lockfile)
        if target_survivor_plan
        else None
    )
    with _integrators["skills"].ownership_snapshot(materialization_session=materialization_session):
        for dep_ref, pkg_info, authorized_targets in target_survivor_plan:
            dep_key = dep_ref.get_unique_key()
            deployed_files = package_deployed_files.setdefault(dep_key, [])
            # Survivors redeploy exactly the selection install deployed (#3760).
            selection = effective_dependency_selection(dep_ref)

            try:
                integration_result = integrate_package_primitives(
                    pkg_info,
                    project_root,
                    targets=authorized_targets,
                    integrators=_integrator_bundle,
                    force=False,
                    managed_files=None,
                    diagnostics=reintegration_diagnostics,
                    package_name=dep_ref.get_identity(),
                    logger=_SurvivorRebuildLogger(logger),
                    scope=_rebuild_scope,
                    skill_subset=selection.skill_subset,
                    skill_pairing=selection.skill_pairing,
                    allow_executables=_allow_executables,
                    trust_bin=False,
                    bin_skip_reason_override="not_retrusted_on_uninstall",
                )
                deployed_files.extend(integration_result["deployed_files"])
            except Exception as exc:
                pkg_id = _dependency_public_label(dep_ref)
                logger.warning(
                    f"Best-effort re-integration skipped for {pkg_id}. "
                    f"{INTEGRATED_FILES_STALE.sentence()}"
                )
                logger.verbose_detail(
                    f"    Re-integration error: {_reintegration_error_detail(dep_ref, exc)}"
                )

    reintegration_diagnostics.render_summary()
    return IntegrationCleanupOutcome(
        counts=counts,
        deployed_files=package_deployed_files,
        failed_paths=failed_hook_paths,
        error_count=result.get("errors", 0),
        materialization_session=materialization_session,
    )


def _restage_changed_local_survivors(
    lockfile: LockFile,
    modules_dir: Path,
    source_root: Path,
    skip_keys: set[str],
    logger: CommandLogger,
) -> set[str]:
    """Re-copy each direct local survivor whose source changed since the lock (#4446).

    Install re-copies local packages on every run; without this, uninstall
    judges and rebuilds a survivor from its stale ``apm_modules`` copy, so a
    hook whose local definition changed since approval is never re-judged.
    Returns the restaged lock keys; their lock rows record the new content hash.
    """
    from ...install.phases.local_content import _copy_local_package, compute_local_package_hash

    restaged: set[str] = set()
    for key, dep in sorted(lockfile.dependencies.items()):
        if key == "." or key in skip_keys or dep.source != "local" or dep.depth != 1:
            continue
        try:
            dep_ref = dep.to_dependency_ref()
            source = (source_root / dep_ref.local_path).resolve()
            install_path = dep_ref.get_install_path(modules_dir)
            current = compute_local_package_hash(source, framed=dep.content_hash_framed)
        except (OSError, ValueError, TypeError, PathTraversalError):
            continue
        # Judge the source against the materialized copy, not the lock row: a
        # native plugin's locked hash is recorded from its normalized image.
        if not install_path.is_dir() or current == compute_local_package_hash(
            install_path, framed=dep.content_hash_framed
        ):
            continue
        staged_path = modules_dir / "_local" / f".apm-uninstall-refresh-{uuid4().hex}"
        refresh_logger = _LocalRefreshLogger(logger, _dependency_public_label(dep_ref))
        staged = _copy_local_package(
            dep_ref, staged_path, source_root, project_root=source_root, logger=refresh_logger
        )
        if staged is None:
            if staged_path.exists():
                safe_rmtree(staged_path, modules_dir)
            continue
        _activate_staged_local_refresh(
            LocalSlotRefresh(
                install_path=install_path, staged_path=staged_path, survivor_keys=(key,)
            ),
            modules_dir,
        )
        dep.content_hash = current
        restaged.add(key)
        logger.progress(
            f"Refreshed local package {_dependency_public_label(dep_ref)} from its source"
        )
    return restaged


def gate_retired_hook_survivors(
    lockfile: "LockFile | None",
    modules_dir: Path,
    manifest_path: Path,
    deploy_root: Path,
    removed_keys: set[str],
    refreshed: set[str],
    *,
    user_scope: bool,
    logger: CommandLogger,
) -> tuple[set[str], set[str]]:
    """Add survivors whose deployed hooks the current gates no longer allow (#4446).

    Returns ``(refresh, exclude)``: *refreshed* plus the survivors to redeploy
    in full, so the rebuild under install's executables gate leaves their
    retired hooks out, and the subset the org policy denies outright, which
    keeps nothing, as the MCP cleanup does for its servers. The verdict is
    the gate retirement owner's
    (:func:`~apm_cli.install.exec_gate.gate_retirements`).
    """
    from ...install.exec_gate import gate_retirements
    from ...models.apm_package import APMPackage
    from .local_survivors import survivor_ships_hooks

    if lockfile is None:
        return refreshed, set()
    refreshed = refreshed | _restage_changed_local_survivors(
        lockfile, modules_dir, manifest_path.parent, removed_keys, logger
    )
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        retirements = gate_retirements(
            lockfile, modules_dir, apm_package, deploy_root, user_scope=user_scope
        )
    except Exception as exc:
        # Integration cleanup reports an unreadable manifest; nothing to judge here.
        logger.verbose_detail(f"Gate retirement skipped: {type(exc).__name__}: {exc}")
        return refreshed, set()
    if retirements is None:
        return refreshed, set()
    # Only survivors: the removed packages' files are already being cleaned. A
    # denied survivor without hook files keeps its plain files; the MCP and
    # LSP cleanups retire its servers.
    denied = {
        k: why
        for k, why in retirements.denied_packages.items()
        if k not in removed_keys and survivor_ships_hooks(lockfile, k, modules_dir)
    }
    hooks = {k: why for k, why in retirements.hook_packages.items() if k not in removed_keys}
    for key, why in sorted(denied.items()):
        logger.warning(f"{key} is no longer allowed ({why}); removing its integrated files")
    for key, why in sorted(hooks.items()):
        if key not in denied:
            logger.warning(f"{key} hooks are no longer allowed ({why}); removing them")
    return refreshed | set(denied) | set(hooks), set(denied)


def _remove_stale_mcp_from_recorded_targets(
    stale_servers: set[str],
    lockfile: LockFile,
    *,
    project_root: Path | None,
    user_scope: bool,
    scope: object | None,
    target_servers: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """Clean only runtimes recorded in the deployment ledger, without fail-fast."""
    if target_servers is None:
        from ...install.mcp.ownership import resolve_mcp_target_servers

        target_servers = resolve_mcp_target_servers(
            recorded_target_servers={
                runtime: builtins.set(servers)
                for runtime, servers in (lockfile.mcp_target_servers or {}).items()
            },
            ownership_present=lockfile._mcp_target_servers_present,
            server_names=stale_servers,
            stored_configs=lockfile.mcp_configs,
            project_root=project_root,
            user_scope=user_scope,
        )

    failures: list[tuple[str, Exception]] = []
    from ...core.deployment_ledger import DeploymentLedgerCodec

    native_hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
    for runtime, managed_servers in sorted(target_servers.items()):
        scoped_stale = stale_servers.intersection(managed_servers)
        if not scoped_stale:
            continue
        try:
            MCPIntegrator.remove_stale(
                scoped_stale,
                runtime=runtime,
                project_root=project_root,
                user_scope=user_scope,
                scope=scope,
                fail_on_write_error=True,
                stored_configs=lockfile.mcp_configs,
                native_hashes=native_hashes,
            )
        except Exception as exc:
            failures.append((runtime, exc))
    if failures:
        raise MCPUninstallCleanupError(failures) from failures[0][1]
    return target_servers


def _cleanup_stale_mcp(
    apm_package,
    lockfile,
    lockfile_path,
    old_mcp_servers,
    modules_dir=None,
    project_root=None,
    user_scope: bool = False,
    scope=None,
    persist: bool = True,
    logger=None,
):
    """Remove MCP servers that are no longer needed after uninstall."""
    managed_servers = set(old_mcp_servers or ())
    target_ownership = {}
    if lockfile is not None:
        managed_servers.update(getattr(lockfile, "mcp_servers", ()) or ())
        target_ownership = getattr(lockfile, "mcp_target_servers", {}) or {}
        if isinstance(target_ownership, dict):
            for servers in target_ownership.values():
                managed_servers.update(servers or ())
        else:
            target_ownership = {}
    if not managed_servers:
        return
    from apm_cli.install.exec_gate import gate_retirements
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.security.trust import McpAdmission

    apm_modules_path = modules_dir if modules_dir is not None else Path.cwd() / APM_MODULES_DIR
    deploy_root = Path(project_root) if project_root is not None else Path.cwd()
    # A survivor the current gates no longer allow loses its servers, as on
    # install (#4446): install's own MCP admission parks a denied, revoked or
    # changed server, and a dependency the org policy denies keeps none.
    retirements = gate_retirements(
        lockfile, apm_modules_path, apm_package, deploy_root, user_scope=user_scope
    )
    denied = retirements.denied_packages if retirements is not None else {}
    for key, why in sorted(denied.items()):
        if logger is not None:
            logger.warning(f"{key} is no longer allowed ({why}); removing its MCP servers")
    view = CurrentMcpConfigView.derive(
        apm_package,
        lockfile,
        apm_modules_path,
        admission=McpAdmission.for_project(deploy_root, user_scope=user_scope),
        logger=logger,
        active_package_keys=(
            frozenset(key for key in lockfile.dependencies if key not in denied) if denied else None
        ),
    )
    # Uninstall never admits a server: only previously managed servers survive.
    # Local-bundle servers stay with their bundle, not the uninstalled package.
    new_mcp_servers = (
        MCPIntegrator.get_server_names(view.dependencies) | view.bundle_owned
    ) & managed_servers
    stale_servers = managed_servers - new_mcp_servers
    from ...install.mcp.ownership import resolve_mcp_target_servers

    target_servers = resolve_mcp_target_servers(
        recorded_target_servers={
            runtime: builtins.set(servers)
            for runtime, servers in (lockfile.mcp_target_servers or {}).items()
        },
        ownership_present=lockfile._mcp_target_servers_present,
        server_names=managed_servers,
        stored_configs=lockfile.mcp_configs,
        project_root=project_root,
        user_scope=user_scope,
    )
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    from ...install.mcp.retired_destinations import (
        heal_retired_project_mcp_destinations,
        report_retired_mcp_heal,
        restore_pending_retired_claims,
        split_mcp_claims,
    )

    # Claims that name the retired Copilot MCP files are never live .mcp.json
    # ownership: a stale one is healed by proof, the rest are carried forward.
    deploy_root = Path(project_root) if project_root is not None else Path.cwd()
    native_hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
    retired = split_mcp_claims(
        target_servers,
        native_hashes,
        apm_version=lockfile.apm_version,
        project_root=deploy_root,
        user_scope=user_scope,
        stored_configs=lockfile.mcp_configs,
    )
    stale_retired = {runtime: names & stale_servers for runtime, names in retired.names.items()}
    retained_unowned: set[str] = set()
    if stale_servers:
        _remove_stale_mcp_from_recorded_targets(
            stale_servers,
            lockfile,
            project_root=project_root,
            user_scope=user_scope,
            scope=scope,
            target_servers=target_servers,
        )
        # Report the heal like install does (C5d, #3967): removed servers,
        # kept user-edited ones, and deleted files.
        report_retired_mcp_heal(
            heal_retired_project_mcp_destinations(
                stale_retired,
                stored_configs=lockfile.mcp_configs,
                native_hashes=retired.hashes,
                project_root=deploy_root,
                keep_signal_files=False,
            ),
            logger,
        )
        owned_servers = {
            server
            for claims in (*target_servers.values(), *retired.names.values())
            for server in claims
        }
        retained_unowned = stale_servers - owned_servers
    contracted_claims = {
        runtime: servers.intersection(new_mcp_servers)
        for runtime, servers in target_servers.items()
    }
    restore_pending_retired_claims(
        retired,
        stale_retired,
        target_servers=contracted_claims,
        native_hashes=native_hashes,
        project_root=deploy_root,
        stored_configs=lockfile.mcp_configs,
    )
    contracted_target_servers = {
        runtime: sorted(servers) for runtime, servers in contracted_claims.items() if servers
    }
    contracted_hashes = native_hashes if retired.names else None
    surviving_mcp_servers = new_mcp_servers | retained_unowned
    surviving_configs = {
        name: config for name, config in view.configs.items() if name in surviving_mcp_servers
    }
    surviving_provenance = {
        name: source for name, source in view.provenance.items() if name in surviving_mcp_servers
    }
    if persist:
        MCPIntegrator.update_lockfile(
            surviving_mcp_servers,
            lockfile_path,
            mcp_configs=surviving_configs,
            mcp_config_provenance=surviving_provenance,
            mcp_target_servers=contracted_target_servers,
            native_hashes=contracted_hashes,
        )
        return

    lockfile.mcp_servers = sorted(surviving_mcp_servers)
    lockfile.mcp_configs = surviving_configs
    lockfile.mcp_config_provenance = surviving_provenance
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lockfile,
        contracted_target_servers,
        native_hashes=contracted_hashes,
    )
