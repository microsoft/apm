# pylint: disable=duplicate-code
"""Marketplace outdated helpers."""

from __future__ import annotations

import builtins
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ...marketplace.semver import parse_semver
from .._helpers import _get_console

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class _OutdatedRow:
    """Simple container for outdated table row data."""

    name: str
    current: str
    range_spec: str
    latest_in_range: str
    latest_overall: str
    status: str
    note: str


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
    from ...marketplace.tag_pattern import build_tag_regex

    pattern = entry.tag_pattern or yml.build.tag_pattern
    tag_rx = build_tag_regex(pattern)
    results = []
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/") :]
        m = tag_rx.match(tag_name)
        if not m:
            continue
        version_str = m.group("version")
        sv = parse_semver(version_str)
        if sv is None:
            continue
        if sv.is_prerelease and not (include_prerelease or entry.include_prerelease):
            continue
        results.append((sv, tag_name))
    return results


def _render_outdated_table(logger, rows):
    """Render the outdated-packages table."""
    console = _get_console()
    if not console:
        for row in rows:
            note = f"  ({row.note})" if row.note else ""
            logger.tree_item(
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
