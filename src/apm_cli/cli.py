"""Command-line interface for Agent Package Manager (APM).

Thin wiring layer  -- all command logic lives in ``apm_cli.commands.*`` modules.
"""

# ruff: noqa: E402

import contextlib
import ctypes
import importlib
import logging
import os
import signal
import sys
import warnings

import click

from apm_cli.core.tls_trust import (
    TLSConfigurationError,
    configure_process_tls_trust,
    log_tls_trust_status,
)

_TLS_BOOTSTRAP_ERROR: TLSConfigurationError | None = None
try:
    configure_process_tls_trust()
except TLSConfigurationError as exc:
    # Command modules may create Sessions at import time but do not perform
    # network I/O. Preserve early TLS ordering, then surface a concise Click
    # error before any command callback can make a request.
    _TLS_BOOTSTRAP_ERROR = exc

from apm_cli.update_notice import check_and_notify_updates as _check_and_notify_updates
from apm_cli.version_output import print_version

_CLI_EPILOG = (
    "\b\n"
    "Common workflows:\n"
    "  apm auth <host>                Set up credentials for a git host\n"
    "  apm init                       Scaffold a new project\n"
    "  apm install                    Install dependencies from apm.yml\n"
    "  apm install --frozen           Reproduce lockfile exactly (CI-safe)\n"
    "  apm lock                       Resolve deps only (no deploy/delete)\n"
    "  apm outdated                   See what's drifted from upstream\n"
    "  apm update                     Refresh refs and rewrite the lockfile\n"
    "  apm audit --ci                 Validate lockfile integrity for CI gates\n"
    "  apm doctor                     Diagnose environment problems"
)


class _LazyCommand(click.Command):
    """Stub whose real Click object is imported on first dispatch.

    Root ``apm --help`` and shell completion only need name, summary and
    visibility, so every command module (and GitPython behind them) stays
    unloaded until the matching verb is invoked or its own ``--help``.
    ``help``/``short_help`` mirror the real command's summary source so the
    rendered summary is identical at every terminal width.
    """

    def __init__(
        self,
        name: str,
        module: str,
        attr: str,
        help: str | None = None,
        *,
        short_help: str | None = None,
        hidden: bool = False,
    ) -> None:
        super().__init__(name=name, help=help, short_help=short_help, hidden=hidden)
        self._module = module
        self._attr = attr
        self._resolved: click.Command | None = None

    def resolved(self) -> click.Command:
        """Return the real command, importing it once.

        The stub stays in the group's registry, so the root command table never
        depends on which verbs earlier in-process invocations dispatched.
        """
        if self._resolved is None:
            self._resolved = self.resolve()
        return self._resolved

    def resolve(self) -> click.Command:
        """Import and return the real command object."""
        loaded = getattr(importlib.import_module(self._module), self._attr)
        if not isinstance(loaded, click.Command):
            raise TypeError(f"{self._module}.{self._attr} is not a Click command")
        return loaded


class _LazyAliasCommand(_LazyCommand):
    """Hidden alias that copies the target's callback, params and help."""

    def resolve(self) -> click.Command:
        target = super().resolve()
        return click.Command(
            name=self.name,
            callback=target.callback,
            params=list(target.params),
            help=target.help,
            hidden=True,
        )


# Every verb is a stub: (name, module, attr, help, short_help, hidden).
# ``help`` is the real command's first help paragraph, used only when the
# real command has no explicit ``short_help``.
_LAZY_COMMANDS: tuple[tuple[str, str, str, str | None, str | None, bool], ...] = (
    (
        "approve",
        "apm_cli.commands.approve",
        "approve_cmd",
        None,
        "Approve executable primitives for packages",
        False,
    ),
    (
        "audit",
        "apm_cli.commands.audit",
        "audit",
        None,
        "Scan for hidden Unicode, drift, and policy violations",
        False,
    ),
    (
        "auth",
        "apm_cli.commands.auth",
        "auth",
        "Get a working credential for a git host. Reports what APM resolves, and walks you through creating a token if there is none.",
        None,
        False,
    ),
    (
        "cache",
        "apm_cli.commands.cache",
        "cache",
        "Manage the local package cache",
        None,
        False,
    ),
    (
        "compile",
        "apm_cli.commands.compile",
        "compile",
        "Compile APM context into distributed AGENTS.md files",
        None,
        False,
    ),
    (
        "config",
        "apm_cli.commands.config",
        "config",
        "Configure APM CLI. Run with no subcommand to show the merged project and global configuration.",
        None,
        False,
    ),
    (
        "deny",
        "apm_cli.commands.approve",
        "deny_cmd",
        None,
        "Deny executable primitives for packages",
        False,
    ),
    (
        "deps",
        "apm_cli.commands",
        "deps",
        "Manage APM package dependencies",
        None,
        False,
    ),
    (
        "discover",
        "apm_cli.commands.discover",
        "discover",
        "Alias for 'apm init --discover'.",
        None,
        True,
    ),
    (
        "doctor",
        "apm_cli.commands.doctor",
        "doctor",
        None,
        "Run environment diagnostics",
        False,
    ),
    (
        "experimental",
        "apm_cli.commands.experimental",
        "experimental",
        "Manage experimental feature flags",
        None,
        False,
    ),
    (
        "export-patch",
        "apm_cli.commands.export_patch",
        "export_patch",
        None,
        "Export local edits as patches to source packages",
        False,
    ),
    (
        "find",
        "apm_cli.commands.find",
        "find",
        None,
        "Trace a materialized file to its package(s)",
        False,
    ),
    (
        "init",
        "apm_cli.commands.init",
        "init",
        "Initialize a new APM project",
        None,
        False,
    ),
    (
        "install",
        "apm_cli.commands.install",
        "install",
        None,
        "Install APM, MCP, and LSP dependencies",
        False,
    ),
    (
        "lifecycle",
        "apm_cli.commands.lifecycle",
        "lifecycle",
        "Inspect, test, and scaffold lifecycle scripts.",
        None,
        False,
    ),
    (
        "list",
        "apm_cli.commands.list_cmd",
        "list",
        "List available scripts in the current project",
        None,
        False,
    ),
    (
        "lock",
        "apm_cli.commands.lock",
        "lock",
        None,
        "Resolve dependencies and write apm.lock.yaml",
        False,
    ),
    (
        "marketplace",
        "apm_cli.commands.marketplace",
        "marketplace",
        "Manage marketplaces for discovery and governance",
        None,
        False,
    ),
    (
        "mcp",
        "apm_cli.commands.mcp",
        "mcp",
        "Discover, inspect, and install MCP servers",
        None,
        False,
    ),
    (
        "outdated",
        "apm_cli.commands.outdated",
        "outdated",
        "Show outdated locked dependencies",
        None,
        False,
    ),
    (
        "pack",
        "apm_cli.commands.pack",
        "pack_cmd",
        "Pack distributable artifacts from your APM project.",
        None,
        False,
    ),
    (
        "plugin",
        "apm_cli.commands.plugin",
        "plugin",
        "Scaffold and manage plugins (plugin-author workflows)",
        None,
        False,
    ),
    (
        "policy",
        "apm_cli.commands.policy",
        "policy",
        "Inspect and diagnose APM policy",
        None,
        False,
    ),
    (
        "preview",
        "apm_cli.commands.run",
        "preview",
        "Preview a script's compiled prompt files",
        None,
        False,
    ),
    (
        "prune",
        "apm_cli.commands.prune",
        "prune",
        None,
        "Remove packages absent from the dependency graph",
        False,
    ),
    (
        "publish",
        "apm_cli.commands.publish",
        "publish_cmd",
        "Publish a package to a registry.",
        None,
        False,
    ),
    (
        "run",
        "apm_cli.commands.run",
        "run",
        "Run a script with parameters (experimental)",
        None,
        False,
    ),
    (
        "runtime",
        "apm_cli.commands.runtime",
        "runtime",
        "Manage AI runtimes (experimental)",
        None,
        False,
    ),
    (
        "search",
        "apm_cli.commands.marketplace",
        "search",
        "Search plugins in a marketplace (QUERY@MARKETPLACE)",
        None,
        False,
    ),
    (
        "self-update",
        "apm_cli.commands.self_update",
        "self_update",
        "Update the APM CLI binary itself to the latest version.",
        None,
        False,
    ),
    (
        "targets",
        "apm_cli.commands.targets",
        "targets",
        "Show resolved targets for the current project. If APM detects a target you don't intend (e.g. CLAUDE.md is documentation, not a Claude Code config), pin your targets explicitly in apm.yml.",
        None,
        False,
    ),
    (
        "uninstall",
        "apm_cli.commands.uninstall",
        "uninstall",
        None,
        "Remove packages by manifest entry or locked key",
        False,
    ),
    (
        "unpack",
        "apm_cli.commands.pack",
        "unpack_cmd",
        "[Deprecated] Extract an APM bundle into the current project. Reads only '--format apm' bundles; install plugin bundles (the default) with 'apm install <bundle-path>'. This command will be removed in a future release.",
        None,
        False,
    ),
    (
        "update",
        "apm_cli.commands.update",
        "update",
        "Refresh APM dependencies to the latest matching refs",
        None,
        False,
    ),
    (
        "view",
        "apm_cli.commands.view",
        "view",
        None,
        "View package metadata, versions, or file owner",
        False,
    ),
)

# Hidden backward-compatible alias: ``apm info`` -> ``apm view``.
_LAZY_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("info", "apm_cli.commands.view", "view", "View package metadata or list remote versions."),
)


class _OutputModeGroup(click.Group):
    """Capture full argv so root output mode precedes subcommand callbacks."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["apm_raw_args"] = tuple(args)
        return super().parse_args(ctx, args)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if isinstance(cmd, _LazyCommand):
            return cmd.resolved()
        return cmd

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """List subcommands from stubs so root --help does not import them."""
        commands: list[tuple[str, click.Command]] = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden:
                continue
            commands.append((subcommand, cmd))

        if commands:
            limit = formatter.width - 6 - max(len(name) for name, _cmd in commands)
            rows = [(subcommand, cmd.get_short_help_str(limit)) for subcommand, cmd in commands]
            if rows:
                with formatter.section("Commands"):
                    formatter.write_dl(rows)

    def shell_complete(self, ctx: click.Context, incomplete: str) -> list:
        """Complete from stubs so tab-complete does not import lazy verbs."""
        from click.shell_completion import CompletionItem

        results = [
            CompletionItem(name, help=command.get_short_help_str())
            for name in self.list_commands(ctx)
            if name.startswith(incomplete)
            for command in (self.commands.get(name),)
            if command is not None and not command.hidden
        ]
        results.extend(click.Command.shell_complete(self, ctx, incomplete))
        return results


def _configure_logging(verbose: bool = False) -> None:
    """Configure stdlib logging for the ``apm_cli`` package.

    Two mechanisms activate debug-level output (either is sufficient):

    * ``--verbose`` / ``-v`` flag on the ``apm`` command.
    * ``APM_LOG_LEVEL=DEBUG`` environment variable (accepts any stdlib
      level name: DEBUG, INFO, WARNING, ERROR, CRITICAL).

    When neither is set the ``apm_cli`` logger defaults to WARNING so
    routine runs stay silent.  A :class:`~apm_cli.core.auth_outcome.SecretRedactionFilter`
    is always installed on the ``apm_cli`` logger to strip token-bearing
    exception strings from debug records regardless of which mechanism
    activated debug mode.
    """
    env_level_str = os.environ.get("APM_LOG_LEVEL", "").strip().upper()
    env_level: int | None = (
        getattr(logging, env_level_str, None) if env_level_str.isalpha() else None
    )

    if verbose:
        level = logging.DEBUG
    elif env_level is not None:
        level = env_level
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    apm_logger = logging.getLogger("apm_cli")
    apm_logger.setLevel(level)

    # Install secret-redaction filter (idempotent: skip if already present).
    from apm_cli.core.auth_outcome import SecretRedactionFilter

    handlers = {
        *logging.getLogger().handlers,
        *apm_logger.handlers,
    }
    for handler in handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())


@click.group(
    cls=_OutputModeGroup,
    help="Agent Package Manager (APM): The package manager for AI-Native Development",
    epilog=_CLI_EPILOG,
)
@click.option(
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
    help="Show version and exit.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable debug-level logging (equivalent to APM_LOG_LEVEL=DEBUG).",
)
@click.pass_context
def cli(ctx, verbose: bool) -> None:
    """Main entry point for the APM CLI."""
    if _TLS_BOOTSTRAP_ERROR is not None:
        raise click.ClickException(
            f"{_TLS_BOOTSTRAP_ERROR}. "
            "Check the APM_EXTRA_CA_BUNDLE setting, or unset it and retry. "
            "See https://microsoft.github.io/apm/troubleshooting/ssl-issues/"
        )
    ctx.ensure_object(dict)
    from apm_cli.core.invocation_memo import invocation_scope

    ctx.with_resource(invocation_scope())
    ctx.obj["verbose"] = verbose
    from apm_cli.core.output_mode import configure_output_mode, detect_output_mode

    output_mode = detect_output_mode(ctx.meta.get("apm_raw_args", sys.argv[1:]))
    configure_output_mode(output_mode)
    ctx.obj["output_mode"] = output_mode

    if verbose:
        # Upgrade to DEBUG when the flag is set; env-var path runs in main().
        _configure_logging(verbose=True)
    log_tls_trust_status()

    # Suppress only the agents-target deprecation warning so CLI users see
    # the formatted logger.warning() in the install phase, not a double print.
    # Scoped to AgentsTargetDeprecationWarning to avoid masking future
    # DeprecationWarnings from apm_cli modules.
    from apm_cli.core.target_detection import AgentsTargetDeprecationWarning

    warnings.filterwarnings("ignore", category=AgentsTargetDeprecationWarning)

    # Check for updates only for known commands; skip on invalid input to fail fast.
    # Discovery must not initialize caches or contact the update service.
    raw_args = ctx.meta.get("apm_raw_args", sys.argv[1:])
    discovering = (
        ctx.invoked_subcommand == "discover"
        or (ctx.invoked_subcommand == "init" and "--discover" in raw_args)
        # Read-only file provenance (#739) must not write caches either.
        or (
            ctx.invoked_subcommand in ("view", "info")
            and any(arg == "--file" or arg.startswith("--file=") for arg in raw_args)
        )
    )
    if (
        not ctx.resilient_parsing
        and not discovering
        and ctx.invoked_subcommand is not None
        and ctx.command.get_command(ctx, ctx.invoked_subcommand) is not None
    ):
        _check_and_notify_updates()


# Register command stubs; real modules load on dispatch (see _LazyCommand).
for _name, _module, _attr, _help, _short_help, _hidden in _LAZY_COMMANDS:
    cli.add_command(
        _LazyCommand(_name, _module, _attr, _help, short_help=_short_help, hidden=_hidden),
        name=_name,
    )
for _name, _module, _attr, _help in _LAZY_ALIASES:
    cli.add_command(_LazyAliasCommand(_name, _module, _attr, _help, hidden=True), name=_name)


def _get_current_code_page() -> "Optional[int]":
    """Get current Windows console code page using WinAPI.

    Returns the code page number (e.g., 65001 for UTF-8, 950 for CP950).
    Returns None if detection fails or on non-Windows platforms.
    """
    if sys.platform != "win32":
        return None

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return kernel32.GetConsoleOutputCP()
    except Exception:
        return None


def _code_page_to_encoding_name(cp: int) -> str:
    """Map code page number to readable encoding name.

    Args:
        cp: Code page number (e.g., 950, 65001).

    Returns:
        Human-readable encoding name or fallback name.
    """
    cp_map = {
        65001: "UTF-8",
        950: "cp950 (Traditional Chinese)",
        936: "cp936 (Simplified Chinese)",
        932: "cp932 (Japanese)",
        949: "cp949 (Korean)",
        1252: "cp1252 (Western European)",
        1251: "cp1251 (Cyrillic)",
    }
    return cp_map.get(cp, f"cp{cp}")


def _try_switch_to_utf8() -> bool:
    """Try to switch console to UTF-8 (code page 65001).

    This function:
    1. Checks if console is already UTF-8.
    2. If not, attempts to switch using SetConsoleCP/SetConsoleOutputCP.
    3. Verifies success by re-checking the code page.

    Returns:
        True if already UTF-8 or successfully switched, False otherwise.
    """
    if sys.platform != "win32":
        return True

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Check current console code page
        current_cp = kernel32.GetConsoleOutputCP()
        if current_cp == 65001:
            return True  # Already UTF-8

        # Attempt to switch to UTF-8
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)

        # Verify success
        new_cp = kernel32.GetConsoleOutputCP()
        return new_cp == 65001
    except Exception:
        return False


def _warn_encoding_issue(failed_cp: int) -> None:
    """Warn user if console UTF-8 switch failed.

    Args:
        failed_cp: The code page that failed to switch from.
    """
    from apm_cli.commands._helpers import RESET, WARNING

    encoding_name = _code_page_to_encoding_name(failed_cp)
    click.echo(
        f"\n{WARNING}Warning: Console is {encoding_name}, UTF-8 switch failed.{RESET}\n",
        err=True,
    )
    click.echo(
        f"{WARNING}Display issues may occur. Suggestions:{RESET}",
        err=True,
    )
    click.echo("  - Run: chcp 65001  (if available)", err=True)
    click.echo("  - Or use: Windows Terminal or VS Code terminal\n", err=True)


def _configure_encoding() -> None:
    """Configure stdout/stderr for full Unicode on Windows.

    The default Windows console encoding (cp1252 or cp950) cannot represent many
    Unicode characters used in APM output (box-drawing, check marks, arrows, etc.).

    This function:
    1. Attempts to switch console to UTF-8 (code page 65001) via WinAPI.
    2. Sets ``PYTHONIOENCODING`` for child processes.
    3. Reconfigures Python text-mode streams to UTF-8.
    4. Only warns if UTF-8 switch fails.

    On non-Windows platforms this is a no-op.
    """
    if sys.platform != "win32":
        return

    # 1. Try to switch console to UTF-8
    utf8_success = _try_switch_to_utf8()

    # 2. Help child processes / pipes default to UTF-8
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 3. Reconfigure Python streams to UTF-8
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                try:  # noqa: SIM105
                    stream.reconfigure(encoding="utf-8", errors="backslashreplace")
                except Exception:
                    pass

    # 4. Warn only if UTF-8 switch failed
    if not utf8_success:
        current_cp = _get_current_code_page()
        if current_cp and current_cp != 65001:
            _warn_encoding_issue(current_cp)


def _interrupt_on_sigterm(signum: int, frame: object) -> None:
    """Treat SIGTERM like Ctrl+C so rollback, residue reporting and reaping run.

    Default handling is restored first, so a second SIGTERM terminates.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise KeyboardInterrupt


def _route_sigterm_to_interrupt() -> None:
    """Install :func:`_interrupt_on_sigterm` for this process (main thread)."""
    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _interrupt_on_sigterm)


def main():
    """Main entry point for the CLI."""
    _configure_logging()  # honours APM_LOG_LEVEL env var; --verbose upgrades in cli()
    _configure_encoding()
    _route_sigterm_to_interrupt()
    try:
        # Click glob-expands argv on Windows by default, so `--skill '*'` became
        # the current directory's file names (#4407). APM never wants that.
        cli(obj={}, windows_expand_args=False)
    except Exception as e:
        from apm_cli.commands._helpers import ERROR, RESET

        click.echo(f"{ERROR}Error: {e}{RESET}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
