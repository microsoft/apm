"""Console utility functions for formatting and output."""

import sys  # noqa: F401
import threading
from contextlib import contextmanager
from typing import Any, Optional  # noqa: F401

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

    init(autoreset=True)
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
}


# Thread-safe console singleton ------------------------------------------------
_console_instance: Any | None = None
_console_lock = threading.Lock()


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
            _console_instance = Console()
        except Exception:
            pass
    return _console_instance


def _reset_console() -> None:
    """Reset the console singleton. For testing only."""
    global _console_instance
    with _console_lock:
        _console_instance = None


def _rich_echo(
    message: str,
    color: str = "white",
    style: str = None,  # noqa: RUF013
    bold: bool = False,
    symbol: str = None,  # noqa: RUF013
):
    """Echo message with Rich formatting or colorama fallback."""
    # Handle backward compatibility - if style is provided, use it as color
    if style is not None:
        color = style

    if symbol and symbol in STATUS_SYMBOLS:
        symbol_char = STATUS_SYMBOLS[symbol]
        message = f"{symbol_char} {message}"

    console = _get_console()
    if console:
        try:
            style_str = color
            if bold:
                style_str = f"bold {color}"
            console.print(message, style=style_str, highlight=False, markup=False)
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
        click.echo(f"{color_code}{style_code}{message}{Style.RESET_ALL}")
    else:
        click.echo(message)


def _rich_success(message: str, symbol: str = None):  # noqa: RUF013
    """Display success message with green color and bold styling."""
    _rich_echo(message, color="green", symbol=symbol, bold=True)


def _rich_error(message: str, symbol: str = None):  # noqa: RUF013
    """Display error message with red color."""
    _rich_echo(message, color="red", symbol=symbol)


def _rich_warning(message: str, symbol: str = None):  # noqa: RUF013
    """Display warning message with yellow color."""
    _rich_echo(message, color="yellow", symbol=symbol)


def _rich_info(message: str, symbol: str = None):  # noqa: RUF013
    """Display info message with blue color."""
    _rich_echo(message, color="blue", symbol=symbol)


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
