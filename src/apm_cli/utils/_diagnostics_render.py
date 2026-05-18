"""Diagnostic rendering helpers extracted from ``DiagnosticCollector``.

Extracted from ``utils.diagnostics`` to keep that module under 400 LOC.
All public functions take ``collector`` (a ``DiagnosticCollector`` instance)
as their first argument; they are called only from the corresponding
delegate one-liners on the class and should not be imported directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click


def _rich_echo(*args, **kwargs):
    from .diagnostics import _rich_echo as diagnostics_rich_echo

    return diagnostics_rich_echo(*args, **kwargs)


def _rich_info(*args, **kwargs):
    from .diagnostics import _rich_info as diagnostics_rich_info

    return diagnostics_rich_info(*args, **kwargs)


def _rich_warning(*args, **kwargs):
    from .diagnostics import _rich_warning as diagnostics_rich_warning

    return diagnostics_rich_warning(*args, **kwargs)


if TYPE_CHECKING:
    from .diagnostics import Diagnostic, DiagnosticCollector

# Mirrors ``_CATEGORY_ORDER`` in ``diagnostics.py`` -- must stay in sync.
_CATEGORY_ORDER = [
    "security",
    "policy",
    "auth",
    "drift",
    "collision",
    "overwrite",
    "warning",
    "error",
    "info",
]


def _group_by_package(items: list[Diagnostic]) -> dict[str, list[Diagnostic]]:
    """Group diagnostics by package, preserving insertion order.

    Items with an empty package key are collected under ``""``.
    """
    groups: dict[str, list[Diagnostic]] = {}
    for d in items:
        groups.setdefault(d.package, []).append(d)
    return groups


def render_summary(collector: DiagnosticCollector) -> None:
    """Render a grouped diagnostic summary to the console.

    In normal mode, shows counts and actionable hints.
    In verbose mode, also lists individual file paths / messages.
    """
    if not collector._diagnostics:
        return

    click.echo("-- Diagnostics --")
    groups = collector.by_category()

    for cat in _CATEGORY_ORDER:
        items = groups.get(cat)
        if not items:
            continue
        renderer = _CATEGORY_RENDERERS.get(cat)
        if renderer:
            renderer(collector, items)


def _render_security_critical(collector: DiagnosticCollector, critical: list[Diagnostic]) -> None:
    """Render critical-severity security findings."""
    _rich_echo(
        f"  [!] {len(critical)} critical security finding(s) -- hidden characters detected",
        color="red",
        bold=True,
    )
    _rich_info("    Run 'apm audit' for full details")
    if collector.verbose:
        by_pkg = _group_by_package(critical)
        for pkg, diags in by_pkg.items():
            if pkg:
                _rich_echo(f"    [{pkg}]", color="dim")
            for d in diags:
                _rich_echo(f"      +- {d.message}", color="red")


def _render_security_warnings(collector: DiagnosticCollector, warnings: list[Diagnostic]) -> None:
    """Render warning-severity security findings."""
    _rich_warning(f"  [!] {len(warnings)} file(s) contain hidden characters")
    if not collector.verbose:
        _rich_info("    Run with --verbose to see details")
    else:
        by_pkg = _group_by_package(warnings)
        for pkg, diags in by_pkg.items():
            if pkg:
                _rich_echo(f"    [{pkg}]", color="dim")
            for d in diags:
                _rich_echo(f"      +- {d.message}", color="dim")


def _render_security_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    critical = [d for d in items if d.severity == "critical"]
    warnings = [d for d in items if d.severity == "warning"]
    info = [d for d in items if d.severity == "info"]

    if critical:
        _render_security_critical(collector, critical)
    if warnings:
        _render_security_warnings(collector, warnings)
    if info and collector.verbose:
        _rich_info(f"  [i] {len(info)} file(s) contain unusual characters")


def _render_policy_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    """Render policy violation diagnostics group.

    Blocked items are rendered in red; warnings in yellow.
    All items show the actionable reason text.
    """
    blocked = [d for d in items if d.severity == "block"]
    warnings = [d for d in items if d.severity != "block"]

    if blocked:
        noun = "dependency" if len(blocked) == 1 else "dependencies"
        _rich_echo(
            f"  [x] {len(blocked)} {noun} blocked by org policy",
            color="red",
            bold=True,
        )
        for d in blocked:
            pkg_prefix = f"{d.package} -- " if d.package else ""
            _rich_echo(f"    +- {pkg_prefix}{d.message}", color="red")
            if d.detail:
                _rich_echo(f"         {d.detail}", color="dim")

    if warnings:
        noun = "policy warning" if len(warnings) == 1 else "policy warnings"
        _rich_warning(f"  [!] {len(warnings)} {noun}")
        for d in warnings:
            pkg_prefix = f"[{d.package}] " if d.package else ""
            _rich_echo(f"    +- {pkg_prefix}{d.message}", color="yellow")
            if d.detail and collector.verbose:
                _rich_echo(f"         {d.detail}", color="dim")


def _render_auth_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    """Render auth diagnostics group."""
    count = len(items)
    noun = "issue" if count == 1 else "issues"
    _rich_warning(f"  [!] {count} authentication {noun}")
    for d in items:
        pkg_prefix = f"[{d.package}] " if d.package else ""
        _rich_echo(f"    +- {pkg_prefix}{d.message}", color="yellow")
        if d.detail and collector.verbose:
            _rich_echo(f"         {d.detail}", color="dim")
    if not collector.verbose:
        _rich_info("    Run with --verbose for auth resolution details")


def _render_collision_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    count = len(items)
    noun = "file" if count == 1 else "files"
    _rich_warning(f"  [!] {count} {noun} skipped -- local files exist, not managed by APM")
    _rich_info("    Use 'apm install --force' to overwrite")
    # Per-dep attribution is now emitted inline by the integrate phase
    # (see services.integrate_package_primitives -- the
    # "(files unchanged)" annotation under each [+] header). The
    # collision footer stays as a global count summary; do NOT enumerate
    # individual file paths even under --verbose.


def _render_overwrite_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    count = len(items)
    noun = "skill" if count == 1 else "skills"
    _rich_warning(f"  [!] {count} {noun} replaced by a different package (last installed wins)")
    if not collector.verbose:
        _rich_info("    Run with --verbose to see details")
    else:
        by_pkg = _group_by_package(items)
        for pkg, diags in by_pkg.items():
            if pkg:
                _rich_echo(f"    [{pkg}]", color="dim")
            for d in diags:
                _rich_echo(f"      +- {d.message}", color="dim")
                if d.detail:
                    _rich_echo(f"         {d.detail}", color="dim")


def _render_warning_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    for d in items:
        pkg_prefix = f"[{d.package}] " if d.package else ""
        _rich_warning(f"  [!] {pkg_prefix}{d.message}")
        if d.detail and collector.verbose:
            _rich_echo(f"    +- {d.detail}", color="dim")


def _render_error_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    count = len(items)
    noun = "package" if count == 1 else "packages"
    _rich_echo(f"  [x] {count} {noun} failed:", color="red")
    for d in items:
        pkg_prefix = f"{d.package} -- " if d.package else ""
        _rich_echo(f"    +- {pkg_prefix}{d.message}", color="red")
        if d.detail and collector.verbose:
            _rich_echo(f"         {d.detail}", color="dim")


def _render_info_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    for d in items:
        _rich_info(f"  [i] {d.message}")
        if d.detail and collector.verbose:
            _rich_echo(f"    +- {d.detail}", color="dim")


def _render_drift_group(collector: DiagnosticCollector, items: list[Diagnostic]) -> None:
    """Render drift findings: modified / unintegrated / orphaned files.

    Stable section header so machine consumers can grep for it.
    Counts shown by kind, then per-file lines with severity-coded markers.
    """
    modified = [d for d in items if d.severity == "modified"]
    unintegrated = [d for d in items if d.severity == "unintegrated"]
    orphaned = [d for d in items if d.severity == "orphaned"]

    total = len(items)
    _rich_warning(f"  [!] Drift detected: {total} file(s) diverge from lockfile")

    for label, group, marker in (
        ("modified", modified, "M"),
        ("unintegrated", unintegrated, "U"),
        ("orphaned", orphaned, "O"),
    ):
        if not group:
            continue
        _rich_echo(f"    {len(group)} {label}:", color="yellow")
        for d in group:
            pkg_prefix = f"[{d.package}] " if d.package else ""
            _rich_echo(f"      {marker}  {pkg_prefix}{d.message}", color="yellow")
            if d.detail and collector.verbose:
                for line in d.detail.splitlines():
                    _rich_echo(f"         {line}", color="dim")


# Dispatch table for render_summary. Defined after all renderers to avoid
# forward-reference issues. Keys mirror _CATEGORY_ORDER.
_CATEGORY_RENDERERS = {
    "security": _render_security_group,
    "policy": _render_policy_group,
    "auth": _render_auth_group,
    "drift": _render_drift_group,
    "collision": _render_collision_group,
    "overwrite": _render_overwrite_group,
    "warning": _render_warning_group,
    "error": _render_error_group,
    "info": _render_info_group,
}
