"""Copilot's side of APM's native plugins: legacy registration migration.

Agent Plugins now take the shared native route (#3921): each admitted plugin
is deployed to ``.agents/plugins/<plugin>/`` (``~/.agents/plugins`` at user
scope), recorded as ``native_plugin`` in ``apm.lock.yaml``, and declared in
Copilot's settings by :mod:`apm_cli.native_plugins.reconcile`.

The earlier route kept its own catalog and ``apm-registration.json`` ledger
under ``apm_modules/.github/plugin/`` and declared the ``apm`` marketplace,
at project scope in the uncommitted ``.github/copilot/settings.local.json``.
:func:`synchronize_copilot_plugins` retires that registration once: it removes
only the entries the ledger proves APM wrote, then the ledger and catalog.
Install, uninstall and prune all call it before the native reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.scope import InstallScope
from ..utils.atomic_io import atomic_write_text
from .constants import (
    APM_MARKETPLACE_NAME,
    MARKETPLACE_MANIFEST_RELATIVE,
    REGISTRATION_LEDGER_RELATIVE,
)
from .settings import (
    read_ledger,
    read_settings_document,
    remove_registration,
    render_settings_document,
    resolve_settings_path,
)


@dataclass(frozen=True, slots=True)
class ResolvedPluginCandidate:
    """One resolved dependency whose bytes may hold an Agent Plugin."""

    dependency_key: str
    install_path: Path
    direct: bool = False
    target_subset: tuple[str, ...] | None = None
    exec_status: str | None = None


def catalog_path_for(modules_dir: Path) -> Path:
    """Return the legacy APM-owned catalog path for one materialization root."""
    return modules_dir / MARKETPLACE_MANIFEST_RELATIVE


def ledger_path_for(modules_dir: Path) -> Path:
    """Return the legacy APM ownership ledger path for one materialization root."""
    return modules_dir / REGISTRATION_LEDGER_RELATIVE


def registration_status(modules_dir: Path) -> tuple[str, ...]:
    """Return the Agent Plugins the lock declares natively to Copilot."""
    from ..deps.lockfile import LockFile, get_lockfile_path

    lockfile = LockFile.read(get_lockfile_path(Path(modules_dir).parent))
    if lockfile is None:
        return ()
    return tuple(
        sorted(
            locked.native_plugin.name
            for locked in lockfile.dependencies.values()
            if locked.native_plugin is not None and locked.package_type == "agent_plugin"
        )
    )


def candidates_from_lockfile(lockfile: Any, modules_dir: Path) -> list[ResolvedPluginCandidate]:
    """Return registration candidates from canonical locked state.

    The synthesized self-entry (dependency key ``.``) is never a registration
    candidate; every other consumer of the dependency map filters it too.
    Direct dependencies (no declaring parent, not resolved by a parent repo)
    and any per-dependency target subset flow through so precedence and target
    narrowing decisions have the same inputs as the install path.

    This is the ONE executable trust gate shared by install and every lifecycle
    command (uninstall, prune, restore): a locked entry whose executables the
    scanner denied or left pending approval (``exec_status`` of
    :data:`~apm_cli.security.executables.TRUST_DENIED` /
    :data:`~apm_cli.security.executables.TRUST_GATED`) is dropped here, so a
    resync can never silently enable an MCP server the install refused. The
    in-flight ``deps_to_install`` overlay is gated separately by the install
    phase, since its exec status is not yet persisted to the lockfile.
    """
    from ..deps.lockfile import _SELF_KEY
    from ..security.executables import REGISTRABLE_EXEC_STATUSES

    candidates: list[ResolvedPluginCandidate] = []
    for dep_key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if dep_key == _SELF_KEY:
            continue
        exec_status = getattr(locked, "exec_status", None)
        # Allowlist, not denylist: a status APM does not recognise is never
        # registrable, matching the severity ladder that ranks unknown above
        # `denied`.
        if exec_status not in REGISTRABLE_EXEC_STATUSES:
            continue
        to_reference = getattr(locked, "to_dependency_ref", None)
        if to_reference is None:
            continue
        try:
            install_path = to_reference().get_install_path(modules_dir)
        except (ValueError, AttributeError):
            continue
        target_subset = getattr(locked, "target_subset", None)
        direct = (
            getattr(locked, "declaring_parent", None) is None
            and getattr(locked, "resolved_by", None) is None
        )
        candidates.append(
            ResolvedPluginCandidate(
                dependency_key=dep_key,
                install_path=Path(install_path),
                direct=direct,
                target_subset=tuple(target_subset) if target_subset else None,
                exec_status=exec_status,
            )
        )
    return candidates


def resync_native_plugins(
    *,
    project_root: Path,
    modules_dir: Path,
    scope: InstallScope | Any,
    lockfile: Any | None = None,
    logger: Any | None = None,
    dry_run: bool = False,
) -> bool:
    """Retire a legacy registration during uninstall and prune (see module doc)."""
    del lockfile  # ownership now lives in the native_plugin lock records
    return synchronize_copilot_plugins(
        project_root=project_root,
        modules_dir=modules_dir,
        scope=scope,
        logger=logger,
        dry_run=dry_run,
    )


def synchronize_copilot_plugins(
    *,
    project_root: Path,
    modules_dir: Path,
    scope: InstallScope | Any,
    logger: Any | None = None,
    dry_run: bool = False,
) -> bool:
    """Remove the legacy ``apm`` registration its ledger proves, then the ledger.

    Returns whether a legacy registration was found. Entries the ledger does
    not name, and a marketplace it does not own, are left alone. The plugins
    themselves are re-declared by the native reconcile from the lock.
    """
    modules_dir = Path(modules_dir)
    catalog_path = catalog_path_for(modules_dir)
    ledger_path = ledger_path_for(modules_dir)
    if not ledger_path.is_file():
        return False
    ledger = _removal_ledger(read_ledger(ledger_path), catalog_path)
    settings_path = resolve_settings_path(scope, project_root, ledger)
    if dry_run:
        if logger is not None:
            logger.dry_run_notice(
                f"Would move the APM Copilot plugin registration out of {settings_path}"
            )
        return True
    document = read_settings_document(settings_path)
    removal = remove_registration(document=document, settings_path=settings_path, ledger=ledger)
    if removal.changed and settings_path.is_file():
        if removal.document:
            atomic_write_text(settings_path, render_settings_document(removal.document))
        else:
            settings_path.unlink()
    for generated in (catalog_path, ledger_path):
        generated.unlink(missing_ok=True)
    _prune_empty_parents(catalog_path.parent, stop=modules_dir)
    if logger is not None:
        logger.info(
            f"Moved APM's Copilot plugin registration out of {settings_path}; Agent "
            "Plugins are now declared from .agents/plugins.",
            symbol="info",
        )
    return True


def _removal_ledger(ledger: Any, catalog_path: Path) -> Any:
    """Return the ledger to drive removal, healing an unreadable ledger.

    A ledger that exists but cannot be parsed would otherwise remove nothing
    (fail-OPEN) while the catalog is still deleted, leaving orphaned
    ``enabledPlugins`` entries. When that happens, the still-present catalog is
    the authoritative record of what APM owns, so removal keys are derived from
    it instead.
    """
    if not getattr(ledger, "unreadable", False):
        return ledger
    from .settings import RegistrationLedger

    return RegistrationLedger(
        marketplace_owned=True,
        enabled_plugins=_catalog_derived_enabled_keys(catalog_path),
    )


def _catalog_derived_enabled_keys(catalog_path: Path) -> tuple[str, ...]:
    """Return ``<plugin>@apm`` keys derived from the on-disk catalog."""
    import json

    if not catalog_path.is_file():
        return ()
    try:
        loaded = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    plugins = loaded.get("plugins") if isinstance(loaded, dict) else None
    if not isinstance(plugins, list):
        return ()
    keys = []
    for plugin in plugins:
        if isinstance(plugin, dict) and isinstance(plugin.get("name"), str):
            keys.append(f"{plugin['name']}@{APM_MARKETPLACE_NAME}")
    return tuple(sorted(set(keys)))


def _prune_empty_parents(start: Path, *, stop: Path) -> None:
    """Remove APM-generated catalog directories once they are empty."""
    current = start
    while current != stop and current.is_dir():
        try:
            next(current.iterdir())
            return
        except StopIteration:
            pass
        except OSError:  # pragma: no cover - defensive
            return
        try:
            current.rmdir()
        except OSError:  # pragma: no cover - defensive
            return
        current = current.parent
