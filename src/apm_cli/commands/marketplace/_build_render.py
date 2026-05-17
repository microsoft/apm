"""Marketplace build rendering helpers."""

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


def _render_build_error(logger, exc):
    """Render a BuildError with actionable hints."""
    if isinstance(exc, GitLsRemoteError):
        logger.error(exc.summary_text, symbol="error")
        if exc.hint:
            logger.progress(f"Hint: {exc.hint}", symbol="info")
    elif isinstance(exc, NoMatchingVersionError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Check that your version range matches published tags.",
            symbol="info",
        )
    elif isinstance(exc, RefNotFoundError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Verify the ref is spelled correctly and the remote is reachable.",
            symbol="info",
        )
    elif isinstance(exc, HeadNotAllowedError):
        logger.error(str(exc), symbol="error")
    elif isinstance(exc, OfflineMissError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Run a build online first to populate the cache.",
            symbol="info",
        )
    else:
        logger.error(f"Build failed: {exc}", symbol="error")


def _render_build_table(logger, report):
    """Render the resolved-packages table (Rich with colorama fallback)."""
    console = _get_console()
    if not console:
        # Colorama fallback
        for pkg in report.resolved:
            sha_short = pkg.sha[:8] if pkg.sha else "--"
            ref_kind = "tag" if not pkg.ref.startswith("refs/heads/") else "branch"
            logger.tree_item(f"  [+] {pkg.name}  {pkg.ref}  {sha_short}  ({ref_kind})")
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
        # Determine ref kind
        ref_kind = "tag"
        if pkg.ref and not parse_semver(pkg.ref.lstrip("vV")):
            ref_kind = "ref"
        table.add_row(Text("[+]"), pkg.name, pkg.ref, sha_short, ref_kind)

    console.print()
    console.print(table)
