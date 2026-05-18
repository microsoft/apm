"""Marketplace CLI package."""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml

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
from ._build_render import _render_build_error, _render_build_table
from ._check import (
    _CheckResult,
    _find_duplicate_names,
    _render_check_table,
    _warn_duplicate_names,
)
from ._doctor import _DoctorCheck, _render_doctor_table
from ._io import (
    _check_gitignore_for_marketplace_json,
    _load_config_or_exit,
    _load_yml_or_exit,
)
from ._outdated import (
    _extract_tag_versions,
    _load_current_versions,
    _OutdatedRow,
    _render_outdated_table,
)
from ._publish_helpers import (
    _load_targets_file,
    _outcome_symbol,
    _render_publish_footer,
    _render_publish_plan,
    _render_publish_summary,
)


class MarketplaceGroup(click.Group):
    """Custom group that organises commands by audience."""

    _consumer_commands = [  # noqa: RUF012
        "add",
        "list",
        "browse",
        "update",
        "remove",
        "validate",
    ]
    _authoring_commands = [  # noqa: RUF012
        "init",
        "check",
        "outdated",
        "doctor",
        "publish",
        "package",
        "migrate",
    ]

    def get_command(self, ctx, cmd_name):
        # The 'build' subcommand was removed in favour of the unified
        # 'apm pack' entrypoint. Surface a hard error with a migration
        # hint rather than silently aliasing.
        if cmd_name == "build":
            raise click.UsageError(
                "'apm marketplace build' was removed. Use 'apm pack' instead.\n"
                "marketplace.json is now produced by 'apm pack' when "
                "apm.yml has a 'marketplace:' block."
            )
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx, formatter):
        sections = [
            ("Consumer commands", self._consumer_commands),
            ("Authoring commands", self._authoring_commands),
        ]

        for section_name, cmd_names in sections:
            commands = []
            for name in cmd_names:
                cmd = self.get_command(ctx, name)
                if cmd is None:
                    continue
                help_text = cmd.get_short_help_str(limit=150)
                commands.append((name, help_text))
            if commands:
                with formatter.section(section_name):
                    formatter.write_dl(commands)


@click.group(cls=MarketplaceGroup, help="Manage marketplaces for discovery and governance")
@click.pass_context
def marketplace(ctx):
    """Register, browse, and search marketplaces."""


from .plugin import package  # noqa: E402

marketplace.add_command(package)

from ._consumer_cmds import add, browse, list_cmd, remove, update  # noqa: E402
from ._search_cmd import search  # noqa: E402
from .check import check  # noqa: E402
from .doctor import doctor  # noqa: E402
from .init import init  # noqa: E402
from .migrate import migrate  # noqa: E402
from .outdated import outdated  # noqa: E402
from .publish import publish  # noqa: E402
from .validate import validate  # noqa: E402

__all__ = [
    "BuildError",
    "BuildOptions",
    "BuildReport",
    "ConfigSource",
    "ConsumerTarget",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "MarketplaceBuilder",
    "MarketplaceGroup",
    "MarketplaceNotFoundError",
    "MarketplacePublisher",
    "MarketplaceYmlError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "PathTraversalError",
    "PrIntegrator",
    "PrResult",
    "PrState",
    "PublishOutcome",
    "PublishPlan",
    "RefNotFoundError",
    "RefResolver",
    "RemoteRef",
    "ResolvedPackage",
    "SemVer",
    "TargetResult",
    "add",
    "browse",
    "check",
    "detect_config_source",
    "doctor",
    "init",
    "list_cmd",
    "load_marketplace_config",
    "load_marketplace_yml",
    "marketplace",
    "migrate",
    "migrate_marketplace_yml",
    "outdated",
    "package",
    "parse_semver",
    "publish",
    "remove",
    "satisfies_range",
    "search",
    "translate_git_stderr",
    "update",
    "validate",
    "validate_path_segments",
]
# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "Path",
    "_CheckResult",
    "_DoctorCheck",
    "_OutdatedRow",
    "_check_gitignore_for_marketplace_json",
    "_extract_tag_versions",
    "_find_duplicate_names",
    "_load_config_or_exit",
    "_load_current_versions",
    "_load_targets_file",
    "_load_yml_or_exit",
    "_outcome_symbol",
    "_render_build_error",
    "_render_build_table",
    "_render_check_table",
    "_render_doctor_table",
    "_render_outdated_table",
    "_render_publish_footer",
    "_render_publish_plan",
    "_render_publish_summary",
    "_rich_info",
    "_rich_warning",
    "_warn_duplicate_names",
    "json",
    "yaml",
]
