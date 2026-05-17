"""Marketplace outdated helpers."""

from __future__ import annotations

import builtins
import json
import re
import sys
import traceback
from pathlib import Path

import click
import yaml

from ...core.command_logger import CommandLogger
from ...marketplace.builder import BuildOptions, BuildReport, MarketplaceBuilder, ResolvedPackage
from ...marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import (
    ConfigSource,
    detect_config_source,
    load_marketplace_config,
    migrate_marketplace_yml,
)
from ...marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ...marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)
from ...marketplace.ref_resolver import RefResolver, RemoteRef
from ...marketplace.semver import SemVer, parse_semver, satisfies_range
from ...marketplace.yml_schema import load_marketplace_yml
from ...utils.console import _rich_info, _rich_warning  # noqa: F401
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console, _is_interactive

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


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
