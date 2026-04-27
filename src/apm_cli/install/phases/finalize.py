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


def run(ctx: "InstallContext") -> "InstallResult":
    """Emit verbose stats, fallback success, unpinned warning, and return final result."""
    from apm_cli.commands import install as _install_mod
    from apm_cli.models.results import InstallResult

    # Show integration stats (verbose-only when logger is available)
    if ctx.total_links_resolved > 0:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Resolved {ctx.total_links_resolved} context file links")

    if ctx.total_commands_integrated > 0:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Integrated {ctx.total_commands_integrated} command(s)")

    if ctx.total_hooks_integrated > 0:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Integrated {ctx.total_hooks_integrated} hook(s)")

    if ctx.total_instructions_integrated > 0:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Integrated {ctx.total_instructions_integrated} instruction(s)")

    # Summary is now emitted by the caller via logger.install_summary()
    if not ctx.logger:
        _install_mod._rich_success(f"Installed {ctx.installed_count} APM dependencies")

    if ctx.unpinned_count:
        noun = "dependency has" if ctx.unpinned_count == 1 else "dependencies have"
        ctx.diagnostics.info(
            f"{ctx.unpinned_count} {noun} no pinned version "
            f"-- pin with #tag or #sha to prevent drift"
        )

    return InstallResult(ctx.installed_count, ctx.total_prompts_integrated, ctx.total_agents_integrated, ctx.diagnostics, package_types=dict(ctx.package_types))
