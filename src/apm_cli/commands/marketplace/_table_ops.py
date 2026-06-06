"""Table-rendering helpers and data containers for the marketplace commands.

Extracted from ``marketplace/__init__.py`` to keep that module under 800 lines.
All names are re-exported from the package ``__init__`` so existing import
paths (``from apm_cli.commands.marketplace import _CheckResult``, etc.) keep
working.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _mkt_get_console():
    """Route to ``marketplace._get_console`` so test patches apply."""
    from apm_cli.commands import marketplace as _m

    return _m._get_console()


# ---------------------------------------------------------------------------
# Build-error rendering
# ---------------------------------------------------------------------------


def _render_build_error(log, exc):
    """Render a BuildError with actionable hints."""
    from ...marketplace.errors import (
        GitLsRemoteError,
        HeadNotAllowedError,
        NoMatchingVersionError,
        OfflineMissError,
        RefNotFoundError,
    )

    if isinstance(exc, GitLsRemoteError):
        log.error(exc.summary_text, symbol="error")
        if exc.hint:
            log.progress(f"Hint: {exc.hint}", symbol="info")
    elif isinstance(exc, NoMatchingVersionError):
        log.error(str(exc), symbol="error")
        log.progress(
            "Check that your version range matches published tags.",
            symbol="info",
        )
    elif isinstance(exc, RefNotFoundError):
        log.error(str(exc), symbol="error")
        log.progress(
            "Verify the ref is spelled correctly and the remote is reachable.",
            symbol="info",
        )
    elif isinstance(exc, HeadNotAllowedError):
        log.error(str(exc), symbol="error")
    elif isinstance(exc, OfflineMissError):
        log.error(str(exc), symbol="error")
        log.progress(
            "Run a build online first to populate the cache.",
            symbol="info",
        )
    else:
        log.error(f"Build failed: {exc}", symbol="error")


def _render_build_table(log, report):
    """Render the resolved-packages table (Rich with colorama fallback)."""
    from ...marketplace.semver import parse_semver

    console = _mkt_get_console()
    if not console:
        for pkg in report.resolved:
            sha_short = pkg.sha[:8] if pkg.sha else "--"
            ref_kind = "tag" if not pkg.ref.startswith("refs/heads/") else "branch"
            log.tree_item(f"  [+] {pkg.name}  {pkg.ref}  {sha_short}  ({ref_kind})")
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Resolved Packages",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", style="green", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Version", style="cyan")
    table.add_column("Commit", style="dim")
    table.add_column("Ref Kind", style="white")

    for pkg in report.resolved:
        sha_short = pkg.sha[:8] if pkg.sha else "--"
        ref_kind = "tag"
        if pkg.ref and not parse_semver(pkg.ref.lstrip("vV")):
            ref_kind = "ref"
        table.add_row(Text("[+]"), pkg.name, pkg.ref, sha_short, ref_kind)

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Outdated-packages helpers
# ---------------------------------------------------------------------------


class _OutdatedRow:
    """Simple container for outdated table row data."""

    __slots__ = (
        "current",
        "latest_in_range",
        "latest_overall",
        "name",
        "note",
        "range_spec",
        "status",
    )

    def __init__(self, name, current, range_spec, latest_in_range, latest_overall, status, note):
        self.name = name
        self.current = current
        self.range_spec = range_spec
        self.latest_in_range = latest_in_range
        self.latest_overall = latest_overall
        self.status = status
        self.note = note


def _load_current_versions():
    """Load current ref versions from marketplace.json if present."""
    mkt_path = Path.cwd() / "marketplace.json"
    if not mkt_path.exists():
        return {}
    try:
        data = json.loads(mkt_path.read_text(encoding="utf-8"))
        result = {}
        for plugin in data.get("plugins", []):
            name = plugin.get("name", "")
            src = plugin.get("source", {})
            if isinstance(src, dict):
                result[name] = src.get("ref", "--")
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_tag_versions(refs, entry, yml, include_prerelease):
    """Extract (SemVer, tag_name) pairs from remote refs for a package entry."""
    from ...marketplace._shared import iter_semver_tags
    from ...marketplace.tag_pattern import (
        build_tag_regex,
        infer_tag_pattern_from_refs,
    )

    def _collect(pattern: str) -> list:
        tag_rx = (
            build_tag_regex(pattern, name=entry.name)
            if "{name}" in pattern
            else build_tag_regex(pattern)
        )
        collected = []
        for sv, tag_name, _ in iter_semver_tags(refs, tag_rx):
            if sv.is_prerelease and not (include_prerelease or entry.include_prerelease):
                continue
            collected.append((sv, tag_name))
        return collected

    pattern = entry.tag_pattern or yml.build.tag_pattern
    results = _collect(pattern)
    if not results:
        inferred = infer_tag_pattern_from_refs(refs, entry.name)
        if inferred and inferred != pattern:
            logger.debug(
                "Configured tag pattern %r matched no tags for %s; inferred %r",
                pattern,
                entry.name,
                inferred,
            )
            results = _collect(inferred)
    return results


def _render_outdated_table(log, rows):
    """Render the outdated-packages table."""
    console = _mkt_get_console()
    if not console:
        for row in rows:
            note = f"  ({row.note})" if row.note else ""
            log.tree_item(
                f"  {row.status} {row.name}  current={row.current}  "
                f"latest-in-range={row.latest_in_range}  "
                f"latest={row.latest_overall}{note}"
            )
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Package Version Status",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", style="green", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Current", style="white")
    table.add_column("Range", style="dim")
    table.add_column("Latest in Range", style="cyan")
    table.add_column("Latest Overall", style="yellow")

    for row in rows:
        note = ""
        if row.note:
            note = f" ({row.note})"
        table.add_row(
            Text(row.status),
            row.name,
            row.current,
            row.range_spec,
            row.latest_in_range + note,
            row.latest_overall,
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Check-results helpers
# ---------------------------------------------------------------------------


class _CheckResult:
    """Container for per-entry check results."""

    __slots__ = ("error", "name", "reachable", "ref_ok", "version_found")

    def __init__(self, name, reachable, version_found, ref_ok, error):
        self.name = name
        self.reachable = reachable
        self.version_found = version_found
        self.ref_ok = ref_ok
        self.error = error


def _render_check_table(log, results):
    """Render the check-results table."""
    console = _mkt_get_console()
    if not console:
        for r in results:
            icon = "[+]" if r.ref_ok else "[x]"
            detail = r.error if r.error else "OK"
            log.tree_item(f"  {icon} {r.name}: {detail}")
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Entry Health Check",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Reachable", style="white", justify="center")
    table.add_column("Version Found", style="white", justify="center")
    table.add_column("Ref OK", style="white", justify="center")
    table.add_column("Detail", style="dim")

    for r in results:
        reach = "[+]" if r.reachable else "[x]"
        ver = "[+]" if r.version_found else "[x]"
        ref = "[+]" if r.ref_ok else "[x]"
        detail = r.error if r.error else "OK"
        table.add_row(
            Text("[+]" if r.ref_ok else "[x]"),
            r.name,
            Text(reach),
            Text(ver),
            Text(ref),
            detail,
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Doctor-check helpers
# ---------------------------------------------------------------------------


class _DoctorCheck:
    """Container for a single doctor check result."""

    __slots__ = ("detail", "informational", "name", "passed")

    def __init__(self, name, passed, detail, informational=False):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.informational = informational


def _render_doctor_table(log, checks):
    """Render the doctor results table."""
    console = _mkt_get_console()
    if not console:
        for c in checks:
            if c.informational:
                icon = "[i]"
            elif c.passed:
                icon = "[+]"
            else:
                icon = "[x]"
            log.tree_item(f"  {icon} {c.name}: {c.detail}")
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Environment Diagnostics",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Check", style="bold white", no_wrap=True)
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Detail", style="white")

    for c in checks:
        if c.informational:
            icon = "[i]"
        elif c.passed:
            icon = "[+]"
        else:
            icon = "[x]"
        table.add_row(c.name, Text(icon), c.detail)

    console.print()
    console.print(table)
