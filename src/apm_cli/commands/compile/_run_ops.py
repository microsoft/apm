"""Main compilation flow helpers extracted from ``compile/cli.py``.

Extracted to keep that module under 800 lines. Contains:
- ``CompilationRunConfig`` -- frozen dataclass grouping compilation options
- ``_run_compilation``     -- main compilation flow (resolves target, compiles,
                              reports results)

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


def _handle_distributed_success(logger, result, dry_run):
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
            compile_has_critical = _handle_distributed_success(logger, result, dry_run)
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

    perf_stats.render_summary(logger, project_root=str(_src))
