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

from .errors import MarketplaceError
from .models import ORIGIN_MANIFEST, MarketplaceSource
from .registration import ParsedSource, parse_registration_source, probe_registration
from .user_manifest import DeclaredMarketplace, parse_declared_marketplaces

if TYPE_CHECKING:
    from ..core.command_logger import CommandLogger
    from ..core.scope import InstallScope


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


@dataclass
class _Pending:
    """A declared entry whose manifest was fetched and awaits the registry write."""

    name: str
    action: str
    source: MarketplaceSource
    plugin_count: int


def _probe(
    item: DeclaredMarketplace,
    parsed: ParsedSource,
    *,
    action: str,
    report: MarketplaceReconcileReport,
    logger: CommandLogger,
) -> _Pending | None:
    try:
        source, manifest = probe_registration(
            item.name, parsed.url, parsed.ref, is_direct_url=parsed.is_direct_url
        )
    except (MarketplaceError, ValueError, OSError) as exc:
        report.failed.append((item.name, str(exc)))
        logger.error(f"Could not {_VERBS[action]} marketplace '{item.name}': {exc}")
        return None
    return _Pending(
        name=item.name,
        action=action,
        source=dataclasses.replace(source, origin=ORIGIN_MANIFEST),
        plugin_count=len(manifest.plugins),
    )


def _apply(
    pending: list[_Pending],
    removals: list[str],
    *,
    report: MarketplaceReconcileReport,
    logger: CommandLogger,
) -> None:
    """Write every fetched entry and removal in one registry transaction."""
    from .registry import apply_manifest_marketplaces

    applied, conflicts, removed = apply_manifest_marketplaces([p.source for p in pending], removals)
    applied_set = set(applied)
    for item in pending:
        if item.name not in applied_set:
            continue
        getattr(report, item.action).append(item.name)
        noun = "plugin" if item.plugin_count == 1 else "plugins"
        logger.success(
            f"{item.action.capitalize()} marketplace '{item.name}' ({item.plugin_count} {noun})",
            symbol="check",
        )
    for name in conflicts:
        _report_conflict(name, report=report, logger=logger)
    for name in removed:
        report.removed.append(name)
        logger.success(
            f"Removed marketplace '{name}' (no longer in the user manifest)", symbol="remove"
        )


def _report_conflict(
    name: str,
    *,
    report: MarketplaceReconcileReport,
    logger: CommandLogger,
    existing: MarketplaceSource | None = None,
):
    report.conflicts.append(name)
    current = ""
    if existing is not None:
        from ._git_utils import redact_token

        shown = redact_token(existing.url) if existing.url else existing.display_source
        current = f" (currently {shown})"
    logger.warning(
        f"Kept manual marketplace '{name}'{current}: it was added with 'apm marketplace add', "
        "so the user manifest entry was not applied. To let the user manifest manage it, "
        f"run 'apm marketplace remove {name}' and re-run 'apm install -g'."
    )


def _load_user_manifest(manifest_path: Path) -> dict:
    """Return the user manifest mapping; a missing file is an empty manifest.

    Raises:
        ValueError: The file is not valid YAML or its root is not a mapping,
            so a malformed manifest can never read as "no marketplaces".
    """
    import yaml

    from ..utils.yaml_io import load_yaml

    if not manifest_path.exists():
        return {}
    display = manifest_path.as_posix()
    try:
        data = load_yaml(manifest_path)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML format in {display}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid user manifest {display}: expected a YAML mapping at the top level, "
            f"got {type(data).__name__}. Marketplaces were not reconciled."
        )
    return data


def reconcile_user_marketplaces(
    manifest_path: Path,
    *,
    logger: CommandLogger,
    dry_run: bool = False,
) -> MarketplaceReconcileReport:
    """Bring the registry in line with the user manifest ``marketplaces:`` list.

    Nothing is removed while any declared entry failed to register, and a
    run that changes nothing (including any ``dry_run``) creates no files.

    Raises:
        ValueError: The user manifest or its ``marketplaces:`` declaration
            is malformed. Raised before the registry is read or changed.
    """
    from .registry import read_registered_marketplaces

    declared = parse_declared_marketplaces(_load_user_manifest(manifest_path), manifest_path)
    registered = {s.name.lower(): s for s in read_registered_marketplaces()}
    owned = [s for s in registered.values() if s.origin == ORIGIN_MANIFEST]
    report = MarketplaceReconcileReport()
    if not declared and not owned:
        return report

    logger.start(f"Reconciling marketplaces from {manifest_path.as_posix()}", symbol="gear")
    pending: list[_Pending] = []
    for item in declared:
        parsed = parse_registration_source(item.source, ref=item.ref)
        existing = registered.get(item.name.lower())
        if existing is not None and existing.origin != ORIGIN_MANIFEST:
            _report_conflict(item.name, report=report, logger=logger, existing=existing)
            continue
        if existing is not None and _same_location(existing, parsed):
            report.kept.append(item.name)
            continue
        action = "updated" if existing is not None else "registered"
        if dry_run:
            getattr(report, action).append(item.name)
            logger.dry_run_notice(f"Would {_VERBS[action]} marketplace '{item.name}'")
            continue
        fetched = _probe(item, parsed, action=action, report=report, logger=logger)
        if fetched is not None:
            pending.append(fetched)

    declared_names = {item.name.lower() for item in declared}
    # A failed fetch may be the replacement for a stale entry (rename or
    # source move); keep every existing registration until all succeed.
    stale = [] if report.failed else [s.name for s in owned]
    removals = [name for name in stale if name.lower() not in declared_names]
    if dry_run:
        for name in removals:
            report.removed.append(name)
            logger.dry_run_notice(f"Would remove marketplace '{name}'")
    elif pending or removals:
        _apply(pending, removals, report=report, logger=logger)

    if report.kept:
        noun = "marketplace" if len(report.kept) == 1 else "marketplaces"
        logger.progress(f"Kept {noun} unchanged: {', '.join(report.kept)}")
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
    names = [name for name, _ in report.failed]
    target = f"'{names[0]}'" if len(names) == 1 else "each failed entry"
    logger.error(
        f"Marketplace reconcile failed for: {', '.join(names)}. Fix the source of {target} "
        f"in {manifest_path.as_posix()} or remove it, then re-run 'apm install -g'."
    )
    return False


def reconcile_user_marketplaces_or_exit(
    manifest_path: Path, scope: InstallScope, *, logger: CommandLogger, dry_run: bool
) -> None:
    """``apm install`` hook: reconcile in user scope, exit 1 when it failed."""
    from ..core.scope import InstallScope

    if scope is InstallScope.USER and not reconcile_user_marketplaces_for_install(
        manifest_path, logger=logger, dry_run=dry_run
    ):
        sys.exit(1)
