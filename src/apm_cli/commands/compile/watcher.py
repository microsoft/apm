"""APM compile watch mode."""

import time

from ...constants import APM_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger


def _watch_mode(
    *,
    target,
    output,
    chatmode,
    no_links,
    dry_run,
    single_agents=False,
    verbose=False,
    local_only=False,
    with_constitution=True,
):
    """Watch for changes in .apm/ directories and auto-recompile.

    Each compile pass is delegated to ``_run_compile_once`` (cli.py), which
    is the same function the non-watch ``apm compile`` calls.  Sharing that
    body is what prevents the watch path from re-introducing the target-
    resolution drift fixed in #1019/#1074 (see #1345 for the regression).
    """
    # Lazy import: cli.py imports _watch_mode from here, so importing
    # _run_compile_once at module load would create a cycle.
    from .cli import _run_compile_once

    logger = CommandLogger("compile-watch", verbose=verbose, dry_run=dry_run)

    def _do_compile():
        """One compile pass; swallows exceptions so the watcher keeps running."""
        try:
            _run_compile_once(
                target=target,
                output=output,
                chatmode=chatmode,
                no_links=no_links,
                dry_run=dry_run,
                single_agents=single_agents,
                verbose=verbose,
                local_only=local_only,
                # `--clean` removes orphaned outputs.  Running it on
                # every recompile would surprise users mid-session;
                # keep watcher recompiles non-destructive.
                clean=False,
                with_constitution=with_constitution,
                logger=logger,
            )
        except Exception as e:
            logger.error(f"Error during recompilation: {e}")

    try:
        from pathlib import Path

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class APMFileHandler(FileSystemEventHandler):
            def __init__(self, on_change):
                self._on_change = on_change
                self.last_compile = 0
                self.debounce_delay = 1.0  # 1 second debounce

            def on_modified(self, event):
                if event.is_directory:
                    return
                # Only react to relevant files
                if not event.src_path.endswith(".md") and not event.src_path.endswith(
                    APM_YML_FILENAME
                ):
                    return
                # Debounce rapid changes
                current_time = time.time()
                if current_time - self.last_compile < self.debounce_delay:
                    return

                self.last_compile = current_time
                logger.progress(f"File changed: {event.src_path}", symbol="eyes")
                self._on_change()

        # Set up file watching
        event_handler = APMFileHandler(_do_compile)
        observer = Observer()

        # Watch patterns for APM files
        watch_paths = []

        # Check for .apm directory
        if Path(APM_DIR).exists():
            observer.schedule(event_handler, APM_DIR, recursive=True)
            watch_paths.append(f"{APM_DIR}/")

        # Check for .github/instructions and agents/chatmodes
        if Path(".github/instructions").exists():
            observer.schedule(event_handler, ".github/instructions", recursive=True)
            watch_paths.append(".github/instructions/")

        # Watch .github/agents/ (new standard)
        if Path(".github/agents").exists():
            observer.schedule(event_handler, ".github/agents", recursive=True)
            watch_paths.append(".github/agents/")

        # Watch .github/chatmodes/ (legacy)
        if Path(".github/chatmodes").exists():
            observer.schedule(event_handler, ".github/chatmodes", recursive=True)
            watch_paths.append(".github/chatmodes/")

        # Watch apm.yml if it exists
        if Path(APM_YML_FILENAME).exists():
            observer.schedule(event_handler, ".", recursive=False)
            watch_paths.append(APM_YML_FILENAME)

        if not watch_paths:
            logger.warning("No APM directories found to watch")
            logger.progress("Run 'apm init' to create an APM project")
            return

        # Start watching
        observer.start()
        logger.progress(f" Watching for changes in: {', '.join(watch_paths)}", symbol="eyes")
        logger.progress("Press Ctrl+C to stop watching...", symbol="info")

        # Do initial compilation
        _do_compile()

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
