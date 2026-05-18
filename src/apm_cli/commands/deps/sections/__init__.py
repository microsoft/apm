"""APM dependency management CLI commands."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from ....constants import APM_MODULES_DIR
from ....core.command_logger import CommandLogger
from ..._helpers import _standalone_installed_packages
from .._utils import (
    _add_tree_children,
    _count_primitives,
    _dep_display_name,
    _deps_list_source_label,
    _format_primitive_counts,
    _get_package_display_info,
    _is_nested_under_package,
)

# Re-export complex functions from helper modules
from .scope_resolver import _resolve_scope_deps
from .update_engine import update


@dataclass(frozen=True, slots=True)
class _ScopeDisplayContext:
    """Rendering dependencies for a single scope listing."""

    logger: object
    console: object
    has_rich: bool
    insecure_only: bool = False


from ._tree_output import (
    _build_dep_tree,
    _build_tree_render_context,
    _render_tree_output,
    _TreeRenderContext,
)


def _format_primitive_cell(primitives: dict, key: str) -> str:
    count = primitives.get(key, 0)
    return str(count) if count > 0 else "-"


def _emit_orphaned_packages(logger: object, orphaned_packages: list[str]) -> None:
    if not orphaned_packages:
        return
    logger.warning(f"{len(orphaned_packages)} orphaned package(s) found (not in apm.yml):")
    for pkg in orphaned_packages:
        logger.warning(f"  - {pkg}")
    logger.info("Run 'apm prune' to remove orphaned packages")


def _build_scope_table(scope_label: str, insecure_only: bool):
    from rich.table import Table

    table = Table(
        title=(
            f" Insecure APM Dependencies ({scope_label})"
            if insecure_only
            else f" APM Dependencies ({scope_label})"
        ),
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Package", style="bold white")
    table.add_column("Version", style="yellow")
    table.add_column("Source", style="blue")
    if insecure_only:
        table.add_column("Origin", style="bold red")
    table.add_column("Prompts", style="magenta", justify="center")
    table.add_column("Instructions", style="green", justify="center")
    table.add_column("Agents", style="cyan", justify="center")
    table.add_column("Skills", style="yellow", justify="center")
    table.add_column("Hooks", style="red", justify="center")
    return table


def _render_rich_scope_deps(
    scope_label: str,
    installed_packages: list[dict],
    orphaned_packages: list[str],
    ctx: _ScopeDisplayContext,
) -> None:
    table = _build_scope_table(scope_label, ctx.insecure_only)
    for pkg in installed_packages:
        primitives = pkg["primitives"]
        row = [pkg["name"], pkg["version"], pkg["source"]]
        if ctx.insecure_only:
            row.append(pkg["insecure_via"])
        row.extend(
            [
                _format_primitive_cell(primitives, "prompts"),
                _format_primitive_cell(primitives, "instructions"),
                _format_primitive_cell(primitives, "agents"),
                _format_primitive_cell(primitives, "skills"),
                _format_primitive_cell(primitives, "hooks"),
            ]
        )
        table.add_row(*row)
    ctx.console.print(table)
    _emit_orphaned_packages(ctx.logger, orphaned_packages)


def _emit_scope_plain_header(scope_label: str, insecure_only: bool) -> None:
    if insecure_only:
        click.echo(f" Insecure APM Dependencies ({scope_label}):")
        click.echo(
            f"{'Package':<30} {'Version':<10} {'Source':<12} {'Origin':<18} "
            f"{'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
        )
        click.echo("-" * 117)
        return
    click.echo(f" APM Dependencies ({scope_label}):")
    click.echo(
        f"{'Package':<30} {'Version':<10} {'Source':<12} {'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
    )
    click.echo("-" * 98)


def _render_plain_scope_row(pkg: dict, insecure_only: bool) -> None:
    primitives = pkg["primitives"]
    counts = [
        _format_primitive_cell(primitives, "prompts"),
        _format_primitive_cell(primitives, "instructions"),
        _format_primitive_cell(primitives, "agents"),
        _format_primitive_cell(primitives, "skills"),
        _format_primitive_cell(primitives, "hooks"),
    ]
    prefix = f"{pkg['name'][:28]:<30} {pkg['version'][:8]:<10} {pkg['source'][:10]:<12}"
    suffix = f"{counts[0]:>7} {counts[1]:>7} {counts[2]:>7} {counts[3]:>7} {counts[4]:>7}"
    if insecure_only:
        click.echo(f"{prefix} {pkg['insecure_via'][:16]:<18} {suffix}")
        return
    click.echo(f"{prefix} {suffix}")


def _render_plain_scope_deps(
    scope_label: str,
    installed_packages: list[dict],
    orphaned_packages: list[str],
    ctx: _ScopeDisplayContext,
) -> None:
    _emit_scope_plain_header(scope_label, ctx.insecure_only)
    for pkg in installed_packages:
        _render_plain_scope_row(pkg, ctx.insecure_only)
    _emit_orphaned_packages(ctx.logger, orphaned_packages)


def _show_scope_deps(scope_label, apm_dir, ctx: _ScopeDisplayContext):
    """Display dependencies for a single scope (Project or Global)."""
    installed_packages, orphaned_packages = _resolve_scope_deps(
        apm_dir,
        ctx.logger,
        ctx.insecure_only,
    )
    if installed_packages is None:
        ctx.logger.progress(f"No APM dependencies installed ({scope_label} scope)")
        ctx.logger.verbose_detail("Run 'apm install' to install dependencies from apm.yml")
        return
    if not installed_packages:
        message = (
            f"No insecure APM dependencies installed ({scope_label} scope)"
            if ctx.insecure_only
            else f"apm_modules/ directory exists but contains no valid packages ({scope_label} scope)"
        )
        ctx.logger.progress(message)
        return
    if ctx.has_rich:
        _render_rich_scope_deps(scope_label, installed_packages, orphaned_packages, ctx)
        return
    _render_plain_scope_deps(scope_label, installed_packages, orphaned_packages, ctx)


def tree(global_):
    """Display dependencies in hierarchical tree format using lockfile."""
    logger = CommandLogger("deps-tree")
    ctx = _build_tree_render_context(logger)
    try:
        from ....core.scope import InstallScope, get_apm_dir

        scope = InstallScope.USER if global_ else InstallScope.PROJECT
        tree_data = _build_dep_tree(get_apm_dir(scope))
        _render_tree_output(tree_data, ctx)
    except Exception as e:
        logger.error(f"Error showing dependency tree: {e}")
        sys.exit(1)


def clean(dry_run: bool, yes: bool):
    """Remove entire apm_modules/ directory."""
    logger = CommandLogger("deps-clean")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.progress("No apm_modules/ directory found - already clean")
        return

    # Count actual installed packages (not just top-level dirs like org namespaces or _local)
    from .._utils import _scan_installed_packages

    packages = _scan_installed_packages(apm_modules_path)
    package_count = len(packages)

    if dry_run:
        logger.progress(f"Dry run: would remove apm_modules/ ({package_count} package(s))")
        for pkg in sorted(packages):
            logger.progress(f"  - {pkg}")
        return

    logger.warning(
        f"This will remove the entire apm_modules/ directory ({package_count} package(s))"
    )

    # Confirmation prompt (skip if --yes provided)
    if not yes:
        try:
            from rich.prompt import Confirm

            confirm = Confirm.ask("Continue?")
        except ImportError:
            confirm = click.confirm("Continue?")

        if not confirm:
            logger.progress("Operation cancelled")
            return

    try:
        shutil.rmtree(apm_modules_path)
        logger.success("Successfully removed apm_modules/ directory")
    except Exception as e:
        logger.error(f"Error removing apm_modules/: {e}")
        sys.exit(1)


__all__ = [
    "_build_dep_tree",
    "_resolve_scope_deps",
    "_show_scope_deps",
    "clean",
    "tree",
    "update",
]
