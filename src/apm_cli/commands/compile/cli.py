"""APM compile command CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ...core.target_detection import CompileTargetType

from ...compilation import AgentsCompiler, CompilationConfig
from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_MODULES_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...primitives.discovery import discover_primitives
from ...utils.console import (
    _rich_error,
    _rich_info,
    _rich_panel,
)
from .._helpers import (
    _check_orphaned_packages,
    _get_console,
    _rich_blank_line,
)
from .watcher import _watch_mode


def _display_single_file_summary(stats, c_status, c_hash, output_path, dry_run):
    """Display compilation summary table for single-file mode."""
    try:
        console = _get_console()
        if not console:
            _rich_info(f"Processed {stats.get('primitives_found', 0)} primitives:")
            _rich_info(f"  * {stats.get('instructions', 0)} instructions")
            _rich_info(f"  * {stats.get('contexts', 0)} contexts")
            _rich_info(f"Constitution status: {c_status} hash={c_hash or '-'}")
            return

        import os

        from rich.table import Table

        table = Table(
            title="Compilation Summary",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Component", style="bold white", min_width=15)
        table.add_column("Count", style="cyan", min_width=8)
        table.add_column("Details", style="white", min_width=20)

        constitution_details = f"Hash: {c_hash or '-'}"
        table.add_row("Spec-kit Constitution", c_status, constitution_details)

        table.add_row(
            "Instructions",
            str(stats.get("instructions", 0)),
            "[+] All validated",
        )
        table.add_row(
            "Contexts",
            str(stats.get("contexts", 0)),
            "[+] All validated",
        )
        table.add_row(
            "Chatmodes",
            str(stats.get("chatmodes", 0)),
            "[+] All validated",
        )

        try:
            file_size = os.path.getsize(output_path) if not dry_run else 0
            size_str = f"{file_size / 1024:.1f}KB" if file_size > 0 else "Preview"
            output_details = f"{output_path.name} ({size_str})"
        except Exception:
            output_details = f"{output_path.name}"

        table.add_row("Output", "* SUCCESS", output_details)
        console.print(table)
    except Exception:
        _rich_info(f"Processed {stats.get('primitives_found', 0)} primitives:")
        _rich_info(f"  * {stats.get('instructions', 0)} instructions")
        _rich_info(f"  * {stats.get('contexts', 0)} contexts")
        _rich_info(f"Constitution status: {c_status} hash={c_hash or '-'}")


def _display_next_steps(output):
    """Display next steps panel after successful single-file compilation."""
    next_steps = [
        f"Review the generated {output} file",
        "Install MCP dependencies: apm install",
        "Execute agentic workflows: apm run <script> --param key=value",
    ]
    try:
        console = _get_console()
        if console:
            from rich.panel import Panel

            steps_content = "\n".join(f"* {step}" for step in next_steps)
            console.print(Panel(steps_content, title=" Next Steps", border_style="blue"))
        else:
            _rich_info("Next steps:")
            for step in next_steps:
                click.echo(f"  * {step}")
    except (ImportError, NameError):
        _rich_info("Next steps:")
        for step in next_steps:
            click.echo(f"  * {step}")


def _display_validation_errors(errors):
    """Display validation errors in a Rich table with actionable feedback."""
    try:
        console = _get_console()
        if console:
            from rich.table import Table

            error_table = Table(
                title="[x] Primitive Validation Errors",
                show_header=True,
                header_style="bold red",
            )
            error_table.add_column("File", style="bold red", min_width=20)
            error_table.add_column("Error", style="white", min_width=30)
            error_table.add_column("Suggestion", style="yellow", min_width=25)

            for error in errors:
                file_path = str(error) if hasattr(error, "__str__") else "Unknown"
                # Extract file path from error string if it contains file info
                if ":" in file_path:
                    parts = file_path.split(":", 1)
                    file_name = parts[0] if len(parts) > 1 else "Unknown"
                    error_msg = parts[1].strip() if len(parts) > 1 else file_path
                else:
                    file_name = "Unknown"
                    error_msg = file_path

                # Provide actionable suggestions based on error type
                suggestion = _get_validation_suggestion(error_msg)
                error_table.add_row(file_name, error_msg, suggestion)

            console.print(error_table)
            return

    except (ImportError, NameError):
        pass

    # Fallback to simple text output
    _rich_error("Validation errors found:")
    for error in errors:
        click.echo(f"  [x] {error}")


def _get_validation_suggestion(error_msg):
    """Get actionable suggestions for validation errors."""
    if "Missing 'description'" in error_msg:
        return "Add 'description: Your description here' to frontmatter"
    elif "applyTo" in error_msg and "globally" in error_msg:
        return "Add 'applyTo: \"**/*.py\"' to scope the instruction, or leave as-is for global"
    elif "Empty content" in error_msg:
        return "Add markdown content below the frontmatter"
    else:
        return "Check primitive structure and frontmatter"


def _resolve_compile_target(target):
    """Map CLI target input to a compiler-understood target.

    The compiler understands single-string targets (``"vscode"``,
    ``"claude"``, ``"gemini"``, ``"all"``) and ``frozenset`` targets
    containing compiler-family names (``"agents"``, ``"claude"``,
    ``"gemini"``).

    Multi-target lists are mapped to the narrowest representation:
    a single string when only one compiler family is needed, or a
    ``frozenset`` of families when multiple are needed.  This avoids
    collapsing to ``"all"`` (which would incorrectly generate files
    for every family).

    Family resolution reads ``TargetProfile.compile_family`` from
    ``KNOWN_TARGETS`` so adding a new compile-eligible target only
    requires populating that field.  The CLI alias ``"vscode"`` is
    treated as ``"copilot"`` for this purpose.

    Args:
        target: A single target string, a list of target strings, or ``None``.

    Returns:
        A single string, a ``frozenset`` of compiler families, or ``None``.
    """
    from ...integration.targets import KNOWN_TARGETS

    if target is None:
        return None  # will trigger detect_target() auto-detection
    if isinstance(target, list):
        target_set = set(target)
        # Strip targets with no compile output (compile_family is None);
        # they would silently fall through the family resolution otherwise.
        # ``vscode`` is a CLI alias for ``copilot`` and shares its profile.
        skip = {name for name, profile in KNOWN_TARGETS.items() if profile.compile_family is None}
        target_set -= skip
        if not target_set:
            # Solo agent-skills (or another no-compile target) in a list --
            # pass through as a string so the compiler's no-op path fires.
            for sentinel in target:
                if sentinel in skip:
                    return sentinel
            return None

        # The "vscode" family handles copilot AND emits AGENTS.md as a
        # bonus; the "agents" family emits AGENTS.md only.  When both
        # appear in a multi-target compile we still need both family
        # tokens so the agents compiler routes correctly.
        def _family_of(name: str) -> str | None:
            if name == "vscode":
                return "vscode"
            profile = KNOWN_TARGETS.get(name)
            return profile.compile_family if profile else None

        families: set[str] = set()
        for name in target_set:
            family = _family_of(name)
            if family is None:
                continue
            families.add(family)
            if family == "vscode":
                # copilot also emits AGENTS.md; mirror legacy behavior.
                families.add("agents")

        if len(families) >= 2:
            # Single-target copilot collapses {"vscode","agents"} to bare
            # "vscode" for routing parity with single-string -t copilot.
            if families == {"vscode", "agents"}:
                return "vscode"
            return frozenset(families)
        if "claude" in families:
            return "claude"
        if "gemini" in families:
            return "gemini"
        if "vscode" in families:
            return "vscode"
        # Bare agents-family target: preserve the original target name so
        # single-element list routing matches single-string semantics
        # (-t cursor and -t [cursor] both end up as "cursor").  Iterate
        # KNOWN_TARGETS in insertion order so priority ties (e.g.
        # ["opencode","codex"]) resolve deterministically to the
        # earliest-registered target.  Adding a new agents-family
        # target (e.g. zed, cline) costs zero edits here -- it inherits
        # whatever priority position it occupies in the registry.
        for name, profile in KNOWN_TARGETS.items():
            if profile.compile_family == "agents" and name in target_set:
                return name
        return "vscode"  # defensive fallback (unreachable)
    return target  # single string pass-through


def _ensure_compilable_content(logger: CommandLogger, dry_run: bool) -> None:
    """Validate that the current project has content worth compiling."""
    from ...compilation.constitution import find_constitution

    if not Path(APM_YML_FILENAME).exists():
        logger.error("Not an APM project - no apm.yml found")
        logger.progress(" To initialize an APM project, run:")
        logger.progress("   apm init")
        sys.exit(1)

    apm_modules_exists = Path(APM_MODULES_DIR).exists()
    constitution_exists = find_constitution(Path(".")).exists()
    apm_dir = Path(APM_DIR)
    local_apm_has_content = apm_dir.exists() and (
        any(apm_dir.rglob("*.instructions.md")) or any(apm_dir.rglob("*.chatmode.md"))
    )
    if apm_modules_exists or local_apm_has_content or constitution_exists:
        return

    has_empty_apm = (
        apm_dir.exists()
        and not any(apm_dir.rglob("*.instructions.md"))
        and not any(apm_dir.rglob("*.chatmode.md"))
    )
    if has_empty_apm:
        logger.error("No instruction files found in .apm/ directory")
        logger.progress(" To add instructions, create files like:")
        logger.progress("   .apm/instructions/coding-standards.instructions.md")
        logger.progress("   .apm/chatmodes/backend-engineer.chatmode.md")
    else:
        logger.error("No APM content found to compile")
        logger.progress(" To get started:")
        logger.progress("   1. Install APM dependencies: apm install <owner>/<repo>")
        logger.progress("   2. Or create local instructions: mkdir -p .apm/instructions")
        logger.progress("   3. Then create .instructions.md or .chatmode.md files")
    if not dry_run:
        sys.exit(1)


def _run_validation_mode(logger: CommandLogger) -> None:
    """Run validation-only mode and exit the command."""
    logger.start("Validating APM context...", symbol="gear")
    compiler = AgentsCompiler(".")
    try:
        primitives = discover_primitives(".")
    except Exception as e:
        logger.error(f"Failed to discover primitives: {e}")
        logger.progress(f" Error details: {type(e).__name__}")
        sys.exit(1)
    validation_errors = compiler.validate_primitives(primitives)
    if validation_errors:
        _display_validation_errors(validation_errors)
        logger.error(f"Validation failed with {len(validation_errors)} errors")
        sys.exit(1)
    logger.success("All primitives validated successfully!")
    logger.progress(f"Validated {primitives.count()} primitives:")
    logger.progress(f"  * {len(primitives.chatmodes)} chatmodes")
    logger.progress(f"  * {len(primitives.instructions)} instructions")
    logger.progress(f"  * {len(primitives.contexts)} contexts")
    try:
        from ...models.apm_package import APMPackage

        mcp_count = len(APMPackage.from_apm_yml(Path(APM_YML_FILENAME)).get_mcp_dependencies())
        if mcp_count > 0:
            logger.progress(f"  * {mcp_count} MCP dependencies")
    except Exception:
        pass


def _load_config_target(apm_yml_path: Path):
    """Load target or targets from apm.yml."""
    from ...models.apm_package import APMPackage

    if not apm_yml_path.exists():
        return None
    apm_pkg = APMPackage.from_apm_yml(apm_yml_path)
    if apm_pkg.target is not None:
        return apm_pkg.target
    try:
        from ...core.apm_yml import parse_targets_field
        from ...utils.yaml_io import load_yaml

        raw = load_yaml(apm_yml_path)
        if not isinstance(raw, dict):
            return None
        yaml_targets = parse_targets_field(raw)
        if not yaml_targets:
            return None
        return yaml_targets[0] if len(yaml_targets) == 1 else yaml_targets
    except Exception:
        return None


def _resolve_effective_target(target):
    """Resolve CLI/config target input to the compiler target and reason."""
    from ...core.target_detection import detect_target

    config_target = _load_config_target(Path(APM_YML_FILENAME))
    compile_target = _resolve_compile_target(target)
    compile_config_target = _resolve_compile_target(config_target)
    if isinstance(compile_target, frozenset):
        return compile_target, "explicit --target flag", config_target
    if isinstance(compile_config_target, frozenset) and compile_target is None:
        return compile_config_target, "apm.yml target", config_target
    detected_target, detection_reason = detect_target(
        project_root=Path("."),
        explicit_target=compile_target,
        config_target=compile_config_target if isinstance(compile_config_target, str) else None,
    )
    return detected_target, detection_reason, config_target


def _coerce_provenance_targets(value):
    """Coerce target provenance input to a list of target labels."""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t) for t in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return []


def _emit_target_provenance(target, config_target, effective_target, detection_reason) -> None:
    """Emit the canonical target provenance line."""
    from ...core.target_detection import ResolvedTargets, format_provenance
    from ...utils.console import _rich_info

    if detection_reason == "explicit --target flag":
        provenance_targets = _coerce_provenance_targets(target)
        provenance_source = "--target flag"
    elif detection_reason == "apm.yml target":
        provenance_targets = _coerce_provenance_targets(config_target)
        provenance_source = "apm.yml"
    else:
        provenance_targets = _coerce_provenance_targets(effective_target)
        provenance_source = f"auto-detect ({detection_reason})"
    if provenance_targets:
        _rich_info(
            format_provenance(
                ResolvedTargets(
                    targets=sorted(set(provenance_targets)),
                    source=provenance_source,
                    auto_create=True,
                )
            ),
            symbol="info",
        )


def _log_compile_strategy(
    logger, config, target, config_target, effective_target, detection_reason
) -> None:
    """Render the target-aware compilation mode line."""
    from ...core.target_detection import (
        REASON_NO_TARGET_FOLDER,
        get_target_description,
        should_compile_agents_md,
        should_compile_claude_md,
        should_compile_gemini_md,
    )

    if config.strategy != "distributed" or config.single_agents:
        logger.progress("Using single-file compilation (legacy mode)", symbol="page")
        return
    if isinstance(effective_target, frozenset):
        if isinstance(target, list):
            target_label = f"--target {','.join(target)}"
        elif isinstance(config_target, list):
            target_label = f"apm.yml target: [{', '.join(config_target)}]"
        else:
            target_label = "multi-target"
        parts = []
        if should_compile_agents_md(effective_target):
            parts.append("AGENTS.md")
        if should_compile_claude_md(effective_target):
            parts.append("CLAUDE.md")
        if should_compile_gemini_md(effective_target):
            parts.append("GEMINI.md")
        logger.progress(f"Compiling for {' + '.join(parts)} ({target_label})")
        return
    if (
        isinstance(effective_target, str)
        and effective_target == "vscode"
        and detection_reason == REASON_NO_TARGET_FOLDER
    ):
        logger.progress(f"Compiling for AGENTS.md only ({detection_reason})")
        logger.progress(
            " Create .github/, .claude/, .codex/, .opencode/ or .cursor/ folder for full integration",
            symbol="light_bulb",
        )
        return
    logger.progress(
        f"Compiling for {get_target_description(effective_target)} - {detection_reason}"
    )


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
    target = params["target"]
    if params["compile_all"]:
        if target is not None:
            logger.error("Cannot use --all together with --target")
            sys.exit(2)
        target = "all"
    elif (isinstance(target, str) and target == "all") or (
        isinstance(target, list) and "all" in target
    ):
        logger.warning("'--target all' is deprecated; use '--all' instead.")

    try:
        _ensure_compilable_content(logger, params["dry_run"])
        if params["validate"]:
            _run_validation_mode(logger)
            return
        if params["watch"]:
            _watch_mode(
                params["output"],
                params["chatmode"],
                params["no_links"],
                params["dry_run"],
                verbose=params["verbose"],
            )
            return

        logger.start("Starting context compilation...", symbol="cogs")
        effective_target, detection_reason, config_target = _resolve_effective_target(target)
        _emit_target_provenance(target, config_target, effective_target, detection_reason)
        config = _build_compile_config(params, effective_target)
        _log_compile_strategy(
            logger, config, target, config_target, effective_target, detection_reason
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
