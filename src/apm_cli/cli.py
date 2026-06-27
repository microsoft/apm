"""Command-line interface for Agent Package Manager (APM).

Thin wiring layer  -- all command logic lives in ``apm_cli.commands.*`` modules.
"""

import ctypes
import logging
import os
import sys
import warnings

import click

from apm_cli.commands._helpers import (
    ERROR,
    RESET,
    WARNING,
    _check_and_notify_updates,
    print_version,
)
from apm_cli.commands.approve import approve_cmd, deny_cmd
from apm_cli.commands.audit import audit
from apm_cli.commands.cache import cache
from apm_cli.commands.compile import compile as compile_cmd
from apm_cli.commands.config import config
from apm_cli.commands.deps import deps
from apm_cli.commands.doctor import doctor
from apm_cli.commands.experimental import experimental
from apm_cli.commands.find import find as find_cmd
from apm_cli.commands.init import init
from apm_cli.commands.install import install
from apm_cli.commands.list_cmd import list as list_cmd
from apm_cli.commands.lock import lock
from apm_cli.commands.marketplace import marketplace
from apm_cli.commands.marketplace import search as marketplace_search
from apm_cli.commands.mcp import mcp
from apm_cli.commands.outdated import outdated as outdated_cmd
from apm_cli.commands.pack import pack_cmd, unpack_cmd
from apm_cli.commands.plugin import plugin as plugin_cmd
from apm_cli.commands.policy import policy
from apm_cli.commands.prune import prune
from apm_cli.commands.publish import publish_cmd
from apm_cli.commands.run import preview, run
from apm_cli.commands.runtime import runtime
from apm_cli.commands.self_update import self_update
from apm_cli.commands.targets import targets
from apm_cli.commands.uninstall import uninstall
from apm_cli.commands.update import update
from apm_cli.commands.view import view as view_cmd

_CLI_EPILOG = (
    "\b\n"
    "Common workflows:\n"
    "  apm init                       Scaffold a new project\n"
    "  apm install                    Install dependencies from apm.yml\n"
    "  apm install --frozen           Reproduce lockfile exactly (CI-safe)\n"
    "  apm lock                       Resolve deps and write lockfile only\n"
    "  apm outdated                   See what's drifted from upstream\n"
    "  apm update                     Refresh refs and rewrite the lockfile\n"
    "  apm audit --ci                 Validate lockfile integrity for CI gates\n"
    "  apm doctor                     Diagnose environment problems\n"
    "  apm run <script>               Execute a script from apm.yml"
)


def _configure_logging(verbose: bool = False) -> None:
    """Configure stdlib logging for the ``apm_cli`` package.

    Two mechanisms activate debug-level output (either is sufficient):

    * ``--verbose`` / ``-v`` flag on the ``apm`` command.
    * ``APM_LOG_LEVEL=DEBUG`` environment variable (accepts any stdlib
      level name: DEBUG, INFO, WARNING, ERROR, CRITICAL).

    When neither is set the ``apm_cli`` logger defaults to WARNING so
    routine runs stay silent.  A :class:`~apm_cli.core.auth.SecretRedactionFilter`
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
    from apm_cli.core.auth import SecretRedactionFilter

    if not any(isinstance(f, SecretRedactionFilter) for f in apm_logger.filters):
        apm_logger.addFilter(SecretRedactionFilter())


@click.group(
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
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    if verbose:
        # Upgrade to DEBUG when the flag is set; env-var path runs in main().
        _configure_logging(verbose=True)

    # Suppress only the agents-target deprecation warning so CLI users see
    # the formatted logger.warning() in the install phase, not a double print.
    # Scoped to AgentsTargetDeprecationWarning to avoid masking future
    # DeprecationWarnings from apm_cli modules.
    from apm_cli.core.target_detection import AgentsTargetDeprecationWarning

    warnings.filterwarnings("ignore", category=AgentsTargetDeprecationWarning)

    # Check for updates only for known commands; skip on invalid input to fail fast.
    if (
        not ctx.resilient_parsing
        and ctx.invoked_subcommand is not None
        and ctx.command.get_command(ctx, ctx.invoked_subcommand) is not None
    ):
        _check_and_notify_updates()


# Register command groups
cli.add_command(approve_cmd, name="approve")
cli.add_command(audit)
cli.add_command(cache)
cli.add_command(deny_cmd, name="deny")
cli.add_command(deps)
cli.add_command(view_cmd)
# Hidden backward-compatible alias: ``apm info`` → ``apm view``
cli.add_command(
    click.Command(
        name="info",
        callback=view_cmd.callback,
        params=list(view_cmd.params),
        help=view_cmd.help,
        hidden=True,
    )
)
cli.add_command(pack_cmd, name="pack")
cli.add_command(unpack_cmd, name="unpack")
cli.add_command(publish_cmd, name="publish")
cli.add_command(init)
cli.add_command(install)
cli.add_command(lock)
cli.add_command(uninstall)
cli.add_command(prune)
cli.add_command(update)
cli.add_command(self_update)
cli.add_command(plugin_cmd, name="plugin")
cli.add_command(compile_cmd, name="compile")
cli.add_command(run)
cli.add_command(preview)
cli.add_command(list_cmd, name="list")
cli.add_command(config)
cli.add_command(experimental)
cli.add_command(runtime)
cli.add_command(targets)
cli.add_command(mcp)
cli.add_command(policy)
cli.add_command(outdated_cmd, name="outdated")
cli.add_command(doctor)
cli.add_command(marketplace)
cli.add_command(find_cmd)
cli.add_command(marketplace_search, name="search")


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


def main():
    """Main entry point for the CLI."""
    _configure_logging()  # honours APM_LOG_LEVEL env var; --verbose upgrades in cli()
    _configure_encoding()
    try:
        cli(obj={})
    except Exception as e:
        click.echo(f"{ERROR}Error: {e}{RESET}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
