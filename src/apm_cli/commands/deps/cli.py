"""APM dependency management CLI commands."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import click

# Import existing APM components
from ...constants import APM_MODULES_DIR
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...models.apm_package import APMPackage as APMPackage
from ._utils import (
    _add_tree_children,
    _dep_display_name,
    _deps_list_source_label,
    _format_primitive_counts,
)

# ---------------------------------------------------------------------------
# Data resolution — deps list
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScopeDisplayContext:
    """Shared display dependencies for listing one install scope."""

    logger: object
    console: object
    has_rich: bool
    insecure_only: bool = False


def _resolve_scope_deps(apm_dir, logger, insecure_only=False):
    return _deps_sections._resolve_scope_deps(apm_dir, logger, insecure_only)


@click.group(help="Manage APM package dependencies")
def deps():
    """APM dependency management commands."""
    pass


def _show_scope_deps(scope_label, apm_dir, ctx: _ScopeDisplayContext):
    return _deps_sections._show_scope_deps(scope_label, apm_dir, ctx)


@deps.command(name="list", help="List installed APM dependencies")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="List user-scope dependencies (~/.apm/) instead of project",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show both project and user-scope dependencies",
)
@click.option(
    "--insecure",
    "insecure_only",
    is_flag=True,
    default=False,
    help="Show only installed dependencies locked to http:// sources",
)
def list_packages(global_, show_all, insecure_only):
    """Show all installed APM dependencies with context files and agent workflows."""
    logger = CommandLogger("deps-list")

    try:
        # Import Rich components with fallback
        import shutil

        from rich.console import Console

        term_width = shutil.get_terminal_size((120, 24)).columns
        console = Console(width=max(120, term_width))
        has_rich = True
    except ImportError:
        has_rich = False
        console = None

    try:
        from ...core.scope import InstallScope, get_apm_dir

        display_ctx = _ScopeDisplayContext(
            logger=logger,
            console=console,
            has_rich=has_rich,
            insecure_only=insecure_only,
        )
        if show_all:
            _show_scope_deps("Project", get_apm_dir(InstallScope.PROJECT), display_ctx)
            if console and has_rich:
                console.print()  # spacing between tables
            _show_scope_deps("Global", get_apm_dir(InstallScope.USER), display_ctx)
        elif global_:
            _show_scope_deps("Global", get_apm_dir(InstallScope.USER), display_ctx)
        else:
            _show_scope_deps("Project", get_apm_dir(InstallScope.PROJECT), display_ctx)
    except Exception as e:
        logger.error(f"Error listing dependencies: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data resolution — deps tree
# ---------------------------------------------------------------------------


def _build_dep_tree(apm_dir):
    return _deps_sections._build_dep_tree(apm_dir)


@deps.command(help="Show dependency tree structure")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Show user-scope dependency tree (~/.apm/)",
)
def tree(global_):
    return _deps_sections.tree(global_)


@deps.command(help="Remove all APM dependencies")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would be removed without removing"
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
def clean(dry_run: bool, yes: bool):
    return _deps_sections.clean(dry_run, yes)


@deps.command(help="Update APM dependencies to latest refs")
@click.argument("packages", nargs=-1)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed update information")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-authored files on collision",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated). Values: copilot, claude, cursor, opencode, codex, gemini, windsurf, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf (excludes agent-skills); combine with 'agent-skills' for both. 'copilot-cowork' is also accepted when the copilot-cowork experimental flag is enabled (run 'apm experimental enable copilot-cowork').",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Update user-scope dependencies (~/.apm/)",
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Compatibility flag for projects that "
        "need per-client skill layouts."
    ),
)
def update(packages, **params):
    return _deps_sections.update(packages, **params)


@deps.command(help="Show detailed package information")
@click.argument("package", required=True)
def info(package: str):
    """Show detailed information about a specific package including context files and workflows."""
    from ..view import display_package_info, resolve_package_path

    logger = CommandLogger("deps-info")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.error("No apm_modules/ directory found")
        logger.progress("Run 'apm install' to install dependencies first")
        sys.exit(1)

    package_path = resolve_package_path(package, apm_modules_path, logger)
    display_package_info(package, package_path, logger)


from . import sections as _deps_sections
