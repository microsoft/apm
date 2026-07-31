"""Main compilation flow helpers extracted from ``compile/cli.py``.

Extracted to keep that module under 800 lines. Contains:
- ``CompilationRunConfig`` -- frozen dataclass grouping compilation options
- ``_run_compilation``     -- main compilation flow (resolves target, compiles,
                              reports results)
- ``_handle_global_flag``  -- --global compilation of user-scope root contexts
- ``_display_user_path``   -- render paths under HOME with tilde prefix

Rule B (monkeypatch safety): any name that tests patch on the *original*
``cli`` module (``AgentsCompiler``, ``CompilationConfig``,
``_resolve_effective_target``, ``_rich_info``) is loaded via a
function-level late import so patches on ``apm_cli.commands.compile.cli.*``
still apply.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from ...constants import AGENTS_MD_FILENAME
from ...utils import perf_stats
from ...utils.console import _rich_panel
from .._helpers import (
    _check_orphaned_packages,
    _rich_blank_line,
)

# ---------------------------------------------------------------------------
# Parameter object
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CompilationRunConfig:
    """Compilation options passed to ``_run_compilation``.

    Groups the nine compilation-specific CLI flags so ``_run_compilation``
    only takes five regular arguments instead of thirteen, satisfying
    PLR0913 without hiding any parameters from callers.
    """

    target: object  # str | list[str] | None
    output: str
    no_links: bool
    chatmode: str | None
    with_constitution: bool
    single_agents: bool
    local_only: bool
    clean: bool
    no_dedup: bool


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _coerce_provenance_targets(value):
    """Coerce a target value to a list of target-name strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t) for t in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return []


def _build_compile_provenance(target, config_target, effective_target, detection_reason):
    """Return ``(provenance_targets, provenance_source)`` for the info line."""
    if detection_reason == "explicit --target flag":
        return _coerce_provenance_targets(target), "--target flag"
    if detection_reason == "apm.yml target":
        return _coerce_provenance_targets(config_target), "apm.yml"
    if isinstance(effective_target, frozenset):
        return sorted(effective_target), f"auto-detect ({detection_reason})"
    if isinstance(effective_target, str):
        return [effective_target], f"auto-detect ({detection_reason})"
    return [], f"auto-detect ({detection_reason})"


def _show_compile_strategy_progress(logger, run_config, config, effective_target, detection_reason):
    """Emit target-aware progress messages before compilation starts."""
    from ...core.target_detection import (
        REASON_NO_TARGET_FOLDER,
        get_target_description,
        should_compile_agents_md,
        should_compile_claude_md,
        should_compile_gemini_md,
    )

    if config.strategy == "distributed" and not run_config.single_agents:
        if isinstance(effective_target, frozenset):
            if isinstance(run_config.target, list):
                _target_label = f"--target {','.join(run_config.target)}"
            elif isinstance(run_config.target, list) or (
                # config_target is not in scope here; re-derive from run_config
                False
            ):
                _target_label = "multi-target"
            else:
                _target_label = "multi-target"

            _parts = []
            if should_compile_agents_md(effective_target):
                _parts.append("AGENTS.md")
            if should_compile_claude_md(effective_target):
                _parts.append("CLAUDE.md")
            if should_compile_gemini_md(effective_target):
                _parts.append("GEMINI.md")
            logger.progress(f"Compiling for {' + '.join(_parts)} ({_target_label})")
        elif (
            isinstance(effective_target, str)
            and effective_target == "vscode"
            and detection_reason == REASON_NO_TARGET_FOLDER
        ):
            logger.progress(f"Compiling for AGENTS.md only ({detection_reason})")
            logger.progress(
                " Create .github/, .claude/, .codex/, .opencode/ or .cursor/ folder"
                " for full integration",
                symbol="light_bulb",
            )
        else:
            description = get_target_description(effective_target)
            logger.progress(f"Compiling for {description} - {detection_reason}")

        if run_config.dry_run if hasattr(run_config, "dry_run") else False:
            logger.dry_run_notice("showing placement without writing files")
    else:
        logger.progress("Using single-file compilation (legacy mode)", symbol="page")


def _check_and_write_output(logger, compiler, config, output_path, final_content):
    """Security-scan and write the final compiled content.

    Returns ``True`` if critical security findings were detected.
    """
    from ...security.gate import WARN_POLICY, SecurityGate

    has_critical = False
    verdict = SecurityGate.scan_text(final_content, str(output_path), policy=WARN_POLICY)
    if verdict.has_findings:
        actionable = verdict.critical_count + verdict.warning_count
        if verdict.has_critical:
            has_critical = True
        if actionable:
            logger.warning(
                f"Compiled output contains {actionable} hidden character(s) "
                f"-- run 'apm audit --file {output_path}' to inspect"
            )
    try:
        # Honour managed_section mode (issue #1764).
        if config.agents_md_mode == "managed_section":
            compiler._write_output_file_with_config(str(output_path), final_content, config)
            if compiler.errors:
                raise OSError(compiler.errors[-1])
        else:
            from ...compilation.output_writer import CompiledOutputWriter

            CompiledOutputWriter().write(output_path, final_content)
    except (OSError, ValueError) as e:
        logger.error(f"Failed to write final AGENTS.md: {e}")
        sys.exit(1)
    return has_critical


def _handle_single_file_success(logger, compiler, config, dry_run, output_str):
    """Handle the single-file compilation success path.

    Returns ``True`` if critical security findings were detected.
    """
    from apm_cli.commands.compile import cli as _c

    has_critical = False

    intermediate_config = dataclasses.replace(config, dry_run=True, strategy="single-file")
    intermediate_result = compiler.compile(intermediate_config)

    if not intermediate_result.success:
        return has_critical

    from ...compilation.injector import ConstitutionInjector

    injector = ConstitutionInjector(base_dir=".")
    output_path = Path(config.output_path)
    final_content, c_status, c_hash = injector.inject(
        intermediate_result.content,
        with_constitution=config.with_constitution,
        output_path=output_path,
    )

    if not dry_run:
        if c_status in ("CREATED", "UPDATED", "MISSING"):
            has_critical = _check_and_write_output(
                logger, compiler, config, output_path, final_content
            )
        else:
            logger.progress("No changes detected; preserving existing AGENTS.md for idempotency")

    if dry_run:
        logger.success(
            "Context compilation completed successfully (dry run)",
            symbol="check",
        )
    else:
        logger.success(f"Context compiled successfully to {output_path}")

    stats = intermediate_result.stats
    _rich_blank_line()
    _c._display_single_file_summary(stats, c_status, c_hash, output_path, dry_run)

    if dry_run:
        preview = final_content[:500] + ("..." if len(final_content) > 500 else "")
        _rich_panel(preview, title=" Generated Content Preview", style="cyan")
    else:
        _c._display_next_steps(output_str)

    return has_critical


def _display_user_path(path: Path) -> str:
    """Render paths under HOME with a stable tilde prefix for CLI output."""
    try:
        rel = path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return str(path)
    return f"~/{rel.as_posix()}"


def _run_watch_mode(
    logger,
    target,
    output: str,
    chatmode,
    no_links: bool,
    dry_run: bool,
    verbose: bool,
    clean: bool,
    source_root: Path | None = None,
) -> None:
    """Set up and run watch mode (``--watch`` flag).

    Resolves the effective compile target using the same logic as the
    one-shot path so that ``targets: [claude, cursor]`` in apm.yml does
    not silently regress on every recompile (#1345), then delegates to
    :func:`_watch_mode`.
    """
    # Late import to stay Rule B safe (tests patch on cli module).
    from apm_cli.commands.compile import cli as _c

    _resolve_effective_target = _c._resolve_effective_target
    _watch_mode_fn = _c._watch_mode  # resolved via cli so test patches are visible

    if clean:
        logger.warning(
            "--clean is ignored in watch mode; run 'apm compile --clean' "
            "separately to remove orphaned outputs."
        )
    effective_target, _detection_reason, config_target = _resolve_effective_target(
        target, source_root=source_root
    )
    _watch_mode_fn(
        output,
        chatmode,
        no_links,
        dry_run,
        verbose=verbose,
        effective_target=effective_target,
        target_label_user=target,
        target_label_config=config_target,
        cli_target=target,
    )


def _handle_global_flag(dry_run: bool, logger) -> int:
    """Handle --global compilation of user-scope root context files.

    Returns 0 on success, 1 on error (for sys.exit).
    """
    from ...compilation import compile_user_root_contexts
    from ...core.scope import InstallScope, get_apm_dir
    from ...integration.targets import KNOWN_TARGETS

    source_root = get_apm_dir(InstallScope.USER)
    apm_modules = source_root / "apm_modules"
    if not apm_modules.is_dir():
        display_path = _display_user_path(apm_modules)
        logger.error(
            f"User-scope apm_modules not found: {display_path}. "
            "Run 'apm install -g <package>' to install packages globally.",
            symbol="error",
        )
        return 1

    results = compile_user_root_contexts(
        list(KNOWN_TARGETS.values()),
        source_root,
        dry_run=dry_run,
        logger=None,
    )

    if not results:
        logger.info(
            "No user-scope targets produced output -- run 'apm install -g <package>' "
            "to add global instructions.",
            symbol="info",
        )
        return 0

    has_error = False
    written_count = 0
    would_write_count = 0
    unchanged_count = 0
    for entry in results:
        status = entry.status
        tname = entry.target
        path = entry.path
        display_path = _display_user_path(path) if path is not None else None
        if status == "written":
            logger.success(f"{tname}: wrote {display_path}", symbol="check")
            written_count += 1
        elif status == "would-write":
            logger.info(f"{tname}: would write {display_path} (dry-run)", symbol="preview")
            would_write_count += 1
        elif status == "unchanged":
            logger.verbose_detail(f"{tname}: unchanged {display_path}")
            unchanged_count += 1
        elif status == "skipped-hand-authored":
            logger.info(f"{tname}: skipped (hand-authored) {display_path}", symbol="info")
        elif status == "skipped-no-instructions":
            logger.verbose_detail(f"{tname}: skipped (no global instructions)")
        elif status.startswith("error:"):
            logger.error(f"{tname}: {status[6:]}", symbol="error")
            has_error = True
        if entry.has_critical_security:
            has_error = True

    if not has_error:
        changed_count = written_count + would_write_count
        if changed_count:
            verb = "Would compile" if dry_run else "Compiled"
            message = f"{verb} {changed_count} user-scope root context file(s)"
            if unchanged_count:
                message += f"; {unchanged_count} unchanged"
            message += "."
            if dry_run:
                logger.info(message, symbol="preview")
            else:
                logger.success(message, symbol="check")
        else:
            logger.info("No user-scope root context files changed.", symbol="info")

    return 1 if has_error else 0


def _handle_distributed_success(logger, result, dry_run, clean=False):
    """Handle the distributed compilation success path.

    Returns ``True`` if critical security findings were detected.
    """
    has_critical = result.has_critical_security

    if dry_run:
        return has_critical

    _files_written = sum(
        int(v or 0)
        for k, v in result.stats.items()
        if k.endswith(("_files_written", "_files_generated"))
    )
    if _files_written > 0:
        logger.success("Compilation completed successfully!", symbol="check")
    elif clean and result.stats.get("claude_empty_due_to_no_primitives"):
        # The compiler already reported the expected cleanup outcome.
        pass
    else:
        logger.warning(
            "Compilation completed but produced no output "
            "files. Check that target directories exist "
            "(e.g. .github/, .claude/) or set 'target:' "
            "in apm.yml / pass --target explicitly."
        )
    return has_critical


# ---------------------------------------------------------------------------
# Main compilation flow
# ---------------------------------------------------------------------------


def _run_compilation(
    logger,
    dry_run: bool,
    verbose: bool,
    source_root: Path | None,
    run_config: CompilationRunConfig,
) -> None:
    """Main compilation flow: target resolution, config, compile, and output.

    Handles both distributed (default) and single-file (``--single-agents``)
    strategies, emits the canonical target-provenance line, runs the
    compiler, reports results, and hard-fails on critical security findings.
    """
    # Late imports for names patched by tests on the original cli module.
    from apm_cli.commands.compile import cli as _c

    AgentsCompiler = _c.AgentsCompiler
    CompilationConfig = _c.CompilationConfig
    _resolve_effective_target = _c._resolve_effective_target
    _rich_info = _c._rich_info

    from ...core.target_detection import ResolvedTargets, format_provenance
    from ...primitives.discovery import clear_discovery_cache

    logger.start("Starting context compilation...", symbol="cogs")

    _src = source_root or Path(".")

    effective_target, detection_reason, config_target = _resolve_effective_target(
        run_config.target, source_root=_src
    )

    # Emit canonical provenance line.
    _provenance_targets, _provenance_source = _build_compile_provenance(
        run_config.target, config_target, effective_target, detection_reason
    )
    if _provenance_targets:
        _rich_info(
            format_provenance(
                ResolvedTargets(
                    targets=sorted(set(_provenance_targets)),
                    source=_provenance_source,
                    auto_create=True,
                )
            ),
            symbol="info",
        )

    # Build compilation config.
    config = CompilationConfig.from_apm_yml(
        output_path=(run_config.output if run_config.output != AGENTS_MD_FILENAME else None),
        chatmode=run_config.chatmode,
        resolve_links=not run_config.no_links if run_config.no_links else None,
        dry_run=dry_run,
        single_agents=run_config.single_agents,
        trace=verbose,
        local_only=run_config.local_only,
        debug=verbose,
        clean_orphaned=run_config.clean,
        target=effective_target,
        no_dedup=run_config.no_dedup,
    )
    config.with_constitution = run_config.with_constitution

    # Show target-aware progress for the chosen strategy.
    if config.strategy == "distributed" and not run_config.single_agents:
        if isinstance(effective_target, frozenset):
            from ...core.target_detection import (
                should_compile_agents_md,
                should_compile_claude_md,
                should_compile_gemini_md,
            )

            if isinstance(run_config.target, list):
                _target_label = f"--target {','.join(run_config.target)}"
            elif isinstance(config_target, list):
                _target_label = f"apm.yml target: [{', '.join(config_target)}]"
            else:
                _target_label = "multi-target"

            _parts = []
            if should_compile_agents_md(effective_target):
                _parts.append("AGENTS.md")
            if should_compile_claude_md(effective_target):
                _parts.append("CLAUDE.md")
            if should_compile_gemini_md(effective_target):
                _parts.append("GEMINI.md")
            logger.progress(f"Compiling for {' + '.join(_parts)} ({_target_label})")
        elif isinstance(effective_target, str) and effective_target == "vscode":
            from ...core.target_detection import REASON_NO_TARGET_FOLDER

            if detection_reason == REASON_NO_TARGET_FOLDER:
                logger.progress(f"Compiling for AGENTS.md only ({detection_reason})")
                logger.progress(
                    " Create .github/, .claude/, .codex/, .opencode/ or .cursor/ folder"
                    " for full integration",
                    symbol="light_bulb",
                )
            else:
                from ...core.target_detection import get_target_description

                description = get_target_description(effective_target)
                logger.progress(f"Compiling for {description} - {detection_reason}")
        else:
            from ...core.target_detection import get_target_description

            description = get_target_description(effective_target)
            logger.progress(f"Compiling for {description} - {detection_reason}")

        if dry_run:
            logger.dry_run_notice("showing placement without writing files")
        if verbose:
            logger.verbose_detail("Verbose mode: showing source attribution and optimizer analysis")
    else:
        logger.progress("Using single-file compilation (legacy mode)", symbol="page")

    # Perform compilation.
    clear_discovery_cache()
    perf_stats.reset()
    compiler = AgentsCompiler(".", source_dir=str(_src))
    result = compiler.compile(config, logger=logger)
    compile_has_critical = result.has_critical_security

    if result.success:
        if config.strategy == "distributed" and not run_config.single_agents:
            compile_has_critical = _handle_distributed_success(
                logger, result, dry_run, clean=run_config.clean
            )
        else:
            single_critical = _handle_single_file_success(
                logger, compiler, config, dry_run, run_config.output
            )
            if single_critical:
                compile_has_critical = True

    # Display warnings and errors for all modes.
    if result.warnings:
        logger.warning(f"Compilation completed with {len(result.warnings)} warning(s):")
        for warning in result.warnings:
            logger.warning(f"  {warning}")

    if result.errors:
        logger.error(f"Compilation failed with {len(result.errors)} errors:")
        for error in result.errors:
            logger.error(f"  {error}")
        sys.exit(1)

    # Check for orphaned packages after successful compilation.
    try:
        orphaned_packages = _check_orphaned_packages()
        if orphaned_packages:
            _rich_blank_line()
            logger.warning(
                f"Found {len(orphaned_packages)} orphaned package(s) that were "
                "included in compilation:"
            )
            for pkg in orphaned_packages:
                logger.progress(f"  * {pkg}")
            logger.progress(" Run 'apm prune' to remove orphaned packages")
    except Exception:
        pass  # Continue if orphan check fails

    # Hard-fail on critical security findings.
    if compile_has_critical:
        logger.error(
            "Compiled output contains critical hidden characters"
            " -- run 'apm audit' to inspect, 'apm audit --strip' to clean"
        )
        perf_stats.render_summary(logger, project_root=str(_src))
        sys.exit(1)

    if result.success and not dry_run:
        from ...install.manifest_reconcile import reconcile_project_deployed_state

        reconcile_project_deployed_state(
            Path(_src).resolve(),
            explicit_target=effective_target,
            deploy_root=Path(".").resolve(),
            lock_root=Path(".").resolve(),
            verbose=verbose,
        )

    perf_stats.render_summary(logger, project_root=str(_src))
