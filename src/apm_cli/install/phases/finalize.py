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

# compile_family values whose user-scope surface for global instructions can
# require a root context file (AGENTS.md / CLAUDE.md / GEMINI.md).  The excluded
# target names deploy user-scope instructions natively or have no verified
# user-scope root-context reader, so they should not receive the hint.
_ROOT_CONTEXT_ONLY_FAMILIES = frozenset({"agents", "claude", "gemini"})
_ROOT_CONTEXT_HINT_EXCLUDED_TARGETS = frozenset(
    {"antigravity", "copilot", "cursor", "kiro", "windsurf"}
)


def _hint_global_root_context(ctx: InstallContext) -> None:
    """Print a one-line hint pointing at ``apm compile -g`` after ``install -g``.

    The hint is emitted only when BOTH conditions hold:

    1. At least one global (apply_to-less) instruction was installed under the
       user-scope ``apm_modules`` tree.
    2. At least one active target is root-context-only -- its user-scope
       ``compile_family`` is in :data:`_ROOT_CONTEXT_ONLY_FAMILIES`.

    No file is written.  Compilation stays explicit: the user runs
    ``apm compile -g`` to materialise the root context files.  Imports are
    kept lazy to avoid pulling the compilation package into the hot install
    path and to prevent import cycles.
    """
    if ctx.dry_run:
        return

    from apm_cli.compilation.user_root_context import discover_global_instructions
    from apm_cli.core.scope import InstallScope, get_apm_dir

    source_root = get_apm_dir(InstallScope.USER)
    if not discover_global_instructions(source_root):
        return

    target_names: list[str] = []
    seen: set[str] = set()
    for target in ctx.targets:
        scoped = target.for_scope(user_scope=True)
        if scoped is None:
            continue
        if scoped.name.lower() in _ROOT_CONTEXT_HINT_EXCLUDED_TARGETS:
            continue
        if scoped.compile_family not in _ROOT_CONTEXT_ONLY_FAMILIES:
            continue
        if scoped.name not in seen:
            seen.add(scoped.name)
            target_names.append(scoped.name)

    if not target_names:
        return

    if ctx.logger:
        targets = ", ".join(target_names)
        message = (
            "Global instructions installed. Run 'apm compile -g' "
            f"to update root context files for: {targets}."
        )
        ctx.logger.info(message, symbol="info")


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

    # User-scope post-install: when global instructions land on a
    # root-context-only target, print a one-line hint pointing at
    # ``apm compile -g``.  No context file is written on install --
    # compilation stays explicit.
    from apm_cli.core.scope import InstallScope

    if ctx.scope is InstallScope.USER:
        _hint_global_root_context(ctx)

    return InstallResult(
        ctx.installed_count,
        ctx.total_prompts_integrated,
        ctx.total_agents_integrated,
        ctx.diagnostics,
        package_types=dict(ctx.package_types),
    )
