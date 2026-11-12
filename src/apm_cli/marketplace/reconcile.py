"""Reconcile the user-manifest ``marketplaces:`` list into the registry.

``apm install -g`` calls :func:`reconcile_user_marketplaces` before it
resolves dependencies, so ``plugin@marketplace`` references in the user
manifest can use marketplaces declared next to them.

Rules:

* Missing entries are registered and changed entries are updated, both
  with ``origin: manifest``.
* Manifest-origin entries that are no longer declared are removed.
* Manual registrations (``apm marketplace add``) are never overwritten or
  removed; a declared entry with the same name is reported as a conflict.

Registering a marketplace enables discovery only. It never installs a
package or grants trust.
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ManualMarketplaceConflictError, MarketplaceError
from .models import ORIGIN_MANIFEST, MarketplaceSource
from .registration import ParsedSource, parse_registration_source, probe_registration
from .user_manifest import DeclaredMarketplace, parse_declared_marketplaces

if TYPE_CHECKING:
    from ..core.command_logger import CommandLogger


@dataclass
class MarketplaceReconcileReport:
    """What a reconcile run registered, updated, removed, kept or refused."""

    registered: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


_VERBS = {"registered": "register", "updated": "update"}


def _same_location(existing: MarketplaceSource, parsed: ParsedSource) -> bool:
    if existing.url != parsed.url:
        return False
    # Direct marketplace.json URLs carry no ref; the stored value is a default.
    return parsed.is_direct_url or existing.ref == parsed.ref


def _register(
    item: DeclaredMarketplace,
    parsed: ParsedSource,
    *,
    action: str,
    report: MarketplaceReconcileReport,
    logger: CommandLogger,
) -> None:
    from .registry import register_manifest_marketplace

    try:
        source, manifest = probe_registration(
            item.name, parsed.url, parsed.ref, is_direct_url=parsed.is_direct_url
        )
        register_manifest_marketplace(dataclasses.replace(source, origin=ORIGIN_MANIFEST))
    except ManualMarketplaceConflictError:
        _report_conflict(item.name, report=report, logger=logger)
        return
    except (MarketplaceError, ValueError, OSError) as exc:
        report.failed.append((item.name, str(exc)))
        logger.error(f"Could not {_VERBS[action]} marketplace '{item.name}': {exc}")
        return
    getattr(report, action).append(item.name)
    count = len(manifest.plugins)
    noun = "plugin" if count == 1 else "plugins"
    logger.success(
        f"{action.capitalize()} marketplace '{item.name}' ({count} {noun})", symbol="check"
    )


def _report_conflict(name: str, *, report: MarketplaceReconcileReport, logger: CommandLogger):
    report.conflicts.append(name)
    logger.warning(
        f"Kept manual marketplace '{name}': it was added with 'apm marketplace add', "
        "so the user manifest entry was not applied. To let the user manifest manage it, "
        f"run 'apm marketplace remove {name}' and re-run 'apm install -g'."
    )


def reconcile_user_marketplaces(
    manifest_path: Path,
    *,
    logger: CommandLogger,
    dry_run: bool = False,
) -> MarketplaceReconcileReport:
    """Bring the registry in line with the user manifest ``marketplaces:`` list.

    Raises:
        ValueError: The ``marketplaces:`` declaration is malformed.
    """
    from ..utils.yaml_io import load_yaml
    from .registry import get_registered_marketplaces, unregister_manifest_marketplace

    data = load_yaml(manifest_path) if manifest_path.exists() else None
    declared = parse_declared_marketplaces(data if isinstance(data, dict) else {}, manifest_path)
    registered = {s.name.lower(): s for s in get_registered_marketplaces()}
    owned = [s for s in registered.values() if s.origin == ORIGIN_MANIFEST]
    report = MarketplaceReconcileReport()
    if not declared and not owned:
        return report

    logger.start(f"Reconciling marketplaces from {manifest_path.as_posix()}", symbol="gear")
    for item in declared:
        parsed = parse_registration_source(item.source, ref=item.ref)
        existing = registered.get(item.name.lower())
        if existing is not None and existing.origin != ORIGIN_MANIFEST:
            _report_conflict(item.name, report=report, logger=logger)
            continue
        if existing is not None and _same_location(existing, parsed):
            report.kept.append(item.name)
            continue
        action = "updated" if existing is not None else "registered"
        if dry_run:
            getattr(report, action).append(item.name)
            logger.dry_run_notice(f"Would {_VERBS[action]} marketplace '{item.name}'")
            continue
        _register(item, parsed, action=action, report=report, logger=logger)

    declared_names = {item.name.lower() for item in declared}
    for stale in owned:
        if stale.name.lower() in declared_names:
            continue
        if dry_run:
            report.removed.append(stale.name)
            logger.dry_run_notice(f"Would remove marketplace '{stale.name}'")
        elif unregister_manifest_marketplace(stale.name):
            report.removed.append(stale.name)
            logger.success(
                f"Removed marketplace '{stale.name}' (no longer in the user manifest)",
                symbol="check",
            )

    if report.kept:
        logger.progress(f"Kept marketplace(s) unchanged: {', '.join(report.kept)}")
    return report


def reconcile_user_marketplaces_for_install(
    manifest_path: Path, *, logger: CommandLogger, dry_run: bool
) -> bool:
    """Run the reconcile for ``apm install -g``; return False when it failed.

    Failures are rendered here so the command only decides the exit code.
    """
    try:
        report = reconcile_user_marketplaces(manifest_path, logger=logger, dry_run=dry_run)
    except ValueError as exc:
        logger.error(str(exc))
        return False
    if report.ok:
        return True
    names = ", ".join(name for name, _ in report.failed)
    logger.error(
        f"Marketplace reconcile failed for: {names}. Fix the source of each entry "
        f"in {manifest_path.as_posix()} or remove it, then re-run 'apm install -g'."
    )
    return False


def reconcile_user_marketplaces_or_exit(
    manifest_path: Path, *, user_scope: bool, logger: CommandLogger, dry_run: bool
) -> None:
    """``apm install`` hook: reconcile in user scope, exit 1 when it failed."""
    if user_scope and not reconcile_user_marketplaces_for_install(
        manifest_path, logger=logger, dry_run=dry_run
    ):
        sys.exit(1)
