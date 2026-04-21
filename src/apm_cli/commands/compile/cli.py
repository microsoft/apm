"""APM compile command CLI."""

import sys
from pathlib import Path

import click

from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_MODULES_DIR, APM_YML_FILENAME
from ...compilation import AgentsCompiler, CompilationConfig
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...primitives.discovery import discover_primitives
from ...utils.console import (
    _rich_error,
    _rich_info,
    _rich_panel,
)
from .._helpers import (
    _atomic_write,
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
            size_str = f"{file_size/1024:.1f}KB" if file_size > 0 else "Preview"
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
            console.print(
                Panel(steps_content, title=" Next Steps", border_style="blue")
            )
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
    """Map CLI target input to compiler-understood target string.

    The compiler only understands ``"vscode"``, ``"claude"``, and ``"all"``.
    Multi-target lists are mapped to the narrowest equivalent.

    Args:
        target: A single target string, a list of target strings, or ``None``.

    Returns:
        A single string (or ``None``) suitable for :func:`detect_target`.
    """
    if target is None:
        return None  # will trigger detect_target() auto-detection
    if isinstance(target, list):
        target_set = set(target)
        # Any target that produces AGENTS.md (copilot/vscode/agents/cursor/opencode/codex)
        has_agents_family = bool(
            target_set & {"copilot", "vscode", "agents", "cursor", "opencode", "codex"}
        )
        has_claude = "claude" in target_set
        if has_agents_family and has_claude:
            return "all"
        elif has_claude:
            return "claude"
        else:
            return "vscode"  # agents-family only
    return target  # single string pass-through


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
    help="Target platform (comma-separated for multiple, e.g. claude,copilot). Use 'all' for every target. Auto-detects if not specified.",
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
@click.pass_context
def compile(
    ctx,
    output,
    target,
    dry_run,
    no_links,
    chatmode,
    watch,
    validate,
    with_constitution,
    single_agents,
    verbose,
    local_only,
    clean,
):
    """Compile APM context into distributed AGENTS.md files.

    By default, uses distributed compilation to generate multiple focused AGENTS.md
    files across your directory structure following the Minimal Context Principle.

    Use --single-agents for traditional single-file compilation when needed.

    Target platforms:
    * vscode/agents: Generates AGENTS.md + .github/ structure (VSCode/GitHub Copilot)
    * claude: Generates CLAUDE.md + .claude/ structure (Claude Code)
    * all: Generates both targets (default)

    Advanced options:
    * --dry-run: Preview compilation without writing files (shows placement decisions)
    * --verbose: Show detailed source attribution and optimizer analysis
    * --local-only: Ignore dependencies, compile only local .apm/ primitives
    * --clean: Remove orphaned AGENTS.md files that are no longer generated
    """
    logger = CommandLogger("compile", verbose=verbose, dry_run=dry_run)

    try:
        # Check if this is an APM project first
        from pathlib import Path

        if not Path(APM_YML_FILENAME).exists():
            logger.error("Not an APM project - no apm.yml found")
            logger.progress(" To initialize an APM project, run:")
            logger.progress("   apm init")
            sys.exit(1)

        # Check if there are any instruction files to compile
        from ...compilation.constitution import find_constitution

        apm_modules_exists = Path(APM_MODULES_DIR).exists()
        constitution_exists = find_constitution(Path(".")).exists()

        # Check if .apm directory has actual content
        apm_dir = Path(APM_DIR)
        local_apm_has_content = apm_dir.exists() and (
            any(apm_dir.rglob("*.instructions.md"))
            or any(apm_dir.rglob("*.chatmode.md"))
        )

        # If no primitive sources exist, check deeper to provide better feedback
        if (
            not apm_modules_exists
            and not local_apm_has_content
            and not constitution_exists
        ):
            # Check if .apm directories exist but are empty
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
                logger.progress(
                    "   2. Or create local instructions: mkdir -p .apm/instructions"
                )
                logger.progress("   3. Then create .instructions.md or .chatmode.md files")

            if not dry_run:  # Don't exit on dry-run to allow testing
                sys.exit(1)

        # Validation-only mode
        if validate:
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
            # Show MCP dependency validation count
            try:
                from ...models.apm_package import APMPackage
                apm_pkg = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
                mcp_count = len(apm_pkg.get_mcp_dependencies())
                if mcp_count > 0:
                    logger.progress(f"  * {mcp_count} MCP dependencies")
            except Exception:
                pass
            return

        # Watch mode
        if watch:
            _watch_mode(output, chatmode, no_links, dry_run, verbose=verbose)
            return

        logger.start("Starting context compilation...", symbol="cogs")

        # Auto-detect target if not explicitly provided
        from ...core.target_detection import detect_target, get_target_description

        # Get config target from apm.yml if available
        config_target = None
        try:
            from ...models.apm_package import APMPackage

            apm_pkg = APMPackage.from_apm_yml(Path(APM_YML_FILENAME))
            config_target = apm_pkg.target
        except Exception:
            # No apm.yml or parsing error - proceed with auto-detection
            pass

        # Resolve list targets to compiler-understood string
        compile_target = _resolve_compile_target(target)
        # Also handle config_target being a list (from apm.yml target: [claude, copilot])
        compile_config_target = _resolve_compile_target(config_target)
        detected_target, detection_reason = detect_target(
            project_root=Path("."),
            explicit_target=compile_target,
            config_target=compile_config_target,
        )

        # Map 'minimal' to 'vscode' for the compiler (AGENTS.md only, no folder integration)
        effective_target = detected_target if detected_target != "minimal" else "vscode"

        # Build config with distributed compilation flags (Task 7)
        config = CompilationConfig.from_apm_yml(
            output_path=output if output != AGENTS_MD_FILENAME else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
            single_agents=single_agents,
            trace=verbose,
            local_only=local_only,
            debug=verbose,
            clean_orphaned=clean,
            target=effective_target,
        )
        config.with_constitution = with_constitution

        # Handle distributed vs single-file compilation
        if config.strategy == "distributed" and not single_agents:
            # Show target-aware message with detection reason. Use
            # get_target_description() so any future target added to
            # target_detection shows up here automatically.
            if isinstance(target, list):
                # Multi-target list: show what the compiler will produce
                _target_label = ",".join(target)
                if effective_target == "all":
                    logger.progress(
                        f"Compiling for AGENTS.md + CLAUDE.md (--target {_target_label})"
                    )
                elif effective_target == "claude":
                    logger.progress(
                        f"Compiling for CLAUDE.md (--target {_target_label})"
                    )
                else:
                    logger.progress(
                        f"Compiling for AGENTS.md (--target {_target_label})"
                    )
            elif detected_target == "minimal":
                logger.progress(f"Compiling for AGENTS.md only ({detection_reason})")
                logger.progress(
                    " Create .github/, .claude/, .codex/, .opencode/ or .cursor/ folder for full integration",
                    symbol="light_bulb",
                )
            else:
                description = get_target_description(detected_target)
                logger.progress(
                    f"Compiling for {description} - {detection_reason}"
                )

            if dry_run:
                logger.dry_run_notice(
                    "showing placement without writing files"
                )
            if verbose:
                logger.verbose_detail(
                    "Verbose mode: showing source attribution and optimizer analysis"
                )
        else:
            logger.progress("Using single-file compilation (legacy mode)", symbol="page")

        # Perform compilation
        compiler = AgentsCompiler(".")
        result = compiler.compile(config, logger=logger)
        compile_has_critical = result.has_critical_security

        if result.success:
            # Handle different compilation modes
            if config.strategy == "distributed" and not single_agents:
                # Distributed compilation results - output already shown by professional formatter
                # Just show final success message
                if dry_run:
                    # Success message for dry run already included in formatter output
                    pass
                else:
                    # Success message for actual compilation
                    logger.success("Compilation completed successfully!", symbol="check")

            else:
                # Traditional single-file compilation - keep existing logic
                # Perform initial compilation in dry-run to get generated body (without constitution)
                intermediate_config = CompilationConfig(
                    output_path=config.output_path,
                    chatmode=config.chatmode,
                    resolve_links=config.resolve_links,
                    dry_run=True,  # force
                    with_constitution=config.with_constitution,
                    strategy="single-file",
                )
                intermediate_result = compiler.compile(intermediate_config)

                if intermediate_result.success:
                    # Perform constitution injection / preservation
                    from ...compilation.injector import ConstitutionInjector

                    injector = ConstitutionInjector(base_dir=".")
                    output_path = Path(config.output_path)
                    final_content, c_status, c_hash = injector.inject(
                        intermediate_result.content,
                        with_constitution=config.with_constitution,
                        output_path=output_path,
                    )

                    # Compute deterministic Build ID (12-char SHA256) over content with placeholder removed
                    import hashlib

                    from ...compilation.constants import BUILD_ID_PLACEHOLDER

                    lines = final_content.splitlines()
                    # Identify placeholder line index
                    try:
                        idx = lines.index(BUILD_ID_PLACEHOLDER)
                    except ValueError:
                        idx = None
                    hash_input_lines = [l for i, l in enumerate(lines) if i != idx]
                    hash_bytes = "\n".join(hash_input_lines).encode("utf-8")
                    build_id = hashlib.sha256(hash_bytes).hexdigest()[:12]
                    if idx is not None:
                        lines[idx] = f"<!-- Build ID: {build_id} -->"
                        final_content = "\n".join(lines) + (
                            "\n" if final_content.endswith("\n") else ""
                        )

                    if not dry_run:
                        # Only rewrite when content materially changes (creation, update, missing constitution case)
                        if c_status in ("CREATED", "UPDATED", "MISSING"):
                            # Defense-in-depth: scan compiled output before writing
                            from ...security.gate import WARN_POLICY, SecurityGate

                            verdict = SecurityGate.scan_text(
                                final_content, str(output_path), policy=WARN_POLICY
                            )
                            if verdict.has_findings:
                                actionable = verdict.critical_count + verdict.warning_count
                                if verdict.has_critical:
                                    compile_has_critical = True
                                if actionable:
                                    logger.warning(
                                        f"Compiled output contains {actionable} hidden character(s) "
                                        f"-- run 'apm audit --file {output_path}' to inspect"
                                    )
                            try:
                                _atomic_write(output_path, final_content)
                            except OSError as e:
                                logger.error(f"Failed to write final AGENTS.md: {e}")
                                sys.exit(1)
                        else:
                            logger.progress(
                                "No changes detected; preserving existing AGENTS.md for idempotency"
                            )

                    # Report success at the top
                    if dry_run:
                        logger.success(
                            "Context compilation completed successfully (dry run)",
                            symbol="check",
                        )
                    else:
                        logger.success(
                            f"Context compiled successfully to {output_path}",
                        )

                    stats = (
                        intermediate_result.stats
                    )  # timestamp removed; stats remain version + counts

                    # Add spacing before summary table
                    _rich_blank_line()

                    _display_single_file_summary(stats, c_status, c_hash, output_path, dry_run)

                    if dry_run:
                        preview = final_content[:500] + (
                            "..." if len(final_content) > 500 else ""
                        )
                        _rich_panel(
                            preview, title=" Generated Content Preview", style="cyan"
                        )
                    else:
                        _display_next_steps(output)

        # Display warnings for all compilation modes
        if result.warnings:
            logger.warning(
                f"Compilation completed with {len(result.warnings)} warning(s):"
            )
            for warning in result.warnings:
                logger.warning(f"  {warning}")

        if result.errors:
            logger.error(f"Compilation failed with {len(result.errors)} errors:")
            for error in result.errors:
                logger.error(f"  {error}")
            sys.exit(1)

        # Check for orphaned packages after successful compilation
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
            pass  # Continue if orphan check fails

        # Hard-fail when critical security findings were detected in compiled
        # output. Consistent with apm install and apm unpack behavior.
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
