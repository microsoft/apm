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


def run(ctx: InstallContext) -> InstallResult:
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
            ctx.logger.verbose_detail(
                f"Integrated {ctx.total_instructions_integrated} instruction(s)"
            )

    # Summary is now emitted by the caller via logger.install_summary()
    if not ctx.logger:
        _install_mod._rich_success(f"Installed {ctx.installed_count} APM dependencies")

    if ctx.unpinned_count:
        # Enumerate names of unpinned deps so the user knows which to pin.
        # Cap at 5 names then "and M more"; fall back to count-only if names
        # cannot be derived.
        _unpinned_names: list[str] = []
        for _ip in ctx.installed_packages:
            _ref = getattr(_ip, "dep_ref", None)
            if _ref is None or _ref.reference:
                continue
            _name = getattr(_ref, "repo_url", None) or getattr(_ref, "local_path", None) or ""
            if _name:
                _unpinned_names.append(str(_name))
        # De-dupe while preserving order.
        _seen: set[str] = set()
        _unique_names: list[str] = []
        for _n in _unpinned_names:
            if _n not in _seen:
                _seen.add(_n)
                _unique_names.append(_n)

        noun = "dependency" if ctx.unpinned_count == 1 else "dependencies"
        if _unique_names:
            _shown = _unique_names[:5]
            _suffix = ", ".join(_shown)
            _extra = len(_unique_names) - len(_shown)
            if _extra > 0:
                _suffix += f", and {_extra} more"
            ctx.diagnostics.warn(
                f"{ctx.unpinned_count} {noun} unpinned: {_suffix} "
                "-- add #tag or #sha to prevent drift"
            )
        else:
            ctx.diagnostics.warn(
                f"{ctx.unpinned_count} {noun} unpinned -- add #tag or #sha to prevent drift"
            )

    return InstallResult(
        ctx.installed_count,
        ctx.total_prompts_integrated,
        ctx.total_agents_integrated,
        ctx.diagnostics,
        package_types=dict(ctx.package_types),
    )
