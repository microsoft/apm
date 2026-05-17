"""Marketplace doctor helpers."""

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


class _DoctorCheck:
    """Container for a single doctor check result."""

    __slots__ = ("detail", "informational", "name", "passed")

    def __init__(self, name, passed, detail, informational=False):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.informational = informational


def _render_doctor_table(logger, checks):
    """Render the doctor results table."""
    console = _get_console()
    if not console:
        for c in checks:
            if c.informational:
                icon = "[i]"
            elif c.passed:
                icon = "[+]"
            else:
                icon = "[x]"
            logger.tree_item(f"  {icon} {c.name}: {c.detail}")
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
