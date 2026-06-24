"""APM compile watch mode."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from ...compilation import AgentsCompiler, CompilationConfig
from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...primitives.discovery import PRIMITIVE_SUFFIXES, clear_discovery_cache
from ...utils import perf_stats

# Skill modules use a fixed filename (``SKILL.md``) rather than a suffix
# pattern, so the watcher checks basename equality in addition to the
# suffix membership test below.
_SKILL_FILENAME = "SKILL.md"

if TYPE_CHECKING:
    from ...core.target_detection import CompileTargetType


def _format_target_label(
    effective_target: CompileTargetType | None,
    target_label_user: str | list[str] | None,
    target_label_config: str | list[str] | None,
) -> str | None:
    """Render a one-shot-parity 'Compiling for ...' label for the watch path.

    Mirrors the family-aware label the one-shot compile path emits so the
    user sees the same string in watch mode (#1345).
    """
    from ...core.target_detection import (
        get_target_description,
        should_compile_agents_md,
        should_compile_claude_md,
        should_compile_gemini_md,
    )

    if isinstance(effective_target, frozenset):
        if isinstance(target_label_user, list):
            source = f"--target {','.join(target_label_user)}"
        elif isinstance(target_label_config, list):
            source = f"apm.yml target: [{', '.join(target_label_config)}]"
        else:
            source = "multi-target"
        parts = []
        if should_compile_agents_md(effective_target):
            parts.append("AGENTS.md")
        if should_compile_claude_md(effective_target):
            parts.append("CLAUDE.md")
        if should_compile_gemini_md(effective_target):
            parts.append("GEMINI.md")
        return f"Compiling for {' + '.join(parts)} ({source})"
    if effective_target is None:
        return None
    return f"Compiling for {get_target_description(effective_target)}"


class APMFileHandler:
    """Watchdog file-system handler that recompiles APM context on edits.

    Defined at module scope (rather than inside ``_watch_mode``) so unit
    tests can instantiate it without spinning up a watchdog ``Observer``
    -- the regression for #1345 lives entirely in the ``from_apm_yml``
    call site this class owns.
    """

    def __init__(
        self,
        output: str,
        chatmode: str | None,
        no_links: bool,
        dry_run: bool,
        logger: CommandLogger,
        effective_target: CompileTargetType | None = None,
        cli_target: str | list[str] | None = None,
    ) -> None:
        self.output = output
        self.chatmode = chatmode
        self.no_links = no_links
        self.dry_run = dry_run
        self.logger = logger
        self.effective_target = effective_target
        # Raw --target CLI argument retained so ``_recompile`` can
        # re-run :func:`_resolve_effective_target` against the
        # current apm.yml on every recompile, letting mid-session
        # ``targets:`` edits take effect on the next file event.
        self.cli_target = cli_target
        self.last_compile = 0.0
        self.debounce_delay = 1.0  # 1 second debounce

    def on_modified(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        src_path = getattr(event, "src_path", "")
        # Smart filter: recompile only when the changed file is an APM
        # primitive (matches one of LOCAL_PRIMITIVE_PATTERNS' suffixes, a
        # SKILL.md basename, or the project manifest). Generic .md edits
        # (README, CHANGELOG, AGENTS output) never affect compile output
        # and would otherwise trigger a full discovery walk on every
        # save. See #1533 follow-up.
        basename = os.path.basename(src_path)
        is_manifest = basename == APM_YML_FILENAME
        is_skill = basename == _SKILL_FILENAME
        is_primitive = any(src_path.endswith(suffix) for suffix in PRIMITIVE_SUFFIXES)
        if not is_manifest and not is_primitive and not is_skill:
            # Leave a verbose breadcrumb so --verbose watch sessions can
            # see why an edit produced no recompile. Silent at default.
            if src_path:
                self.logger.verbose_detail(f"Skipping non-primitive change: {basename}")
            return
        current_time = time.time()
        if current_time - self.last_compile < self.debounce_delay:
            return
        self.last_compile = current_time
        self._recompile(src_path)

    def _recompile(self, changed_file: str) -> None:
        """Recompile after a file change, honoring the resolved target."""
        try:
            self.logger.progress(f"File changed: {changed_file}", symbol="eyes")
            self.logger.progress("Recompiling...", symbol="gear")

            # The process-scoped discovery cache (populated by the previous
            # compile pass) MUST be cleared before re-walking, otherwise
            # subsequent recompiles serve the stale primitive set from
            # before the edit. See #1533 follow-up. perf_stats counters
            # are NOT reset here -- they accumulate across the watch
            # session and are rendered once at teardown.
            clear_discovery_cache()

            # When apm.yml itself was the trigger, re-resolve so a
            # mid-session edit to ``target:`` / ``targets:`` takes
            # effect on this recompile, then persist the fresh value
            # so subsequent instruction-file edits do not silently
            # revert to the startup snapshot.  Match on basename
            # rather than ``endswith`` so a stray ``backup_apm.yml``
            # cannot masquerade as the project root manifest.
            effective_target = self.effective_target
            if os.path.basename(changed_file) == APM_YML_FILENAME:
                from .cli import _resolve_effective_target

                effective_target, _reason, _config_target = _resolve_effective_target(
                    self.cli_target
                )
                self.effective_target = effective_target

            config = CompilationConfig.from_apm_yml(
                output_path=self.output if self.output != AGENTS_MD_FILENAME else None,
                chatmode=self.chatmode,
                resolve_links=not self.no_links if self.no_links else None,
                dry_run=self.dry_run,
                target=effective_target,
            )

            compiler = AgentsCompiler(".")
            result = compiler.compile(config, logger=self.logger)

            if result.success:
                if self.dry_run:
                    self.logger.success("Recompilation successful (dry run)", symbol="sparkles")
                else:
                    self.logger.success(f"Recompiled to {result.output_path}", symbol="sparkles")
            else:
                self.logger.error("Recompilation failed")
                for error in result.errors:
                    self.logger.error(f"  {error}")

        except Exception as e:
            self.logger.error(f"Error during recompilation: {e}")


def _watch_mode(
    output: str,
    chatmode: str | None,
    no_links: bool,
    dry_run: bool,
    verbose: bool = False,
    effective_target: CompileTargetType | None = None,
    target_label_user: str | list[str] | None = None,
    target_label_config: str | list[str] | None = None,
    cli_target: str | list[str] | None = None,
) -> None:
    """Watch for changes in .apm/ directories and auto-recompile.

    ``effective_target`` is the compiler-understood target resolved by
    :func:`apm_cli.commands.compile.cli._resolve_effective_target` (the
    same resolver the one-shot path uses) and is forwarded as ``target=``
    into the initial compile so the startup label matches the one-shot
    path (#1345).

    ``cli_target`` is the raw ``--target`` argument; recompiles re-run
    the resolver against the current apm.yml so mid-session edits to
    ``targets:`` take effect on the next file event without restarting
    the watcher.
    """
    logger = CommandLogger("compile-watch", verbose=verbose, dry_run=dry_run)

    try:
        from pathlib import Path

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        # Adapt the test-friendly module-level handler to watchdog's
        # FileSystemEventHandler base so Observer.schedule() accepts it.
        class _WatchdogAdapter(APMFileHandler, FileSystemEventHandler):
            pass

        event_handler = _WatchdogAdapter(
            output,
            chatmode,
            no_links,
            dry_run,
            logger,
            effective_target=effective_target,
            cli_target=cli_target,
        )
        observer = Observer()

        watch_paths = []

        if Path(APM_DIR).exists():
            observer.schedule(event_handler, APM_DIR, recursive=True)
            watch_paths.append(f"{APM_DIR}/")

        if Path(".github/instructions").exists():
            observer.schedule(event_handler, ".github/instructions", recursive=True)
            watch_paths.append(".github/instructions/")

        if Path(".github/agents").exists():
            observer.schedule(event_handler, ".github/agents", recursive=True)
            watch_paths.append(".github/agents/")

        if Path(".github/chatmodes").exists():
            observer.schedule(event_handler, ".github/chatmodes", recursive=True)
            watch_paths.append(".github/chatmodes/")

        if Path(APM_YML_FILENAME).exists():
            observer.schedule(event_handler, ".", recursive=False)
            watch_paths.append(APM_YML_FILENAME)

        if not watch_paths:
            logger.warning("No APM directories found to watch")
            logger.progress("Run 'apm init' to create an APM project")
            return

        observer.start()
        logger.progress(f" Watching for changes in: {', '.join(watch_paths)}", symbol="eyes")
        logger.progress("Press Ctrl+C to stop watching...", symbol="info")

        # Surface the same family-aware label the one-shot path prints so
        # users see at-a-glance which AGENTS / CLAUDE / GEMINI files watch
        # mode will (re)write (#1345).
        label = _format_target_label(effective_target, target_label_user, target_label_config)
        if label:
            logger.progress(label, symbol="gear")

        logger.progress("Performing initial compilation...", symbol="gear")

        # Watch mode is a long-lived process. Reset both the discovery
        # cache and perf counters on entry so neither carries state from
        # a sibling REPL/test run sharing this Python process.
        clear_discovery_cache()
        perf_stats.reset()

        config = CompilationConfig.from_apm_yml(
            output_path=output if output != AGENTS_MD_FILENAME else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
            target=effective_target,
        )

        compiler = AgentsCompiler(".")
        result = compiler.compile(config)

        # NOTE: render_summary moved to the Ctrl+C teardown below so the
        # watch session emits ONE aggregate perf block at exit instead of
        # spamming a 5-6 line block after every recompile.

        if result.success:
            if dry_run:
                logger.success("Initial compilation successful (dry run)", symbol="sparkles")
            else:
                logger.success(
                    f"Initial compilation complete: {result.output_path}",
                    symbol="sparkles",
                )
        else:
            logger.error("Initial compilation failed")
            for error in result.errors:
                logger.error(f"  [x] {error}")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            logger.progress("Stopped watching for changes", symbol="info")
            # Render aggregate perf counters accumulated across the
            # session. Reset once at watch start (above), so this summary
            # covers initial compile + every subsequent recompile.
            perf_stats.render_summary(logger, project_root=".")

        observer.join()

    except ImportError:
        logger.error("Watch mode requires the 'watchdog' library")
        logger.progress("Install it with: uv pip install watchdog")
        logger.progress("Or reinstall APM: uv pip install -e . (from the apm directory)")
        import sys

        sys.exit(1)
    except Exception as e:
        logger.error(f"Error in watch mode: {e}")
        import sys

        sys.exit(1)
