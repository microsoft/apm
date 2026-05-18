"""Command logger infrastructure for structured CLI output.

Provides CommandLogger (base for all commands) and InstallLogger
(install-specific phases). All methods delegate to ``_rich_*`` helpers
from apm_cli.utils.console -- no new output primitives.
"""

from apm_cli.utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)

from ._install_logger_policy import _PolicyLoggingMixin
from ._messages import (
    _download_complete_msg,
    _install_summary_spec,
    _policy_miss_spec,
    _policy_resolved_msg,
)
from ._types import _ValidationOutcome

__all__ = ["CommandLogger", "InstallLogger", "_ValidationOutcome"]


class CommandLogger:
    """Base context-aware logger for all CLI commands.

    Standard lifecycle: start -> progress -> complete/error -> summary.
    All methods delegate to ``_rich_*`` helpers; this is a semantic wrapper.
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
        """Emit a single batch heartbeat before MCP registry validation.

        Surfaces ``[>] Looking up N MCP server(s) in registry...`` so the
        user sees progress during the registry round trip.  Skipped
        silently when ``count <= 0``.
        """
        if count <= 0:
            return
        noun = "server" if count == 1 else "servers"
        _rich_info(f"Looking up {count} MCP {noun} in registry...", symbol="running")

    def info(self, message: str, symbol: str = "info"):
        """Log persistent advisory context.

        Distinct from :meth:`progress` at the semantic level: ``info``
        carries advisory context that must survive quiet-mode suppression.
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
        """Log a tree sub-item (visual continuation line, green text)."""
        _rich_echo(message, color="green")

    def blank_line(self):
        """Log a blank line through the shared console output path."""
        _rich_echo("")

    def package_inline_warning(self, message: str):
        """Log an inline warning under a package block (verbose only)."""
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
        """Log the resolved auth context (verbose only)."""
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


class InstallLogger(CommandLogger, _PolicyLoggingMixin):
    """Install-specific logger with validation, resolution, and download phases.

    Knows whether this is a partial install (specific packages requested) or
    full install (all deps from apm.yml). Adjusts messages accordingly.
    """

    def __init__(self, verbose: bool = False, dry_run: bool = False, partial: bool = False):
        super().__init__("install", verbose=verbose, dry_run=dry_run)
        self.partial = partial  # True when specific packages are passed to `apm install`
        self._stale_cleaned_total = 0  # Accumulated by stale_cleanup / orphan_cleanup

    # --- Validation phase ---

    def validation_start(self, count: int):
        """Log start of package validation."""
        noun = "package" if count == 1 else "packages"
        _rich_info(f"Validating {count} {noun}...", symbol="gear")

    def validation_pass(self, canonical: str, already_present: bool):
        """Log a package that passed validation."""
        if already_present:
            _rich_echo(f"{canonical} (already in apm.yml)", color="dim", symbol="check")
        else:
            _rich_success(canonical, symbol="check")

    def validation_fail(self, package: str, reason: str):
        """Log a package that failed validation."""
        _rich_error(f"{package} -- {reason}", symbol="error")

    def validation_summary(self, outcome: _ValidationOutcome):
        """Log validation summary; return True if install should continue."""
        if outcome.all_failed:
            _rich_error("All packages failed validation. Nothing to install.")
            return False
        if outcome.has_failures:
            failed_count = len(outcome.invalid)
            noun = "package" if failed_count == 1 else "packages"
            _rich_warning(f"{failed_count} {noun} failed validation and will be skipped.")
        return True

    # --- Resolution phase ---

    def resolution_start(self, to_install_count: int, lockfile_count: int):
        """Log start of dependency resolution."""
        if self.partial:
            noun = "package" if to_install_count == 1 else "packages"
            _rich_info(f"Installing {to_install_count} new {noun}...", symbol="running")
            if lockfile_count > 0 and self.verbose:
                _rich_echo(
                    f"  ({lockfile_count} existing dependencies in lockfile)",
                    color="dim",
                )
        else:
            _rich_info("Installing dependencies from apm.yml...", symbol="running")
            if lockfile_count > 0:
                _rich_info(f"Using apm.lock.yaml ({lockfile_count} locked dependencies)")

    def nothing_to_install(
        self,
        lockfile_present: bool = False,
        update_mode: bool = False,
    ):
        """Log when there is nothing to install.

        Appends a nudge towards ``apm update`` when the lockfile is present
        and we are not already in update mode.
        """
        if self.partial:
            _rich_info("Requested packages are already installed.", symbol="check")
        else:
            _rich_success("All dependencies are up to date.", symbol="check")
        if lockfile_present and not update_mode:
            _rich_info("Lockfile already satisfied -- run 'apm update' to resolve latest refs.")

    # --- Download phase ---

    def download_start(self, dep_name: str, cached: bool):
        """Log start of a package download."""
        if cached:
            self.verbose_detail(f"  Using cached: {dep_name}")
        elif self.verbose:
            _rich_info(f"  Downloading: {dep_name}", symbol="download")

    def resolving_heartbeat(self, dep_name: str):
        """Emit a per-dependency progress heartbeat during BFS resolve."""
        _rich_info(f"Resolving {dep_name}...", symbol="running")

    def download_complete(
        self,
        dep_name: str,
        ref: str = "",
        sha: str = "",
        cached: bool = False,
        # Legacy compat: if callers pass ref_suffix= we handle it
        ref_suffix: str = "",
    ):
        """Log completion of a package download.

        Args:
            dep_name: Package display name (repo_url or virtual path).
            ref: Git reference (tag name, branch) if any.
            sha: Short commit SHA (8 chars) if any.
            cached: Whether this was a cache hit.
            ref_suffix: Deprecated -- legacy pass-through until all callers
                are migrated.
        """
        _rich_echo(_download_complete_msg(dep_name, ref, sha, cached, ref_suffix), color="green")

    def download_failed(self, dep_name: str, error: str):
        """Log a download failure."""
        _rich_error(f"  [x] {dep_name} -- {error}")

    # --- Verbose sub-item methods (install-specific) ---

    def lockfile_entry(self, key: str, ref: str = "", sha: str = ""):
        """Log a lockfile entry in verbose mode; omits unpinned deps."""
        if not self.verbose:
            return
        if sha:
            _rich_echo(f"    {key}: locked at {sha}", color="dim")
        elif ref:
            _rich_echo(f"    {key}: pinned to {ref}", color="dim")

    def package_auth(self, source: str, token_type: str = ""):
        """Log auth source for a package (verbose only, 4-space indent)."""
        if not self.verbose:
            return
        type_str = f" ({token_type})" if token_type else ""
        _rich_echo(f"    Auth: {source}{type_str}", color="dim")

    def package_type_info(self, type_label: str):
        """Log detected package type (verbose only, 4-space indent)."""
        if not self.verbose:
            return
        _rich_echo(f"    Package type: {type_label}", color="dim")

    # --- Cleanup phase (stale and orphan file removal) ---

    def stale_cleanup(self, dep_key: str, count: int):
        """Log per-package stale-file cleanup; visible without ``--verbose``.

        Stale-file deletion is a destructive operation in the user's
        tracked workspace; it must be visible at default verbosity.
        """
        if count <= 0:
            return
        self._stale_cleaned_total += count
        noun = "file" if count == 1 else "files"
        _rich_info(f"Cleaned {count} stale {noun} from {dep_key}", symbol="info")

    def orphan_cleanup(self, count: int):
        """Log post-install orphan-file cleanup; visible without ``--verbose``."""
        if count <= 0:
            return
        self._stale_cleaned_total += count
        noun = "file" if count == 1 else "files"
        _rich_info(
            f"Cleaned {count} {noun} from packages no longer in apm.yml",
            symbol="info",
        )

    @property
    def stale_cleaned_total(self) -> int:
        """Total files removed by stale and orphan cleanup during this install."""
        return self._stale_cleaned_total

    def cleanup_skipped_user_edit(self, rel_path: str, dep_key: str):
        """Warn that a user-edited stale file was kept rather than deleted."""
        _rich_warning(
            f"  Kept user-edited file {rel_path} (from {dep_key}); "
            "delete manually if no longer needed",
            symbol="warning",
        )

    # --- Install summary ---

    def install_summary(
        self,
        apm_count: int,
        mcp_count: int,
        errors: int = 0,
        stale_cleaned: int = 0,
        elapsed_seconds: float | None = None,
    ):
        """Log final install summary.

        Args:
            apm_count: Number of APM dependencies installed.
            mcp_count: Number of MCP servers installed.
            errors: Number of errors collected during install.
            stale_cleaned: Total stale + orphan files removed during this
                install.
            elapsed_seconds: Wall-clock duration; appended as
                `` in {x:.1f}s`` when provided (F5, microsoft/apm#1116).
        """
        spec = _install_summary_spec(apm_count, mcp_count, errors, stale_cleaned, elapsed_seconds)
        if spec is None:
            return
        style, message = spec
        if style == "success":
            _rich_success(message, symbol="sparkles")
        elif style == "warning":
            _rich_warning(message, symbol="warning")
        elif style == "error":
            _rich_error(message, symbol="error")

    def install_interrupted(self, elapsed_seconds: float):
        """Log a minimal elapsed-time line when normal summary did not render."""
        _rich_warning(
            f"Install interrupted after {elapsed_seconds:.1f}s.",
            symbol="warning",
        )
