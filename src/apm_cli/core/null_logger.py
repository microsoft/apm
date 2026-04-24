"""Null-object CommandLogger that silently delegates to _rich_* helpers.

Use this instead of ``logger=None`` checks. Every method matches the
CommandLogger interface but calls _rich_* directly, so output is
preserved even without a CLI-provided logger.
"""

from apm_cli.utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)


class NullCommandLogger:
    """Drop-in replacement for CommandLogger when no logger is provided.

    All methods delegate to _rich_* helpers from console.py, preserving
    user-visible output. The ``verbose`` attribute is always False so
    verbose_detail() calls are silently discarded (matching the behavior
    of the ``if logger:`` branches that guard verbose output).
    """

    verbose = False

    def start(self, message: str, symbol: str = "running"):
        _rich_info(message, symbol=symbol)

    def progress(self, message: str, symbol: str = "info"):
        _rich_info(message, symbol=symbol)

    def success(self, message: str, symbol: str = "sparkles"):
        _rich_success(message, symbol=symbol)

    def warning(self, message: str, symbol: str = "warning"):
        _rich_warning(message, symbol=symbol)

    def error(self, message: str, symbol: str = "error"):
        _rich_error(message, symbol=symbol)

    def verbose_detail(self, message: str):
        """Discard verbose details (no CLI context to show them)."""
        pass

    def tree_item(self, message: str):
        _rich_echo(message, color="green")

    def package_inline_warning(self, message: str):
        """Discard inline warnings (verbose is always False)."""
        pass
