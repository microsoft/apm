"""Finalize phase: emit verbose stats, bare-success fallback, and return result.

Extracted from the trailing block of ``_install_apm_dependencies`` in
``commands/install.py`` (P2.S6).  Faithfully preserves the four separate
``if X > 0:`` stat blocks, the ``if not logger:`` bare-success fallback,
and the unpinned-dependency warning.

``_rich_success`` is resolved through the ``_install_mod`` indirection so
that test patches at ``apm_cli.commands.install._rich_success`` remain
effective.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.results import InstallResult


def _emit_verbose_stats(ctx: InstallContext) -> None:
    if not ctx.logger:
        return

    verbose_stats = (
        (ctx.total_links_resolved, "Resolved {count} context file links"),
        (ctx.total_commands_integrated, "Integrated {count} command(s)"),
        (ctx.total_hooks_integrated, "Integrated {count} hook(s)"),
        (ctx.total_instructions_integrated, "Integrated {count} instruction(s)"),
    )
    for count, template in verbose_stats:
        if count > 0:
            ctx.logger.verbose_detail(template.format(count=count))


def _emit_unpinned_warning(ctx: InstallContext) -> None:
    if not ctx.unpinned_count:
        return

    unpinned_names: list[str] = []
    for installed_package in ctx.installed_packages:
        ref = getattr(installed_package, "dep_ref", None)
        if ref is None or ref.reference:
            continue
        name = getattr(ref, "repo_url", None) or getattr(ref, "local_path", None) or ""
        if name:
            unpinned_names.append(str(name))

    seen: set[str] = set()
    unique_names: list[str] = []
    for name in unpinned_names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    noun = "dependency" if ctx.unpinned_count == 1 else "dependencies"
    if unique_names:
        shown = unique_names[:5]
        suffix = ", ".join(shown)
        extra = len(unique_names) - len(shown)
        if extra > 0:
            suffix += f", and {extra} more"
        ctx.diagnostics.warn(
            f"{ctx.unpinned_count} {noun} unpinned: {suffix} -- add #tag or #sha to prevent drift"
        )
        return

    ctx.diagnostics.warn(
        f"{ctx.unpinned_count} {noun} unpinned -- add #tag or #sha to prevent drift"
    )


def run(ctx: InstallContext) -> InstallResult:
    """Emit verbose stats, fallback success, unpinned warning, and return final result."""
    from apm_cli.commands import install as _install_mod
    from apm_cli.models.results import InstallResult

    _emit_verbose_stats(ctx)

    if not ctx.logger:
        _install_mod._rich_success(f"Installed {ctx.installed_count} APM dependencies")

    _emit_unpinned_warning(ctx)

    return InstallResult(
        ctx.installed_count,
        ctx.total_prompts_integrated,
        ctx.total_agents_integrated,
        ctx.diagnostics,
        package_types=dict(ctx.package_types),
    )
