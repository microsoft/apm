"""APM compile watch mode."""

import time

from ...compilation import AgentsCompiler, CompilationConfig
from ...constants import AGENTS_MD_FILENAME, APM_DIR, APM_YML_FILENAME
from ...core.command_logger import CommandLogger


def _watch_mode(output, chatmode, no_links, dry_run, verbose=False):
    """Watch for changes in .apm/ directories and auto-recompile."""
    logger = CommandLogger("compile-watch", verbose=verbose, dry_run=dry_run)

    try:
        # Try to import watchdog for file system monitoring
        from pathlib import Path

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class APMFileHandler(FileSystemEventHandler):
            def __init__(self, output, chatmode, no_links, dry_run, logger):
                self.output = output
                self.chatmode = chatmode
                self.no_links = no_links
                self.dry_run = dry_run
                self.logger = logger
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
                self._recompile(event.src_path)

            def _recompile(self, changed_file):
                """Recompile after file change."""
                try:
                    self.logger.progress(f"File changed: {changed_file}", symbol="eyes")
                    self.logger.progress("Recompiling...", symbol="gear")

                    # Create configuration from apm.yml with overrides
                    config = CompilationConfig.from_apm_yml(
                        output_path=self.output if self.output != AGENTS_MD_FILENAME else None,
                        chatmode=self.chatmode,
                        resolve_links=not self.no_links if self.no_links else None,
                        dry_run=self.dry_run,
                    )

                    # Create compiler and compile
                    compiler = AgentsCompiler(".")
                    result = compiler.compile(config, logger=self.logger)

                    if result.success:
                        if self.dry_run:
                            self.logger.success(
                                "Recompilation successful (dry run)", symbol="sparkles"
                            )
                        else:
                            self.logger.success(
                                f"Recompiled to {result.output_path}", symbol="sparkles"
                            )
                    else:
                        self.logger.error("Recompilation failed")
                        for error in result.errors:
                            self.logger.error(f"  {error}")

                except Exception as e:
                    self.logger.error(f"Error during recompilation: {e}")

        # Set up file watching
        event_handler = APMFileHandler(output, chatmode, no_links, dry_run, logger)
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
        logger.progress("Performing initial compilation...", symbol="gear")

        config = CompilationConfig.from_apm_yml(
            output_path=output if output != AGENTS_MD_FILENAME else None,
            chatmode=chatmode,
            resolve_links=not no_links if no_links else None,
            dry_run=dry_run,
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
