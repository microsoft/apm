"""Command logger infrastructure for structured CLI output.

Provides CommandLogger (base for all commands) and InstallLogger
(install-specific phases). All methods delegate to _rich_* helpers
from apm_cli.utils.console — no new output primitives.
"""

from dataclasses import dataclass

from apm_cli.utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)


def _strip_source_prefix(source: str) -> str:
    """Strip the ``org:`` / ``url:`` prefix from a policy source string."""
    if not source:
        return ""
    return source.removeprefix("org:").removeprefix("url:")


@dataclass
class _ValidationOutcome:
    """Result of package validation before install."""

    valid: list  # List of (canonical_name, already_present: bool) tuples
    invalid: list  # List of (package_name, reason: str) tuples
    marketplace_provenance: dict = None  # canonical -> {discovered_via, marketplace_plugin_name}

    @property
    def all_failed(self) -> bool:
        return len(self.valid) == 0 and len(self.invalid) > 0

    @property
    def has_failures(self) -> bool:
        return len(self.invalid) > 0

    @property
    def new_packages(self) -> list:
        """Packages that are valid and NOT already present."""
        return [(name, present) for name, present in self.valid if not present]


class CommandLogger:
    """Base context-aware logger for all CLI commands.

    Provides a standard lifecycle: start → progress → complete/error → summary.
    All methods delegate to existing _rich_* helpers from apm_cli.utils.console.
    No new output primitives — this is a semantic wrapper.

    Usage:
        logger = CommandLogger("compile", verbose=True, dry_run=False)
        logger.start("Compiling agent manifests...")
        logger.progress("Processing 3 files...")
        logger.success("Compiled 3 manifests")
        logger.render_summary()
    """

    def __init__(self, command: str, verbose: bool = False, dry_run: bool = False):
        self.command = command
        self.verbose = verbose
        self.dry_run = dry_run
        self._diagnostics = None  # Lazy init

    @property
    def diagnostics(self):
        """Lazy-init DiagnosticCollector."""
        if self._diagnostics is None:
            from apm_cli.utils.diagnostics import DiagnosticCollector

            self._diagnostics = DiagnosticCollector(verbose=self.verbose)
        return self._diagnostics

    # --- Common lifecycle ---

    def start(self, message: str, symbol: str = "running"):
        """Log start of an operation."""
        _rich_info(message, symbol=symbol)

    def progress(self, message: str, symbol: str = "info"):
        """Log progress during an operation."""
        _rich_info(message, symbol=symbol)

    def mcp_lookup_heartbeat(self, count: int):
        """Emit a single batch heartbeat before MCP registry validation
        (F4, microsoft/apm#1116).

        Surfaces a static ``[>] Looking up N MCP server(s) in
        registry...`` line so the user sees the install moving forward
        during the (sometimes multi-second) registry round trip. Static
        line, not a transient progress bar, so it survives in CI logs
        and ``2>&1 | tee`` pipelines.

        Skipped silently when ``count <= 0`` to avoid noisy zero-batch
        output on installs with no registry MCP deps.
        """
        if count <= 0:
            return
        noun = "server" if count == 1 else "servers"
        _rich_info(f"Looking up {count} MCP {noun} in registry...", symbol="running")

    def info(self, message: str, symbol: str = "info"):
        """Log static advisory / informational context.

        Distinct from :meth:`progress` only at the semantic level:
        ``progress`` narrates an in-flight step (may be suppressed in
        ``--quiet``/CI), while ``info`` carries persistent advisory
        context such as recovery hints that must survive quiet-mode
        suppression. Both currently delegate to ``_rich_info``; the
        split exists so future quiet-mode policy can drop ``progress``
        without dropping advisory context.
        """
        _rich_info(message, symbol=symbol)

    def success(self, message: str, symbol: str = "sparkles"):
        """Log successful completion."""
        _rich_success(message, symbol=symbol)

    def warning(self, message: str, symbol: str = "warning"):
        """Log a warning."""
        _rich_warning(message, symbol=symbol)

    def error(self, message: str, symbol: str = "error"):
        """Log an error."""
        _rich_error(message, symbol=symbol)

    def verbose_detail(self, message: str):
        """Log a detail only when verbose mode is enabled."""
        if self.verbose:
            _rich_echo(message, color="dim")

    def tree_item(self, message: str):
        """Log a tree sub-item (└─ line) under a package block.

        Renders green text with no symbol prefix — these are visual
        continuation lines, not standalone status messages.
        """
        _rich_echo(message, color="green")

    def dim(self, message: str):
        """Log a dim (grey) line unconditionally — no verbose gate.

        Use for secondary/contextual lines that must always appear
        (e.g. inline remediation hints under an error), where
        :meth:`verbose_detail` would suppress them for non-verbose runs.
        """
        _rich_echo(message, color="dim")

    def dim_check_item(self, message: str):
        """Log a dim check-marked item (used by validation_pass).

        Renders the message in dim colour with a check symbol — these
        are "already present / already updated" confirmations that sit
        visually alongside :meth:`tree_item` lines.
        """
        _rich_echo(message, color="dim", symbol="check")

    def blank_line(self):
        """Log a blank line through the shared console output path."""
        _rich_echo("")

    def package_inline_warning(self, message: str):
        """Log an inline warning under a package block (verbose only).

        Use for per-package diagnostic hints shown inline during install,
        supplementing the deferred DiagnosticCollector summary.
        """
        if self.verbose:
            _rich_echo(message, color="yellow")

    # --- Dry-run awareness ---

    def dry_run_notice(self, what_would_happen: str):
        """Log what would happen in dry-run mode."""
        _rich_info(f"[dry-run] {what_would_happen}", symbol="info")

    @property
    def should_execute(self) -> bool:
        """Return False if in dry-run mode."""
        return not self.dry_run

    # --- Auth diagnostics (available to all commands) ---

    def auth_step(self, step: str, success: bool, detail: str = ""):
        """Log an auth resolution step (verbose only)."""
        if self.verbose:
            msg = f"  auth: {step}"
            if detail:
                msg += f" ({detail})"
            _rich_echo(msg, color="dim", symbol="check" if success else "error")

    def auth_resolved(self, ctx):
        """Log the resolved auth context (verbose only).

        Args:
            ctx: AuthContext instance (imported lazily to avoid circular deps)
        """
        if self.verbose:
            source = getattr(ctx, "source", "unknown")
            token_type = getattr(ctx, "token_type", "unknown")
            has_token = getattr(ctx, "token", None) is not None
            if has_token:
                _rich_echo(f"  auth: resolved via {source} (type: {token_type})", color="dim")
            else:
                _rich_echo("  auth: no credentials available", color="dim")

    # --- Summary ---

    def render_summary(self):
        """Render diagnostic summary if any diagnostics were collected."""
        if self._diagnostics and self._diagnostics.has_diagnostics:
            self._diagnostics.render_summary()


def __getattr__(name: str):
    """Lazily re-export ``InstallLogger`` (PEP 562).

    ``_install_logger`` imports ``CommandLogger`` (its base class) from this
    module at module scope. Re-exporting ``InstallLogger`` eagerly here would
    create a circular import that fails whenever ``_install_logger`` is imported
    first (partially-initialised module). Deferring the import until
    ``command_logger.InstallLogger`` is first accessed breaks the cycle while
    preserving the public ``apm_cli.core.command_logger.InstallLogger`` surface.
    """
    if name == "InstallLogger":
        from ._install_logger import InstallLogger

        return InstallLogger
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
