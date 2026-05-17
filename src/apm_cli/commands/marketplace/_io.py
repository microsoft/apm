"""Marketplace I/O helpers."""

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


def _load_yml_or_exit(logger):
    """Load ``./marketplace.yml`` from CWD or exit with an appropriate code.

    Returns the parsed ``MarketplaceYml`` on success.
    Calls ``sys.exit(1)`` on ``FileNotFoundError`` and
    ``sys.exit(2)`` on ``MarketplaceYmlError`` (schema/parse errors).
    """
    yml_path = Path.cwd() / "marketplace.yml"
    if not yml_path.exists():
        logger.error(
            "No marketplace.yml found. Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    try:
        return load_marketplace_yml(yml_path)
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)


def _load_config_or_exit(logger):
    """Load the marketplace config from CWD (apm.yml or marketplace.yml).

    Returns ``(project_root, config)``. Exits with code 1 when no config
    is found or both files coexist; exits with code 2 on validation errors.
    Emits a deprecation warning when the legacy file is in use.
    """
    project_root = Path.cwd()
    try:
        config = load_marketplace_config(
            project_root,
            warn_callback=lambda msg: logger.warning(msg, symbol="warning"),
        )
    except MarketplaceYmlError as exc:
        msg = str(exc)
        if msg.startswith("No marketplace config"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        if msg.startswith("Both apm.yml"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        logger.error(f"marketplace config error: {exc}", symbol="error")
        sys.exit(2)
    return project_root, config


def _check_gitignore_for_marketplace_json(logger):
    """Warn if .gitignore contains a rule that would ignore marketplace outputs."""
    gitignore_path = Path.cwd() / ".gitignore"
    if not gitignore_path.exists():
        return

    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    patterns = {
        "marketplace.json",
        "**/marketplace.json",
        "/marketplace.json",
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        "*.json",
    }
    for line in lines:
        stripped = line.strip()
        # Skip blank and commented lines
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in patterns:
            logger.warning(
                "Your .gitignore ignores marketplace.json. "
                "Track apm.yml plus generated marketplace files such as "
                ".claude-plugin/marketplace.json and .agents/plugins/marketplace.json. "
                "Remove the .gitignore rule or add explicit unignore entries.",
                symbol="warning",
            )
            return
