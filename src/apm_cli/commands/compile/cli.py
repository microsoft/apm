"""APM compile command CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ...core.target_detection import CompileTargetType

from ...compilation import (
    AgentsCompiler,
    CompilationConfig,  # noqa: F401 -- patched by tests
)
from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_MODULES_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...primitives.discovery import clear_discovery_cache, discover_primitives
from ...utils import perf_stats
from ...utils.console import (
    _rich_error,
    _rich_info,
)
from .._helpers import (
    _get_console,
)
from ._run_ops import CompilationRunConfig as CompilationRunConfig
from ._run_ops import _run_compilation as _run_compilation
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


def _resolve_list_target(target_list, KNOWN_TARGETS):
    """Resolve a list of targets to a compiler family string or frozenset."""
    target_set = set(target_list)
    skip = {name for name, profile in KNOWN_TARGETS.items() if profile.compile_family is None}
    target_set -= skip
    if not target_set:
        for sentinel in target_list:
            if sentinel in skip:
                return sentinel
        return None

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
            families.add("agents")

    if len(families) >= 2:
        # Collapse {"vscode","agents"} to bare "vscode" ONLY when the
        # original target list contains no non-Copilot agents-family
        # targets (e.g. codex, opencode, windsurf).  When mixed targets
        # like [copilot, codex] are requested, keep the frozenset so
        # downstream dedup logic knows non-Copilot targets also consume
        # AGENTS.md (issue #1678).
        if families == {"vscode", "agents"}:
            _vscode_names = {"copilot", "vscode", "agents"}
            has_non_vscode_agents = any(
                name in target_set
                for name, profile in KNOWN_TARGETS.items()
                if profile.compile_family == "agents" and name not in _vscode_names
            )
            if not has_non_vscode_agents:
                return "vscode"
        return frozenset(families)
    for fam in ("claude", "gemini", "vscode"):
        if fam in families:
            return fam
    for name, profile in KNOWN_TARGETS.items():
        if profile.compile_family == "agents" and name in target_set:
            return name
    return "vscode"  # defensive fallback (unreachable)


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
        return _resolve_list_target(target, KNOWN_TARGETS)
    return target  # single string pass-through


def _resolve_effective_target(
    target: str | list[str] | None,
    source_root: Path | None = None,
) -> tuple[CompileTargetType, str, str | list[str] | None]:
    """Resolve the CLI --target arg to the compiler-understood effective target.

    Mirrors the resolution the one-shot compile path performs (load
    apm.yml ``target:`` / ``targets:``, run :func:`_resolve_compile_target`
    on both, fall back to :func:`detect_target` for the auto-detect case)
    so the watch path can build ``CompilationConfig`` with the same
    ``target=`` value the one-shot path uses (#1345).

    Args:
        target: The raw ``--target`` CLI argument (None, str, or list).
        source_root: Project source root (where apm.yml lives).
            Defaults to ``Path(".")`` for back-compat.

    Returns:
        Tuple ``(effective_target, detection_reason, config_target)`` where
        ``effective_target`` is what to pass as ``target=`` to
        :meth:`CompilationConfig.from_apm_yml`, ``detection_reason`` is the
        provenance label, and ``config_target`` is the raw apm.yml value
        (str | list | None) for user-facing label rendering.
    """
    from ...core.target_detection import detect_target
    from ...models.apm_package import APMPackage

    _root = source_root or Path(".")
    config_target = None
    apm_yml_path = _root / APM_YML_FILENAME
    if apm_yml_path.exists():
        apm_pkg = APMPackage.from_apm_yml(apm_yml_path)
        config_target = apm_pkg.target
        if config_target is None:
            try:
                from ...core.apm_yml import parse_targets_field
                from ...utils.yaml_io import load_yaml

                _raw = load_yaml(apm_yml_path)
                if isinstance(_raw, dict):
                    _yaml_targets = parse_targets_field(_raw)
                    if _yaml_targets:
                        config_target = (
                            _yaml_targets[0] if len(_yaml_targets) == 1 else _yaml_targets
                        )
            except Exception:
                pass

    compile_target = _resolve_compile_target(target)
    compile_config_target = _resolve_compile_target(config_target)

    if isinstance(compile_target, frozenset):
        return compile_target, "explicit --target flag", config_target
    if isinstance(compile_config_target, frozenset) and compile_target is None:
        return compile_config_target, "apm.yml target", config_target

    detected_target, detection_reason = detect_target(
        project_root=_root,
        explicit_target=compile_target,
        config_target=compile_config_target if isinstance(compile_config_target, str) else None,
    )
    return detected_target, detection_reason, config_target


def _validate_project(logger: CommandLogger, dry_run: bool, source_root: Path) -> None:
    """Check APM project exists and has content.

    Calls ``sys.exit(1)`` on fatal errors.  In dry-run mode the function
    emits diagnostic messages but does *not* exit so callers can test the
    full compile path even without real content.
    """
    from ...compilation.constitution import find_constitution

    if not (source_root / APM_YML_FILENAME).exists():
        logger.error("Not an APM project - no apm.yml found")
        logger.progress(" To initialize an APM project, run:")
        logger.progress("   apm init")
        sys.exit(1)

    # Check if there are any instruction files to compile
    apm_modules_exists = (source_root / APM_MODULES_DIR).exists()
    constitution_exists = find_constitution(source_root).exists()

    # Check if .apm directory has actual content
    apm_dir = source_root / APM_DIR
    local_apm_has_content = apm_dir.exists() and (
        any(apm_dir.rglob("*.instructions.md")) or any(apm_dir.rglob("*.chatmode.md"))
    )

    # If no primitive sources exist, check deeper to provide better feedback
    if not apm_modules_exists and not local_apm_has_content and not constitution_exists:
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
            logger.progress("   2. Or create local instructions: mkdir -p .apm/instructions")
            logger.progress("   3. Then create .instructions.md or .chatmode.md files")

        if not dry_run:  # Don't exit on dry-run to allow testing
            sys.exit(1)


def _run_validation_mode(logger: CommandLogger, verbose: bool, source_root: Path) -> None:
    """Run validation-only mode (``--validate`` flag).

    Discovers all primitives, validates them, and prints a structured
    summary.  Calls ``sys.exit(1)`` when validation errors are found.
    """
    logger.start("Validating APM context...", symbol="gear")
    clear_discovery_cache()
    perf_stats.reset()
    compiler = AgentsCompiler(".", source_dir=str(source_root))
    try:
        primitives = discover_primitives(str(source_root))
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

        apm_pkg = APMPackage.from_apm_yml(source_root / APM_YML_FILENAME)
        mcp_count = len(apm_pkg.get_mcp_dependencies())
        if mcp_count > 0:
            logger.progress(f"  * {mcp_count} MCP dependencies")
    except Exception:
        pass

    perf_stats.render_summary(logger, project_root=str(source_root))


def _run_watch_mode(
    logger: CommandLogger,
    target: str | list[str] | None,
    output: str,
    chatmode: str | None,
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
    if clean:
        logger.warning(
            "--clean is ignored in watch mode; run 'apm compile --clean' "
            "separately to remove orphaned outputs."
        )
    effective_target, _detection_reason, config_target = _resolve_effective_target(
        target, source_root=source_root
    )
    _watch_mode(
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
    help="Target platform (comma-separated). Values: copilot, claude, cursor, opencode, codex, gemini, antigravity, windsurf, kiro, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'antigravity' (alias 'agy') deploys to .agents/ and is explicit-only -- not part of 'all'. 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf+kiro (excludes agent-skills and antigravity); combine with 'agent-skills' or 'antigravity' to add them.",
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
    help=(
        "Remove orphaned output files (AGENTS.md, CLAUDE.md) no longer generated. "
        "Hand-authored files are never deleted; use --dry-run to preview removals."
    ),
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
@click.option(
    "--no-dedup/--no-force-instructions",
    "no_dedup",
    is_flag=True,
    default=False,
    help=(
        "Include the instructions section in CLAUDE.md even when .claude/rules/ is "
        "already populated. Overrides the default deduplication that normally omits "
        "the section to avoid duplicate context in Claude Code. Affects the Claude "
        "target only. Alias: --force-instructions."
    ),
)
@click.option(
    "--force-instructions",
    "no_dedup",
    is_flag=True,
    default=False,
    help="Alias for --no-dedup.",
    hidden=True,
)
@click.option(
    "--root",
    "root",
    type=click.Path(file_okay=False, resolve_path=True),
    default=None,
    metavar="DIR",
    help=(
        "Write AGENTS.md / CLAUDE.md outputs under DIR instead of $PWD; "
        "sources (apm.yml, .apm/, project tree for placement scoring) "
        "continue resolving from $PWD. Pairs with 'apm install --root' "
        "for scratch-dir verification. Cannot be combined with --watch."
    ),
)
@click.pass_context
def compile(  # noqa: PLR0913 -- Click handler
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
    legacy_skill_paths,
    compile_all,
    no_dedup,
    root,
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
    * --clean: Remove orphaned AGENTS.md files no longer generated; for
      --target claude, also removes a stale APM-generated CLAUDE.md when
      deduplication suppresses CLAUDE.md generation entirely (instructions
      already in .claude/rules/ with no constitution or other keep-alive).
      Hand-authored files are never deleted. Combine with --dry-run to
      preview removals before they happen.
    """
    logger = CommandLogger("compile", verbose=verbose, dry_run=dry_run)

    # --all flag: equivalent to --target all, with deprecation path
    if compile_all:
        if target is not None:
            logger.error("Cannot use --all together with --target")
            sys.exit(2)
        target = "all"
    elif (isinstance(target, str) and target == "all") or (
        isinstance(target, list) and "all" in target
    ):
        # Surface deprecation through the same UX channel as other
        # warnings so users actually see it (convergence item 9).
        # warnings.warn(DeprecationWarning) is invisible by default in
        # CLI output and would only ever fire for downstream library
        # consumers running with -W default, which we have none of.
        logger.warning("'--target all' is deprecated; use '--all' instead.")

    # --root + --watch is rejected: ``_watch_mode`` uses bare-relative
    # paths (``Path(APM_DIR)``, ``AgentsCompiler(".")``) and the watch
    # loop would scan the deploy root rather than the source tree. The
    # flag combination has no real use case -- watch is interactive
    # development; --root is for CI scratch-dir verification.
    if root and watch:
        raise click.UsageError("--root is not valid with --watch")

    # --root: see apm_cli.install.root_redirect.compile_root_redirect.
    # Bracket the handler so writes land under *root* while sources keep
    # resolving from the captured original $PWD via the source-root
    # override. ``--dry-run`` is threaded through so the context manager
    # skips the ``mkdir`` side-effect on previews. The manager is entered
    # manually (rather than via ``with``) so the existing top-level
    # try/except below does not need a 300-line re-indent; the matching
    # ``finally`` at the end of the handler restores cwd + clears the
    # override on every exit path (return, sys.exit, exception).
    from ...core.scope import InstallScope, get_source_root
    from ...install.root_redirect import compile_root_redirect

    _root_redirect = compile_root_redirect(root, dry_run=dry_run)
    _root_redirect.__enter__()
    try:
        # Source root: where apm.yml, .apm/, and the project tree are read
        # from. Equals $PWD unless --root redirects writes elsewhere.
        source_root = get_source_root(InstallScope.PROJECT)

        _validate_project(logger, dry_run, source_root)

        if validate:
            _run_validation_mode(logger, verbose, source_root)
            return

        if watch:
            _run_watch_mode(
                logger,
                target,
                output,
                chatmode,
                no_links,
                dry_run,
                verbose,
                clean,
                source_root=source_root,
            )
            return

        run_config = CompilationRunConfig(
            target=target,
            output=output,
            no_links=no_links,
            chatmode=chatmode,
            with_constitution=with_constitution,
            single_agents=single_agents,
            local_only=local_only,
            clean=clean,
            no_dedup=no_dedup,
        )
        _run_compilation(logger, dry_run, verbose, source_root, run_config)

    except ImportError as e:
        logger.error(f"Compilation module not available: {e}")
        logger.progress("This might be a development environment issue.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error during compilation: {e}")
        sys.exit(1)
    finally:
        # Restore cwd + clear the source-root override regardless of how
        # the handler exits (return, sys.exit -> SystemExit, exception).
        _root_redirect.__exit__(None, None, None)
