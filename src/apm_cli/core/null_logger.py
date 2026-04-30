"""Console-fallback logger for MCPIntegrator contexts.

Provides a partial ``CommandLogger`` interface backed by ``_rich_*``
console helpers.  This is NOT a silent null object -- every implemented
method produces visible terminal output.

Use this instead of ``logger=None`` checks inside ``MCPIntegrator``
methods.  It is NOT a drop-in replacement for the full
``CommandLogger`` or ``InstallLogger`` interfaces used in CLI command
functions.
"""

from apm_cli.utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)


class NullCommandLogger:
    """Partial ``CommandLogger`` facade for ``MCPIntegrator`` contexts.

    Implements only the subset of ``CommandLogger`` needed by
    ``MCPIntegrator``: ``start``, ``progress``, ``success``,
    ``warning``, ``error``, ``verbose_detail``, ``tree_item``, and
    ``package_inline_warning``.

    **Not implemented** (will raise ``AttributeError`` if called):
    ``dry_run_notice()``, ``should_execute()``, ``auth_step()``,
    ``auth_resolved()``, ``validation_start()``, ``validation_fail()``,
    ``render_summary()``, and all ``InstallLogger``-specific methods.

    .. note::

        This is NOT a silent null object.  Every implemented method
        delegates to ``_rich_*`` console helpers and therefore produces
        **visible terminal output**.

    The ``verbose`` attribute is always ``False`` so
    ``verbose_detail()`` calls are silently discarded (matching the
    behaviour of the ``if logger:`` branches that guard verbose output).
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
