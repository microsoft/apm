"""Console utility functions for formatting and output."""

import re
import threading
from contextlib import contextmanager
from typing import Any

import click

# Rich library imports with fallbacks
try:
    from rich import print as rich_print
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    Console = Any
    Panel = Any
    Table = Any
    rich_print = None

# Colorama imports for fallback
try:
    from colorama import Fore, Style, init

    init(autoreset=False)
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    Fore = None
    Style = None


# Status symbols for consistent iconography (ASCII-safe for Windows cp1252)
STATUS_SYMBOLS = {
    "success": "[*]",
    "sparkles": "[*]",
    "running": "[>]",
    "gear": "[*]",
    "info": "[i]",
    "warning": "[!]",
    "error": "[x]",
    "check": "[+]",
    "cross": "[x]",
    "list": "[#]",
    "preview": "[>]",
    "robot": "[>]",
    "metrics": "[#]",
    "default": "[>]",  # Default script marker
    "eyes": "[>]",  # Watch mode
    "folder": "[>]",  # Directory/folder operations
    "cogs": "[*]",  # Compilation/processing
    "plugin": "[>]",  # Plugin-related operations
    "search": "[>]",  # Search operations
    "download": "[>]",  # Download operations
    "update": "[~]",  # Plan diff: dep refreshed to a new ref/commit
    "remove": "[-]",  # Plan diff: dep removed from manifest
    "equal": "[=]",  # Plan diff: dep unchanged (verbose only)
}


# ASCII-only Rich consoles ------------------------------------------------------
_ascii_console_classes: dict[type, type] = {}
_ELLIPSIS_RUN = re.compile(r"([^\s|]{0,2})\u2026")


def ascii_safe(text: str) -> str:
    """Return *text* with every non-ASCII character as a Python escape (#4330).

    User-supplied text such as a project or package path may hold any
    character; output stays printable ASCII, so ``\u00fc`` prints as
    ``\\xfc``. Control characters are left to their callers.
    """
    if text.isascii():
        return text
    return text.encode("ascii", "backslashreplace").decode("ascii")


def ascii_text(text: Any) -> Any:
    """Return a Rich ``Text`` whose plain text is ``ascii_safe``, keeping its styles.

    Escaping changes lengths, so the text is split at every span boundary
    and each piece is escaped and re-styled on its own (#4330).
    """
    if text.plain.isascii():
        return text
    from rich.text import Span, Text

    boundaries = sorted({offset for span in text.spans for offset in (span.start, span.end)})
    escaped = Text(
        style=text.style,
        justify=text.justify,
        overflow=text.overflow,
        no_wrap=text.no_wrap,
        end=text.end,
        tab_size=text.tab_size,
    )
    for piece in text.divide(boundaries):
        plain = ascii_safe(piece.plain)
        escaped.append_text(
            Text(plain, spans=[Span(0, len(plain), span.style) for span in piece.spans])
        )
    return escaped


def _ascii_renderable(renderable: Any) -> Any:
    """Escape pre-built ``Text`` in *renderable* before Rich measures it (#4330).

    Plain strings are escaped by ``render_str``; a ``Text`` (or one inside a
    table, panel, tree or group) is not, and escaping it after layout would
    widen its cell past the measured column.
    """
    from rich.console import Group
    from rich.padding import Padding
    from rich.panel import Panel as RichPanel
    from rich.table import Table as RichTable
    from rich.text import Text
    from rich.tree import Tree

    if isinstance(renderable, Text):
        return ascii_text(renderable)
    if isinstance(renderable, RichTable):
        for column in renderable.columns:
            column._cells = [_ascii_renderable(cell) for cell in column._cells]
            column.header = _ascii_renderable(column.header)
            column.footer = _ascii_renderable(column.footer)
        renderable.title = _ascii_renderable(renderable.title)
        renderable.caption = _ascii_renderable(renderable.caption)
    elif isinstance(renderable, RichPanel):
        renderable.renderable = _ascii_renderable(renderable.renderable)
        renderable.title = _ascii_renderable(renderable.title)
        renderable.subtitle = _ascii_renderable(renderable.subtitle)
    elif isinstance(renderable, Padding):
        renderable.renderable = _ascii_renderable(renderable.renderable)
    elif isinstance(renderable, Tree):
        renderable.label = _ascii_renderable(renderable.label)
        for child in renderable.children:
            _ascii_renderable(child)
    elif isinstance(renderable, Group):
        renderable._renderables = [_ascii_renderable(item) for item in renderable._renderables]
    return renderable


def _ascii_ellipsis(segment: Any) -> Any:
    """Return *segment* in ASCII: U+2026 runs become dots of the same width.

    Any other non-ASCII character left by a pre-built renderable is escaped.
    """
    text = segment.text
    if text.isascii():
        return segment
    if "\u2026" in text:
        text = _ELLIPSIS_RUN.sub(lambda m: "." * (len(m.group(1)) + 1), text)
    return segment._replace(text=ascii_safe(text))


def _ascii_console(base: Any, **kwargs: Any) -> Any:
    """Build *base* so boxes, rules, and trees render in printable ASCII.

    Rich picks ASCII box characters when the console encoding is not UTF.
    Reporting ``ascii`` keeps tables and panels within the encoding rule on
    every terminal. A non-class *base* (a test double) is called as is.
    """
    if not isinstance(base, type):
        return base(**kwargs)
    cls = _ascii_console_classes.get(base)
    if cls is None:

        class _AsciiConsole(base):  # type: ignore[misc, valid-type]
            @property
            def encoding(self) -> str:
                return "ascii"

            def render_str(self, text: str, **kwargs: Any) -> Any:
                # Escape before layout so table columns size the escaped text.
                return super().render_str(ascii_safe(text), **kwargs)

            def print(self, *objects: Any, **kwargs: Any) -> None:
                # Pre-built Text bypasses render_str; escape it before measurement.
                super().print(*(_ascii_renderable(item) for item in objects), **kwargs)

            def _render_buffer(self, buffer: Any) -> str:
                # Rich truncates with U+2026 whatever the encoding (#3994); swap
                # it and up to two preceding cells for dots so columns stay aligned.
                # Rewrite segment text, before styles become ANSI escapes.
                return super()._render_buffer(_ascii_ellipsis(segment) for segment in buffer)

        cls = _ascii_console_classes.setdefault(base, _AsciiConsole)
    return cls(**kwargs)


def new_console(**kwargs: Any) -> Any:
    """Return a new Rich console that renders boxes and rules in ASCII.

    The one constructor for Rich consoles in APM; ``kwargs`` pass through to
    ``rich.console.Console``.
    """
    from rich import console as rich_console

    return _ascii_console(rich_console.Console, **kwargs)


# Thread-safe console singleton ------------------------------------------------
_console_instance: Any | None = None
_console_lock = threading.Lock()
_console_stderr: bool = False
_stderr_console_instance: Any | None = None


def _get_stderr_console() -> Any | None:
    """Get a Rich console bound to stderr, even when normal output goes to stdout.

    For diagnostics that must never mix with command output (for example a
    deprecation warning ahead of ``--format json``).
    """
    global _stderr_console_instance
    if _console_stderr:
        return _get_console()
    if _stderr_console_instance is not None or not RICH_AVAILABLE:
        return _stderr_console_instance
    with _console_lock:
        if _stderr_console_instance is None:
            try:  # noqa: SIM105
                _stderr_console_instance = _ascii_console(Console, stderr=True)
            except Exception:
                pass
    return _stderr_console_instance


def _get_console() -> Any | None:
    """Get Rich console instance if available (singleton, thread-safe)."""
    global _console_instance
    if _console_instance is not None:
        return _console_instance
    if not RICH_AVAILABLE:
        return None
    with _console_lock:
        if _console_instance is not None:
            return _console_instance
        try:  # noqa: SIM105
            _console_instance = _ascii_console(Console, stderr=_console_stderr)
        except Exception:
            pass
    return _console_instance


def set_console_stderr(enabled: bool = True) -> None:
    """Route all console output to stderr.

    Call BEFORE any output is emitted (e.g. at CLI entry under --json).
    Resets the console singleton so the next _get_console() call picks
    up the new stream.
    """
    global _console_stderr, _console_instance
    with _console_lock:
        _console_stderr = enabled
        _console_instance = None


def _reset_console() -> None:
    """Reset the console singleton. For testing only."""
    global _console_instance, _console_stderr, _stderr_console_instance
    with _console_lock:
        _console_instance = None
        _stderr_console_instance = None
        _console_stderr = False


def _rich_echo(
    message: str,
    color: str = "white",
    style: str = None,  # noqa: RUF013
    bold: bool = False,
    symbol: str = None,  # noqa: RUF013
    *,
    soft_wrap: bool = False,
    err: bool = False,
):
    """Echo message with Rich formatting or colorama fallback.

    ``err=True`` writes to stderr regardless of the configured output stream.
    """
    # Handle backward compatibility - if style is provided, use it as color
    if style is not None:
        color = style

    if symbol and symbol in STATUS_SYMBOLS:
        symbol_char = STATUS_SYMBOLS[symbol]
        message = f"{symbol_char} {message}"

    # A hard-wrapped URL is split into lines that no longer open (#3994).
    soft_wrap = soft_wrap or "://" in message
    console = _get_stderr_console() if err else _get_console()
    if console:
        try:
            style_str = color
            if bold:
                style_str = f"bold {color}"
            console.print(
                message,
                style=style_str,
                highlight=False,
                markup=False,
                soft_wrap=soft_wrap,
            )
            return
        except Exception:
            pass

    # Colorama fallback
    if COLORAMA_AVAILABLE and Fore:
        color_map = {
            "red": Fore.RED,
            "green": Fore.GREEN,
            "yellow": Fore.YELLOW,
            "blue": Fore.BLUE,
            "cyan": Fore.CYAN,
            "white": Fore.WHITE,
            "magenta": Fore.MAGENTA,
            "muted": Fore.WHITE,  # Add muted mapping
            "info": Fore.BLUE,
        }
        color_code = color_map.get(color, Fore.WHITE)
        style_code = Style.BRIGHT if bold else ""
        click.echo(
            f"{color_code}{style_code}{ascii_safe(message)}{Style.RESET_ALL}",
            err=err or _console_stderr,
        )
    else:
        click.echo(ascii_safe(message), err=err or _console_stderr)


def _rich_success(message: str, symbol: str = None):  # noqa: RUF013
    """Display success message with green color and bold styling."""
    _rich_echo(message, color="green", symbol=symbol, bold=True)


def _rich_error(message: str, symbol: str = None, *, err: bool = False):  # noqa: RUF013
    """Display error message with red color (``err=True`` writes to stderr)."""
    _rich_echo(message, color="red", symbol=symbol, err=err)


def _rich_warning(message: str, symbol: str = None, *, err: bool = False):  # noqa: RUF013
    """Display warning message with yellow color (``err=True`` writes to stderr).

    Continuation lines carry remediation commands; they are never hard-wrapped,
    so a command and its argument stay on one copy-pasteable line.
    """
    head, sep, rest = message.partition("\n")
    _rich_echo(head, color="yellow", symbol=symbol, err=err)
    if sep:
        _rich_echo(rest, color="yellow", soft_wrap=True, err=err)


def _rich_info(
    message: str,
    symbol: str = None,  # noqa: RUF013
    *,
    soft_wrap: bool = False,
):
    """Display info message with blue color."""
    _rich_echo(message, color="blue", symbol=symbol, soft_wrap=soft_wrap)


def _rich_panel(content: str, title: str = None, style: str = "cyan"):  # noqa: RUF013
    """Display content in a Rich panel with fallback."""
    console = _get_console()
    if console and Panel:
        try:
            panel = Panel(content, title=title, border_style=style)
            console.print(panel)
            return
        except Exception:
            pass

    # Fallback to simple text display
    if title:
        click.echo(f"\n--- {title} ---")
    click.echo(content)
    if title:
        click.echo("-" * (len(title) + 8))


def _create_files_table(files_data: list, title: str = "Files") -> Any | None:
    """Create a Rich table for file display."""
    if not RICH_AVAILABLE or not Table:
        return None

    try:
        table = Table(title=title, show_header=True, header_style="bold cyan")
        table.add_column("File", style="bold white")
        table.add_column("Description", style="white")

        for file_info in files_data:
            if isinstance(file_info, dict):
                table.add_row(file_info.get("name", ""), file_info.get("description", ""))
            elif isinstance(file_info, (list, tuple)) and len(file_info) >= 2:
                table.add_row(str(file_info[0]), str(file_info[1]))
            else:
                table.add_row(str(file_info), "")

        return table
    except Exception:
        return None


@contextmanager
def show_download_spinner(repo_name: str):
    """Show spinner during download operations.

    Usage:
        with show_download_spinner("microsoft/apm-sample-package"):
            # Long-running download here
            pass
    """
    console = _get_console()
    if console and RICH_AVAILABLE:
        try:
            with console.status(f"[cyan]Downloading {repo_name}...", spinner="dots") as status:
                yield status
        except Exception:
            # Fallback if Rich fails
            click.echo(f"Downloading {repo_name}...")
            yield None
    else:
        # Fallback for non-Rich environments
        click.echo(f"Downloading {repo_name}...")
        yield None
