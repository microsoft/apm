"""Diagnostic collector for structured warning/error reporting.

Provides a collect-then-render pattern: integrators push diagnostics
during install (or any command), and the collector renders a clean,
grouped summary at the end.  This replaces inline ``print()`` /
``_rich_warning()`` calls that previously produced noisy, repetitive
output when many packages are involved.
"""

import threading
from dataclasses import dataclass, field  # noqa: F401
from typing import Dict, List, Optional  # noqa: F401, UP035

from apm_cli.utils.console import (
    _get_console,
    _rich_echo,
    _rich_info,
    _rich_warning,
)

# Diagnostic categories -- used as grouping keys in render_summary()
CATEGORY_COLLISION = "collision"
CATEGORY_OVERWRITE = "overwrite"
CATEGORY_WARNING = "warning"
CATEGORY_ERROR = "error"
CATEGORY_SECURITY = "security"
CATEGORY_POLICY = "policy"
CATEGORY_AUTH = "auth"
CATEGORY_INFO = "info"

_CATEGORY_ORDER = [
    CATEGORY_SECURITY,
    CATEGORY_POLICY,
    CATEGORY_AUTH,
    CATEGORY_COLLISION,
    CATEGORY_OVERWRITE,
    CATEGORY_WARNING,
    CATEGORY_ERROR,
    CATEGORY_INFO,
]


@dataclass(frozen=True)
class Diagnostic:
    """Single diagnostic message produced during an operation."""

    message: str
    category: str
    package: str = ""
    detail: str = ""
    severity: str = ""  # e.g. "critical", "warning", "info" -- used by security category


class DiagnosticCollector:
    """Collects diagnostics during a multi-package operation and renders
    a grouped summary at the end.

    Thread-safe: multiple integrators may push diagnostics concurrently
    during parallel installs.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._diagnostics: list[Diagnostic] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def skip(self, path: str, package: str = "") -> None:
        """Record a collision skip (file exists, not managed by APM)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=path,
                    category=CATEGORY_COLLISION,
                    package=package,
                )
            )

    def overwrite(self, path: str, package: str = "", detail: str = "") -> None:
        """Record a sub-skill or file overwrite."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=path,
                    category=CATEGORY_OVERWRITE,
                    package=package,
                    detail=detail,
                )
            )

    def warn(self, message: str, package: str = "", detail: str = "") -> None:
        """Record a general warning."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_WARNING,
                    package=package,
                    detail=detail,
                )
            )

    def error(self, message: str, package: str = "", detail: str = "") -> None:
        """Record an error (download failure, integration failure, etc.)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_ERROR,
                    package=package,
                    detail=detail,
                )
            )

    def security(
        self,
        message: str,
        package: str = "",
        detail: str = "",
        severity: str = "warning",
    ) -> None:
        """Record a security finding (hidden characters, etc.)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_SECURITY,
                    package=package,
                    detail=detail,
                    severity=severity,
                )
            )

    def info(self, message: str, package: str = "", detail: str = "") -> None:
        """Record an informational hint (non-blocking, actionable guidance)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_INFO,
                    package=package,
                    detail=detail,
                )
            )

    def policy(
        self,
        message: str,
        package: str = "",
        detail: str = "",
        severity: str = "warning",
    ) -> None:
        """Record a policy violation (blocked dep, denied source, etc.)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_POLICY,
                    package=package,
                    detail=detail,
                    severity=severity,
                )
            )

    def auth(self, message: str, package: str = "", detail: str = "") -> None:
        """Record an authentication diagnostic (credential resolution, fallback, EMU detection)."""
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=message,
                    category=CATEGORY_AUTH,
                    package=package,
                    detail=detail,
                )
            )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @property
    def has_diagnostics(self) -> bool:
        """Return True if any diagnostics have been recorded."""
        return len(self._diagnostics) > 0

    @property
    def error_count(self) -> int:
        return sum(1 for d in self._diagnostics if d.category == CATEGORY_ERROR)

    @property
    def security_count(self) -> int:
        """Return number of security findings."""
        return sum(1 for d in self._diagnostics if d.category == CATEGORY_SECURITY)

    @property
    def auth_count(self) -> int:
        """Return number of auth diagnostics."""
        return sum(1 for d in self._diagnostics if d.category == CATEGORY_AUTH)

    @property
    def policy_count(self) -> int:
        """Return number of policy diagnostics."""
        return sum(1 for d in self._diagnostics if d.category == CATEGORY_POLICY)

    @property
    def has_critical_security(self) -> bool:
        """Return True if any critical-severity security finding exists."""
        return any(
            d.category == CATEGORY_SECURITY and d.severity == "critical" for d in self._diagnostics
        )

    def by_category(self) -> dict[str, list[Diagnostic]]:
        """Return diagnostics grouped by category, preserving insertion order."""
        groups: dict[str, list[Diagnostic]] = {}
        for d in self._diagnostics:
            groups.setdefault(d.category, []).append(d)
        return groups

    def count_for_package(self, package: str, category: str = "") -> int:
        """Count diagnostics for a specific package, optionally filtered by category."""
        with self._lock:
            return sum(
                1
                for d in self._diagnostics
                if d.package == package and (not category or d.category == category)
            )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_summary(self) -> None:
        """Render a grouped diagnostic summary to the console.

        In normal mode, shows counts and actionable hints.
        In verbose mode, also lists individual file paths / messages.
        """
        if not self._diagnostics:
            return

        groups = self.by_category()

        console = _get_console()
        # Separator line
        if console:
            try:
                console.print()
                console.print("-- Diagnostics --", style="bold cyan")
            except Exception:
                _rich_echo("")
                _rich_echo("-- Diagnostics --", color="cyan", bold=True)
        else:
            _rich_echo("")
            _rich_echo("-- Diagnostics --", color="cyan", bold=True)

        for cat in _CATEGORY_ORDER:
            items = groups.get(cat)
            if not items:
                continue

            if cat == CATEGORY_SECURITY:
                self._render_security_group(items)
            elif cat == CATEGORY_POLICY:
                self._render_policy_group(items)
            elif cat == CATEGORY_AUTH:
                self._render_auth_group(items)
            elif cat == CATEGORY_COLLISION:
                self._render_collision_group(items)
            elif cat == CATEGORY_OVERWRITE:
                self._render_overwrite_group(items)
            elif cat == CATEGORY_WARNING:
                self._render_warning_group(items)
            elif cat == CATEGORY_ERROR:
                self._render_error_group(items)
            elif cat == CATEGORY_INFO:
                self._render_info_group(items)

        if console:
            try:
                console.print()
            except Exception:
                _rich_echo("")
        else:
            _rich_echo("")

    # -- Per-category renderers ------------------------------------

    def _render_security_group(self, items: list[Diagnostic]) -> None:
        critical = [d for d in items if d.severity == "critical"]
        warnings = [d for d in items if d.severity == "warning"]
        info = [d for d in items if d.severity == "info"]

        if critical:
            _rich_echo(
                f"  [!] {len(critical)} critical security finding(s) -- hidden characters detected",
                color="red",
                bold=True,
            )
            _rich_info("    Run 'apm audit' for full details")
            if self.verbose:
                by_pkg = _group_by_package(critical)
                for pkg, diags in by_pkg.items():
                    if pkg:
                        _rich_echo(f"    [{pkg}]", color="dim")
                    for d in diags:
                        _rich_echo(f"      +- {d.message}", color="red")

        if warnings:
            _rich_warning(f"  [!] {len(warnings)} file(s) contain hidden characters")
            if not self.verbose:
                _rich_info("    Run with --verbose to see details")
            else:
                by_pkg = _group_by_package(warnings)
                for pkg, diags in by_pkg.items():
                    if pkg:
                        _rich_echo(f"    [{pkg}]", color="dim")
                    for d in diags:
                        _rich_echo(f"      +- {d.message}", color="dim")

        if info and self.verbose:
            _rich_info(f"  [i] {len(info)} file(s) contain unusual characters")

    def _render_policy_group(self, items: list[Diagnostic]) -> None:
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
                if d.detail and self.verbose:
                    _rich_echo(f"         {d.detail}", color="dim")

    def _render_auth_group(self, items: list[Diagnostic]) -> None:
        """Render auth diagnostics group."""
        count = len(items)
        noun = "issue" if count == 1 else "issues"
        _rich_warning(f"  [!] {count} authentication {noun}")
        for d in items:
            pkg_prefix = f"[{d.package}] " if d.package else ""
            _rich_echo(f"    +- {pkg_prefix}{d.message}", color="yellow")
            if d.detail and self.verbose:
                _rich_echo(f"         {d.detail}", color="dim")
        if not self.verbose:
            _rich_info("    Run with --verbose for auth resolution details")

    def _render_collision_group(self, items: list[Diagnostic]) -> None:
        count = len(items)
        noun = "file" if count == 1 else "files"
        _rich_warning(f"  [!] {count} {noun} skipped -- local files exist, not managed by APM")
        _rich_info("    Use 'apm install --force' to overwrite")
        if not self.verbose:
            _rich_info("    Run with --verbose to see individual files")
        else:
            # Group by package for readability
            by_pkg = _group_by_package(items)
            for pkg, diags in by_pkg.items():
                if pkg:
                    _rich_echo(f"    [{pkg}]", color="dim")
                for d in diags:
                    _rich_echo(f"      +- {d.message}", color="dim")

    def _render_overwrite_group(self, items: list[Diagnostic]) -> None:
        count = len(items)
        noun = "skill" if count == 1 else "skills"
        _rich_warning(f"  [!] {count} {noun} replaced by a different package (last installed wins)")
        if not self.verbose:
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

    def _render_warning_group(self, items: list[Diagnostic]) -> None:
        for d in items:
            pkg_prefix = f"[{d.package}] " if d.package else ""
            _rich_warning(f"  [!] {pkg_prefix}{d.message}")
            if d.detail and self.verbose:
                _rich_echo(f"    +- {d.detail}", color="dim")

    def _render_error_group(self, items: list[Diagnostic]) -> None:
        count = len(items)
        noun = "package" if count == 1 else "packages"
        _rich_echo(f"  [x] {count} {noun} failed:", color="red")
        for d in items:
            pkg_prefix = f"{d.package} -- " if d.package else ""
            _rich_echo(f"    +- {pkg_prefix}{d.message}", color="red")
            if d.detail and self.verbose:
                _rich_echo(f"         {d.detail}", color="dim")

    def _render_info_group(self, items: list[Diagnostic]) -> None:
        for d in items:
            _rich_info(f"  [i] {d.message}")
            if d.detail and self.verbose:
                _rich_echo(f"    +- {d.detail}", color="dim")


def _group_by_package(items: list[Diagnostic]) -> dict[str, list[Diagnostic]]:
    """Group diagnostics by package, preserving insertion order.

    Items with an empty package key are collected under ``""``.
    """
    groups: dict[str, list[Diagnostic]] = {}
    for d in items:
        groups.setdefault(d.package, []).append(d)
    return groups
