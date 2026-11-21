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
from typing import TYPE_CHECKING, Any

from apm_cli.core.remediation import ROOT_CONTEXT_COMPILE, USER_ROOT_CONTEXT_COMPILE
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


# apm.lock.yaml already pins the resolved commit; the ref in apm.yml decides
# what 'apm update' moves to. A literal tag pins it; only a semver range
# tracks releases (#3994).
UNPINNED_HINT = (
    "apm.lock.yaml pins the resolved commit; to pin it in apm.yml, declare the "
    "dependency at that commit as shown, or, when it publishes release tags, use a "
    "semver range such as #^1.0.0 to track releases on update "
    "(a tag like #v1.0.0 stays put)"
)


def _unpinned_entries(ctx: Any) -> tuple[list[str], list[str]]:
    """Return (direct, transitive) unpinned remote dependencies, de-duplicated.

    Each entry is the exact apm.yml entry that pins the locked commit. A
    transitive entry also names the package that declares it.
    """
    direct: list[str] = []
    transitive: list[str] = []
    for installed in ctx.installed_packages:
        ref = getattr(installed, "dep_ref", None)
        if ref is None or ref.reference or getattr(ref, "is_local", False) is True:
            continue
        name = getattr(ref, "repo_url", None) or ""
        commit = getattr(installed, "resolved_commit", None)
        if name and isinstance(commit, str) and commit:
            name = f"{ref.to_display_reference()}#{commit}"
        if not name:
            continue
        depth = getattr(installed, "depth", 1)
        parent = getattr(installed, "resolved_by", None)
        if isinstance(depth, int) and depth > 1:
            entry = f"{name} (declared by {parent})" if isinstance(parent, str) else str(name)
            if entry not in transitive:
                transitive.append(entry)
        elif str(name) not in direct:
            direct.append(str(name))
    return direct, transitive


def _name_list(names: list[str]) -> str:
    shown = ", ".join(names[:5])
    extra = len(names) - 5
    return f"{shown}, and {extra} more" if extra > 0 else shown


def _warn_unpinned(ctx: Any) -> None:
    """Warn about unpinned direct dependencies; transitive ones are verbose (#4183).

    The user can pin only what their apm.yml declares. A transitive
    dependency is chosen by the package that declares it, so it is reported
    only in verbose output, naming that package.
    """
    direct, transitive = _unpinned_entries(ctx)
    if direct:
        noun = "dependency" if len(direct) == 1 else "dependencies"
        ctx.diagnostics.warn(
            f"{len(direct)} {noun} unpinned: {_name_list(direct)} -- {UNPINNED_HINT}"
        )
    elif not transitive:
        noun = "dependency" if ctx.unpinned_count == 1 else "dependencies"
        ctx.diagnostics.warn(f"{ctx.unpinned_count} {noun} unpinned -- {UNPINNED_HINT}")
    verbose = bool(getattr(ctx.diagnostics, "verbose", False)) or bool(
        getattr(getattr(ctx, "logger", None), "verbose", False) is True
    )
    if transitive and verbose:
        noun = "dependency" if len(transitive) == 1 else "dependencies"
        ctx.diagnostics.info(
            f"{len(transitive)} transitive {noun} unpinned: {_name_list(transitive)} -- "
            "apm.lock.yaml pins the resolved commit; the declaring package chooses "
            "its version, or declare it in apm.yml at that commit to override"
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
    compile.  Diagnostics are optional at this boundary: the hint must work
    for a context that carries none (unsafe links are then skipped silently).
    """
    apm_modules = ctx.apm_modules_dir or (ctx.project_root / "apm_modules")
    diagnostics = getattr(ctx, "diagnostics", None)
    return any(True for _ in _iter_dep_instruction_files(apm_modules, diagnostics))


def _project_compile_hint(target_names: list[str], families: frozenset[str], verb: str) -> str:
    """Render the single project-scope ``apm compile`` hint line."""
    targets = ", ".join(target_names)
    outputs = " / ".join(_root_context_outputs(families))
    advice = ROOT_CONTEXT_COMPILE.bind(outputs=outputs).sentence()
    return f"Instructions {verb} for {targets}. {advice}"


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
        advice = USER_ROOT_CONTEXT_COMPILE.bind(targets=targets).sentence()
        message = f"Applicable instructions are available. {advice}"
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

    # A converged run changed nothing, so it repeats no unpinned notice (#4184).
    if ctx.unpinned_count and ctx.installed_count:
        _warn_unpinned(ctx)

    # Post-install hints: remind the user to run ``apm compile`` when
    # instructions land in deps but are not written to a per-file rules
    # directory (compile-only targets such as gemini, agents, claude).
    from apm_cli.core.scope import InstallScope

    if ctx.scope is InstallScope.USER:
        _hint_global_root_context(ctx)
    elif ctx.scope is InstallScope.PROJECT:
        _hint_project_compile_needed(ctx)

    _report_kept_overwrites(ctx)

    from apm_cli.install.outcome import result_from_install_context

    return result_from_install_context(ctx)


def _report_kept_overwrites(ctx: InstallContext) -> None:
    """Fail on managed files install kept without deployment evidence (#4360).

    The lockfile already keeps their prior entries; the files written this
    run are recorded, so the lockfile matches the project.
    """
    if not ctx.kept_overwrites:
        return
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.base_integrator import kept_edit_message

    user_scope = ctx.scope is InstallScope.USER
    kinds = getattr(ctx, "kept_overwrite_kinds", {})
    blocking = 0
    for owner, entry in ctx.kept_overwrites:
        kind = kinds.get(entry)
        message = kept_edit_message(entry, user_scope=user_scope, proven_edit=kind is not None)
        if kind == "drift":
            # A user edit with nothing new to deploy: kept and reported, not a failure.
            ctx.diagnostics.warn(message, package=owner)
            continue
        blocking += 1
        ctx.diagnostics.error(message, package=owner)
    if blocking and ctx.logger is not None:
        ctx.logger.kept_overwrites(blocking, ctx.kept_overwrite_written)
