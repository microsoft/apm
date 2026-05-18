"""Diagnostic collector for structured warning/error reporting.

Provides a collect-then-render pattern: integrators push diagnostics
during install (or any command), and the collector renders a clean,
grouped summary at the end.  This replaces inline ``print()`` /
``_rich_warning()`` calls that previously produced noisy, repetitive
output when many packages are involved.
"""

import threading
from dataclasses import dataclass

from apm_cli.utils.console import (
    _get_console,
    _rich_echo,
    _rich_info,
    _rich_warning,
)

from . import _diagnostics_render as _dr

# Diagnostic categories -- used as grouping keys in render_summary()
CATEGORY_COLLISION = "collision"
CATEGORY_OVERWRITE = "overwrite"
CATEGORY_WARNING = "warning"
CATEGORY_ERROR = "error"
CATEGORY_SECURITY = "security"
CATEGORY_POLICY = "policy"
CATEGORY_AUTH = "auth"
CATEGORY_DRIFT = "drift"
CATEGORY_INFO = "info"

# Drift severities: kinds of divergence from the lockfile-defined state.
DRIFT_MODIFIED = "modified"  # tracked file content changed
DRIFT_UNINTEGRATED = "unintegrated"  # tracked file missing from project
DRIFT_ORPHANED = "orphaned"  # tracked in lockfile but not produced by replay

_CATEGORY_ORDER = [
    CATEGORY_SECURITY,
    CATEGORY_POLICY,
    CATEGORY_AUTH,
    CATEGORY_DRIFT,
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

    def drift(
        self,
        path: str,
        kind: str,
        package: str = "",
        detail: str = "",
    ) -> None:
        """Record a drift finding from ``apm audit`` replay.

        Parameters
        ----------
        path : str
            Project-relative path of the divergent file.
        kind : str
            One of ``DRIFT_MODIFIED``, ``DRIFT_UNINTEGRATED``, ``DRIFT_ORPHANED``.
        package : str
            Package name owning the file (best-effort; may be empty for orphans).
        detail : str
            Optional inline diff or extra context (rendered only in verbose).
        """
        with self._lock:
            self._diagnostics.append(
                Diagnostic(
                    message=path,
                    category=CATEGORY_DRIFT,
                    package=package,
                    detail=detail,
                    severity=kind,
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
    def drift_count(self) -> int:
        """Return number of drift findings."""
        return sum(1 for d in self._diagnostics if d.category == CATEGORY_DRIFT)

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
    # Rendering -- delegates to _diagnostics_render for line-count budget
    # ------------------------------------------------------------------

    def render_summary(self) -> None:
        return _dr.render_summary(self)

    def _render_security_group(self, items: list) -> None:
        return _dr._render_security_group(self, items)

    def _render_policy_group(self, items: list) -> None:
        return _dr._render_policy_group(self, items)

    def _render_auth_group(self, items: list) -> None:
        return _dr._render_auth_group(self, items)

    def _render_collision_group(self, items: list) -> None:
        return _dr._render_collision_group(self, items)

    def _render_overwrite_group(self, items: list) -> None:
        return _dr._render_overwrite_group(self, items)

    def _render_warning_group(self, items: list) -> None:
        return _dr._render_warning_group(self, items)

    def _render_error_group(self, items: list) -> None:
        return _dr._render_error_group(self, items)

    def _render_info_group(self, items: list) -> None:
        return _dr._render_info_group(self, items)

    def _render_drift_group(self, items: list) -> None:
        return _dr._render_drift_group(self, items)


def _group_by_package(
    items: list,
) -> dict:
    return _dr._group_by_package(items)
