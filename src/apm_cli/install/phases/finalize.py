"""Finalize phase: emit verbose stats, bare-success fallback, and return result.

Extracted from the trailing block of ``_install_apm_dependencies`` in
``commands/install.py`` (P2.S6).  Faithfully preserves the four separate
``if X > 0:`` stat blocks, the ``if not logger:`` bare-success fallback,
and the unpinned-dependency warning.

``_rich_success`` comes from the console owner directly; the install layer
does not import the commands layer (#3879).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.utils.console import _rich_success

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.install.context import InstallContext
    from apm_cli.models.results import InstallResult

# compile_family values whose user-scope surface for global instructions can
# require a root context file (AGENTS.md / CLAUDE.md / GEMINI.md).  The excluded
# target names deploy user-scope instructions natively or have no verified
# user-scope root-context reader, so they should not receive the hint.
_ROOT_CONTEXT_ONLY_FAMILIES = frozenset({"agents", "claude", "gemini"})
_ROOT_CONTEXT_HINT_EXCLUDED_TARGETS = frozenset(
    {"antigravity", "copilot", "cursor", "intellij", "kiro", "windsurf"}
)


def _compile_hint_targets(
    targets: Iterable[object], *, user_scope: bool, scoped_instructions_only: bool = False
) -> tuple[list[str], frozenset[str]]:
    """Return display names and compiler families that need root-context hints."""
    target_names: list[str] = []
    families: set[str] = set()
    seen: set[str] = set()
    for target in targets:
        scoped = target.for_scope(user_scope=user_scope)
        if scoped is None:
            continue
        if scoped.name.lower() in _ROOT_CONTEXT_HINT_EXCLUDED_TARGETS:
            continue
        if scoped.compile_family not in _ROOT_CONTEXT_ONLY_FAMILIES:
            continue
        if not user_scope and scoped.compile_family == "claude":
            continue
        if scoped_instructions_only and not scoped.include_scoped_in_user_root_context:
            continue
        families.add(scoped.compile_family)
        if scoped.name not in seen:
            seen.add(scoped.name)
            target_names.append(scoped.name)
    return target_names, frozenset(families)


def _root_context_outputs(families: frozenset[str]) -> list[str]:
    """Return root context filenames produced for compiler families."""
    from apm_cli.core.target_detection import (
        should_compile_agents_md,
        should_compile_claude_md,
        should_compile_gemini_md,
    )

    outputs: list[str] = []
    if should_compile_agents_md(families):
        outputs.append("AGENTS.md")
    if should_compile_claude_md(families):
        outputs.append("CLAUDE.md")
    if should_compile_gemini_md(families):
        outputs.append("GEMINI.md")
    return outputs


def _iter_dep_instruction_files(apm_modules: Path, diagnostics=None) -> Iterator[str]:
    """Yield ``apm_modules``-relative compile-input instruction files.

    Scans ``apm_modules/`` for ``*.instructions.md`` files under any
    ``.apm/instructions/`` subdirectory, which is where ``_copy_local_package``
    and the bundle staging logic place compile-only instruction files.  Links
    that resolve outside ``apm_modules`` are skipped (and reported when
    *diagnostics* is given).  Imports stay lazy to keep the common no-hint
    path lightweight.
    """
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    apm_modules = apm_modules.resolve()
    if not apm_modules.is_dir():
        return

    for found in apm_modules.rglob("*.instructions.md"):
        relative_found = found.relative_to(apm_modules).as_posix()
        try:
            candidate = ensure_path_within(found, apm_modules)
        except PathTraversalError:
            if diagnostics is not None:
                diagnostics.warn(
                    "Ignored unsafe instruction link that resolves outside "
                    f"apm_modules: {relative_found}"
                )
            continue
        if not candidate.is_file():
            continue
        parts = candidate.parts
        try:
            idx = parts.index(".apm")
        except ValueError:
            continue
        if idx + 1 < len(parts) and parts[idx + 1] == "instructions":
            yield relative_found


def _has_dep_instruction_files(ctx: InstallContext) -> bool:
    """Return True if any installed dep directory contains ``.instructions.md`` files.

    The all-modules scan is intentionally conservative: after any successful
    install, every dependency instruction must be represented in the next
    compile.
    """
    apm_modules = ctx.apm_modules_dir or (ctx.project_root / "apm_modules")
    return any(True for _ in _iter_dep_instruction_files(apm_modules, ctx.diagnostics))


def _project_compile_hint(target_names: list[str], families: frozenset[str], verb: str) -> str:
    """Render the single project-scope ``apm compile`` hint line."""
    targets = ", ".join(target_names)
    outputs = " / ".join(_root_context_outputs(families))
    return f"Instructions {verb} for {targets}. Run 'apm compile' to update {outputs}."


def _hint_project_compile_needed(ctx: InstallContext) -> None:
    """Hint to run ``apm compile`` after installing packages with compile-only instructions.

    Fires on project-scope installs (not global) when BOTH conditions hold:

    1. At least one active target stores instructions via the compile pipeline
       (``compile_family`` is in :data:`_ROOT_CONTEXT_ONLY_FAMILIES`, e.g. gemini,
       agents, claude) -- i.e. instructions are NOT written to a per-file rules
       directory during ``apm install``.
    2. At least one installed dependency directory contains ``.instructions.md``
       files under its ``.apm/instructions/`` subtree.

    No file is written.  Compilation stays explicit: the user runs
    ``apm compile`` to materialise AGENTS.md / GEMINI.md / CLAUDE.md.
    """
    if ctx.dry_run:
        return
    if ctx.installed_count == 0:
        return

    target_names, families = _compile_hint_targets(ctx.targets, user_scope=False)
    if not target_names:
        return

    if not _has_dep_instruction_files(ctx):
        return

    if ctx.logger:
        ctx.logger.info(_project_compile_hint(target_names, families, "installed"), symbol="info")


@dataclass(frozen=True)
class CompileInputSnapshot:
    """Compile-input instructions present before a project-scope removal."""

    target_names: tuple[str, ...] = ()
    families: frozenset[str] = frozenset()
    instruction_files: frozenset[str] = frozenset()


def snapshot_compile_inputs(
    project_root: Path, explicit_target: str | list[str] | None, apm_modules: Path
) -> CompileInputSnapshot:
    """Record compile-input instructions before ``apm uninstall`` / ``apm prune``.

    Targets come from the same declaration ``apm install`` resolves.  The scan
    is skipped when no active target compiles root context files, so the
    common native-rules path stays cheap.  Never raises: a hint must not break
    the removal it follows.
    """
    from apm_cli.integration.targets import resolve_targets

    try:
        targets = resolve_targets(
            project_root, explicit_target=explicit_target, create_config=False
        )
    except Exception:
        return CompileInputSnapshot()
    target_names, families = _compile_hint_targets(targets, user_scope=False)
    if not target_names:
        return CompileInputSnapshot()
    return CompileInputSnapshot(
        tuple(target_names), families, frozenset(_iter_dep_instruction_files(apm_modules))
    )


def hint_compile_after_removal(
    snapshot: CompileInputSnapshot, apm_modules: Path, logger: CommandLogger
) -> None:
    """Hint to run ``apm compile`` when a removal dropped compile-input instructions.

    The compiled root context files (AGENTS.md / GEMINI.md) still carry the
    removed instructions until the next compile.  Compilation stays explicit:
    no file is written or recompiled here.
    """
    if not snapshot.instruction_files:
        return
    if snapshot.instruction_files <= frozenset(_iter_dep_instruction_files(apm_modules)):
        return
    message = _project_compile_hint(list(snapshot.target_names), snapshot.families, "removed")
    logger.info(message, symbol="info")


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
    include_scoped = any(
        scoped is not None and scoped.include_scoped_in_user_root_context
        for target in ctx.targets
        if (scoped := target.for_scope(user_scope=True)) is not None
    )
    instructions = discover_global_instructions(source_root, include_scoped=include_scoped)
    if not instructions:
        return

    target_names, _ = _compile_hint_targets(
        ctx.targets,
        user_scope=True,
        scoped_instructions_only=not any(not instruction.apply_to for instruction in instructions),
    )
    if not target_names:
        return

    if ctx.logger:
        targets = ", ".join(target_names)
        message = (
            "Applicable instructions are available. Run 'apm compile -g' "
            f"to update root context files for: {targets}."
        )
        ctx.logger.info(message, symbol="info")


def run(ctx: InstallContext) -> InstallResult:
    """Emit verbose stats, fallback success, unpinned warning, and return final result."""
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
        _rich_success(f"Installed {ctx.installed_count} APM dependencies")

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

    # Post-install hints: remind the user to run ``apm compile`` when
    # instructions land in deps but are not written to a per-file rules
    # directory (compile-only targets such as gemini, agents, claude).
    from apm_cli.core.scope import InstallScope

    if ctx.scope is InstallScope.USER:
        _hint_global_root_context(ctx)
    elif ctx.scope is InstallScope.PROJECT:
        _hint_project_compile_needed(ctx)

    from apm_cli.install.outcome import result_from_install_context

    return result_from_install_context(ctx)
