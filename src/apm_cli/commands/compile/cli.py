"""APM compile command CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ...core.target_detection import CompileTargetType

from ...compilation import AgentsCompiler, CompilationConfig
from ...constants import AGENTS_MD_FILENAME
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...utils.console import _rich_panel
from .._helpers import (
    _check_orphaned_packages,
    _rich_blank_line,
)
from ._display import (
    _display_next_steps,
    _display_single_file_summary,
    _display_validation_errors,
    _get_validation_suggestion,
)
from ._preflight import _ensure_compilable_content, _run_validation_mode
from ._target import (
    _CompileStrategyContext,
    _emit_target_provenance,
    _log_compile_strategy,
    _resolve_compile_target,
    _resolve_effective_target,
)
from .watcher import _watch_mode

# Re-export for backward-compatible imports (tests + __init__.py).
__all__ = [
    "_display_validation_errors",
    "_get_validation_suggestion",
    "_resolve_compile_target",
    "compile",
]


def _normalise_compile_target(logger: CommandLogger, raw_target, compile_all: bool):
    """Resolve ``--all`` and deprecated ``--target all`` usage."""
    if compile_all:
        if raw_target is not None:
            logger.error("Cannot use --all together with --target")
            sys.exit(2)
        return "all"
    if (isinstance(raw_target, str) and raw_target == "all") or (
        isinstance(raw_target, list) and "all" in raw_target
    ):
        logger.warning("'--target all' is deprecated; use '--all' instead.")
    return raw_target


def _run_watch_mode(logger: CommandLogger, params) -> bool:
    """Run watch mode when requested and report whether execution ended."""
    if not params["watch"]:
        return False
    _watch_mode(
        params["output"],
        params["chatmode"],
        params["no_links"],
        params["dry_run"],
        verbose=params["verbose"],
    )
    return True


def _build_compile_config(params, effective_target):
    """Build the compilation config from Click parameters."""
    config = CompilationConfig.from_apm_yml(
        output_path=params["output"] if params["output"] != AGENTS_MD_FILENAME else None,
        chatmode=params["chatmode"],
        resolve_links=not params["no_links"] if params["no_links"] else None,
        dry_run=params["dry_run"],
        single_agents=params["single_agents"],
        trace=params["verbose"],
        local_only=params["local_only"],
        debug=params["verbose"],
        clean_orphaned=params["clean"],
        target=effective_target,
    )
    config.with_constitution = params["with_constitution"]
    return config


def _handle_distributed_success(logger: CommandLogger, result, dry_run: bool) -> None:
    """Render distributed compilation success or zero-output warning."""
    if dry_run:
        return
    files_written = sum(
        int(v or 0)
        for k, v in result.stats.items()
        if k.endswith(("_files_written", "_files_generated"))
    )
    if files_written > 0:
        logger.success("Compilation completed successfully!", symbol="check")
        return
    logger.warning(
        "Compilation completed but produced no output files. Check that target directories exist "
        "(e.g. .github/, .claude/) or set 'target:' in apm.yml / pass --target explicitly."
    )


def _scan_and_write_single_file(
    logger, final_content: str, output_path: Path, c_status: str
) -> bool:
    """Scan and write a single-file compile result. Return critical-finding state."""
    if c_status not in ("CREATED", "UPDATED", "MISSING"):
        logger.progress("No changes detected; preserving existing AGENTS.md for idempotency")
        return False
    from ...security.gate import WARN_POLICY, SecurityGate

    compile_has_critical = False
    verdict = SecurityGate.scan_text(final_content, str(output_path), policy=WARN_POLICY)
    if verdict.has_findings:
        actionable = verdict.critical_count + verdict.warning_count
        compile_has_critical = verdict.has_critical
        if actionable:
            logger.warning(
                f"Compiled output contains {actionable} hidden character(s) "
                f"-- run 'apm audit --file {output_path}' to inspect"
            )
    try:
        from ...compilation.output_writer import CompiledOutputWriter

        CompiledOutputWriter().write(output_path, final_content)
    except OSError as e:
        logger.error(f"Failed to write final AGENTS.md: {e}")
        sys.exit(1)
    return compile_has_critical


def _handle_single_file_success(logger, compiler, config, dry_run: bool, output: str) -> bool:
    """Run legacy single-file post-processing and render output."""
    intermediate_config = CompilationConfig(
        output_path=config.output_path,
        chatmode=config.chatmode,
        resolve_links=config.resolve_links,
        dry_run=True,
        with_constitution=config.with_constitution,
        strategy="single-file",
        target=config.target,
    )
    intermediate_result = compiler.compile(intermediate_config)
    if not intermediate_result.success:
        return False
    from ...compilation.injector import ConstitutionInjector

    output_path = Path(config.output_path)
    final_content, c_status, c_hash = ConstitutionInjector(base_dir=".").inject(
        intermediate_result.content,
        with_constitution=config.with_constitution,
        output_path=output_path,
    )
    compile_has_critical = False
    if not dry_run:
        compile_has_critical = _scan_and_write_single_file(
            logger, final_content, output_path, c_status
        )
    if dry_run:
        logger.success("Context compilation completed successfully (dry run)", symbol="check")
    else:
        logger.success(f"Context compiled successfully to {output_path}")
    _rich_blank_line()
    _display_single_file_summary(intermediate_result.stats, c_status, c_hash, output_path, dry_run)
    if dry_run:
        preview = final_content[:500] + ("..." if len(final_content) > 500 else "")
        _rich_panel(preview, title=" Generated Content Preview", style="cyan")
    else:
        _display_next_steps(output)
    return compile_has_critical


def _handle_compile_result(logger, compiler, config, result, params) -> bool:
    """Handle success output for distributed and single-file compile modes."""
    compile_has_critical = result.has_critical_security
    if not result.success:
        return compile_has_critical
    if config.strategy == "distributed" and not params["single_agents"]:
        _handle_distributed_success(logger, result, params["dry_run"])
        return compile_has_critical
    return (
        _handle_single_file_success(
            logger,
            compiler,
            config,
            params["dry_run"],
            params["output"],
        )
        or compile_has_critical
    )


def _report_warnings_errors_and_orphans(logger, result) -> None:
    """Render warnings, errors, and orphan-package diagnostics."""
    if result.warnings:
        logger.warning(f"Compilation completed with {len(result.warnings)} warning(s):")
        for warning in result.warnings:
            logger.warning(f"  {warning}")
    if result.errors:
        logger.error(f"Compilation failed with {len(result.errors)} errors:")
        for error in result.errors:
            logger.error(f"  {error}")
        sys.exit(1)
    try:
        orphaned_packages = _check_orphaned_packages()
        if orphaned_packages:
            _rich_blank_line()
            logger.warning(
                f"Found {len(orphaned_packages)} orphaned package(s) that were included in compilation:"
            )
            for pkg in orphaned_packages:
                logger.progress(f"  * {pkg}")
            logger.progress(" Run 'apm prune' to remove orphaned packages")
    except Exception:
        pass


@click.command(help="Compile APM context into distributed AGENTS.md files")
@click.option(
    "--output",
    "-o",
    default=AGENTS_MD_FILENAME,
    help="Output file path (for single-file mode)",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated). Values: copilot, claude, cursor, opencode, codex, gemini, windsurf, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf (excludes agent-skills); combine with 'agent-skills' for both.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview compilation without writing files (shows placement decisions)",
)
@click.option("--no-links", is_flag=True, help="Skip markdown link resolution")
@click.option("--chatmode", help="Chatmode to prepend to AGENTS.md files")
@click.option("--watch", is_flag=True, help="Auto-regenerate on changes")
@click.option("--validate", is_flag=True, help="Validate primitives without compiling")
@click.option(
    "--with-constitution/--no-constitution",
    default=True,
    show_default=True,
    help="Include Spec Kit constitution block at top if memory/constitution.md present",
)
# Distributed compilation options (Task 7)
@click.option(
    "--single-agents",
    is_flag=True,
    help="Force single-file compilation (legacy mode)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed source attribution and optimizer analysis",
)
@click.option(
    "--local-only",
    is_flag=True,
    help="Ignore dependencies, compile only local primitives",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Remove orphaned AGENTS.md files that are no longer generated",
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
@click.option(
    "--all",
    "compile_all",
    is_flag=True,
    default=False,
    help="Compile for all canonical targets. Equivalent to --target all.",
)
@click.pass_context
def compile(ctx: click.Context, **params: object) -> None:
    """Compile APM context into distributed AGENTS.md files."""
    logger = CommandLogger("compile", verbose=params["verbose"], dry_run=params["dry_run"])
    target = _normalise_compile_target(logger, params["target"], params["compile_all"])

    try:
        _ensure_compilable_content(logger, params["dry_run"])
        if params["validate"]:
            _run_validation_mode(logger)
            return
        if _run_watch_mode(logger, params):
            return

        logger.start("Starting context compilation...", symbol="cogs")
        effective_target, detection_reason, config_target = _resolve_effective_target(target)
        _emit_target_provenance(target, config_target, effective_target, detection_reason)
        config = _build_compile_config(params, effective_target)
        _log_compile_strategy(
            logger,
            config,
            _CompileStrategyContext(
                target=target,
                config_target=config_target,
                effective_target=effective_target,
                detection_reason=detection_reason,
            ),
        )
        if params["dry_run"]:
            logger.dry_run_notice("showing placement without writing files")
        if params["verbose"]:
            logger.verbose_detail("Verbose mode: showing source attribution and optimizer analysis")

        compiler = AgentsCompiler(".")
        result = compiler.compile(config, logger=logger)
        compile_has_critical = _handle_compile_result(logger, compiler, config, result, params)
        _report_warnings_errors_and_orphans(logger, result)
        if compile_has_critical:
            logger.error(
                "Compiled output contains critical hidden characters"
                " -- run 'apm audit' to inspect, 'apm audit --strip' to clean"
            )
            sys.exit(1)
    except ImportError as e:
        logger.error(f"Compilation module not available: {e}")
        logger.progress("This might be a development environment issue.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error during compilation: {e}")
        sys.exit(1)
