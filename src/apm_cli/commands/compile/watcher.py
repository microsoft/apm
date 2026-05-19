"""APM compile watch mode."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ...compilation import AgentsCompiler, CompilationConfig
from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger

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
    ) -> None:
        self.output = output
        self.chatmode = chatmode
        self.no_links = no_links
        self.dry_run = dry_run
        self.logger = logger
        self.effective_target = effective_target
        self.last_compile = 0.0
        self.debounce_delay = 1.0  # 1 second debounce

    def on_modified(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        src_path = getattr(event, "src_path", "")
        if not src_path.endswith(".md") and not src_path.endswith(APM_YML_FILENAME):
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

            config = CompilationConfig.from_apm_yml(
                output_path=self.output if self.output != AGENTS_MD_FILENAME else None,
                chatmode=self.chatmode,
                resolve_links=not self.no_links if self.no_links else None,
                dry_run=self.dry_run,
                target=self.effective_target,
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
) -> None:
    """Watch for changes in .apm/ directories and auto-recompile.

    ``effective_target`` is the compiler-understood target resolved by
    :func:`apm_cli.commands.compile.cli._resolve_effective_target` (the
    same resolver the one-shot path uses) and is forwarded as ``target=``
    into every :meth:`CompilationConfig.from_apm_yml` call so watch mode
    honors ``targets: [claude, cursor]`` instead of silently fanning out
    to all families on every recompile (#1345).
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

        config = CompilationConfig.from_apm_yml(
            output_path=output if output != AGENTS_MD_FILENAME else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
            target=effective_target,
        )

        compiler = AgentsCompiler(".")
        result = compiler.compile(config)

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
