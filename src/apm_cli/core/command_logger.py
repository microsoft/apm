"""Command logger infrastructure for structured CLI output.

Provides CommandLogger (base for all commands) and InstallLogger
(install-specific phases). All methods delegate to _rich_* helpers
from apm_cli.utils.console — no new output primitives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from apm_cli.core.remediation import (
    DRY_RUN_INCOMPLETE,
    LEGACY_RENDER_MIGRATE,
    LOCKED_REFS_SATISFIED,
    ORPHANED_PACKAGES,
)
from apm_cli.models.results import InstallDisposition
from apm_cli.utils.console import (
    _rich_echo,
    _rich_error,
    _rich_info,
    _rich_success,
    _rich_warning,
)

if TYPE_CHECKING:
    from apm_cli.deps.revision_pins import RevisionPinSkip
    from apm_cli.install.failure_residue import FailedInstallResidue
    from apm_cli.integration.cleanup import CleanupResult
    from apm_cli.policy.models import PolicyRemedy


def _dependency_label(dep_key: str) -> str:
    """Render a lockfile dependency key without a machine-specific path.

    A local dependency declared by absolute or home path is keyed by that
    path; output names it ``_local/<name>``, as the manifest and lockfile do.
    """
    if dep_key.startswith("~") or PurePosixPath(dep_key).is_absolute():
        name = PurePosixPath(dep_key.replace("\\", "/")).name
    elif PureWindowsPath(dep_key).is_absolute():
        name = PureWindowsPath(dep_key).name
    else:
        return dep_key
    return f"_local/{name}" if name else dep_key


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
    prospective_package: object | None = None
    updated_packages: tuple[str, ...] = ()

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
        self._targets_announced = False

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

    def announce_targets(self, line: str) -> None:
        """Print the canonical ``Targets: X  (source: Y)`` line once per command."""
        if self._targets_announced:
            return
        self._targets_announced = True
        _rich_info(line, symbol="info")

    def mcp_lookup_heartbeat(self, count: int, registry: str | None = None):
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
        where = f"registry {registry}" if registry else "registry"
        _rich_info(f"Looking up {count} MCP {noun} in {where}...", symbol="running")

    def info(self, message: str, symbol: str = "info", *, soft_wrap: bool = False):
        """Log static advisory / informational context.

        Distinct from :meth:`progress` only at the semantic level:
        ``progress`` narrates an in-flight step (may be suppressed in
        ``--quiet``/CI), while ``info`` carries persistent advisory
        context such as recovery hints that must survive quiet-mode
        suppression. Both currently delegate to ``_rich_info``; the
        split exists so future quiet-mode policy can drop ``progress``
        without dropping advisory context.
        """
        # Only soft-wrapped calls pass the flag; the default call shape stays unchanged.
        if soft_wrap:
            _rich_info(message, symbol=symbol, soft_wrap=True)
            return
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

    def refusal(self, message: str) -> None:
        """Render a refusal on stderr, where click reports errors (#3992)."""
        _rich_error(message, symbol="error", err=True)

    def error_detail(self, message: str):
        """Log supporting error context that must remain visible by default."""
        _rich_echo(message)

    def exception(self, message: str):
        """Log an unexpected error and own the verbose recovery hint."""
        self.error(message)
        if not self.verbose:
            self.info("Run with --verbose for detailed diagnostics")

    def verbose_detail(self, message: str):
        """Log a detail only when verbose mode is enabled."""
        if self.verbose:
            _rich_echo(message, color="dim")

    def stale_cleanup(self, dep_key: str, cleanup: CleanupResult) -> int:
        """Name each deployed path a cleanup pass removed; return how many were named."""
        return self._removed_paths(
            cleanup.announced_paths,
            lambda noun: f"stale deployed {noun} for {_dependency_label(dep_key)}",
        )

    @staticmethod
    def _removed_paths(paths: Sequence[str], describe: Callable[[str], str]) -> int:
        """Render one cleanup header whose count matches the paths listed under it."""
        if not paths:
            return 0
        noun = "path" if any(path.endswith("/") for path in paths) else "file"
        noun += "" if len(paths) == 1 else "s"
        _rich_info(f"Cleaned {len(paths)} {describe(noun)}:", symbol="info")
        for path in paths:
            _rich_info(f"  - {path}", soft_wrap=True)
        return len(paths)

    def cleanup_skipped_user_edit(self, rel_path: str, dep_key: str):
        """Explain that a user-edited managed file was retained."""
        _rich_warning(
            f"Retained user-edited file {rel_path} from {_dependency_label(dep_key)}; "
            "resolve it and retry.",
            symbol="warning",
        )

    def tree_item(self, message: str):
        """Log a tree sub-item (└─ line) under a package block.

        Renders green text with no symbol prefix — these are visual
        continuation lines, not standalone status messages.
        """
        _rich_echo(message, color="green")

    def blank_line(self):
        """Log a blank line through the shared console output path."""
        _rich_echo("")

    def preformatted(self, text: str):
        """Render preformatted text (a diff or snippet) without a status symbol."""
        _rich_echo(text.rstrip("\n"), soft_wrap=True)

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

    def nothing_to_prune(self) -> None:
        """Log the single no-op line of ``apm prune``, whatever apm_modules/ holds (#4460)."""
        _rich_success("No orphaned packages found. Nothing to prune.", symbol="check")


# --- Library output -----------------------------------------------------------
# Library code below the command layer (MCP client adapters, the safe MCP
# installer) has no logger parameter. It emits through library_logger(), which
# returns the logger the running command bound, so its lines share that
# command's verbosity and rendering (#4089).

_LIBRARY_LOGGER: ContextVar[CommandLogger | None] = ContextVar("apm_library_logger", default=None)


@contextmanager
def library_output(logger: CommandLogger | None) -> Iterator[None]:
    """Route library output through *logger* for the duration of the block."""
    token = _LIBRARY_LOGGER.set(logger)
    try:
        yield
    finally:
        _LIBRARY_LOGGER.reset(token)


def library_logger() -> CommandLogger:
    """Return the logger library code emits through.

    That is the logger the running command bound, else the console-fallback
    ``NullCommandLogger`` so library code works without a live logger.
    """
    bound = _LIBRARY_LOGGER.get()
    if bound is not None:
        return bound
    from apm_cli.core.null_logger import NullCommandLogger

    return NullCommandLogger()


def _dependency_identity(ref: str) -> str | None:
    """Canonical identity of a dependency reference, or ``None`` if it does not parse."""
    from apm_cli.models.dependency.reference import DependencyReference

    try:
        return DependencyReference.parse(ref).get_identity()
    except (ValueError, TypeError):
        return None


class InstallLogger(CommandLogger):
    """Install-specific logger with validation, resolution, and download phases.

    Knows whether this is a partial install (specific packages requested) or
    full install (all deps from apm.yml). Adjusts messages accordingly.
    """

    def __init__(
        self,
        verbose: bool = False,
        dry_run: bool = False,
        partial: bool = False,
        *,
        command: str = "install",
    ):
        # ``apm update`` reuses this logger; ``command`` keeps hints true for it.
        super().__init__(command, verbose=verbose, dry_run=dry_run)
        self.partial = partial  # True when specific packages are passed to `apm install`
        self._stale_cleaned_total = 0  # Accumulated by stale_cleanup / orphan_cleanup
        self._dry_run_apm_update_count = 0
        self._dry_run_incomplete = False
        self._lockfile_written = False
        # MCP servers this run removed from native config; a change on their own.
        self._mcp_removed: set[str] = set()
        # The project's own .apm/ content changed on disk; never an APM dependency.
        self._local_content_updated = False
        self._failed_install_residue_note = ""
        # Identities `apm install <pkg>` is adding; a refused add never reaches apm.yml.
        self._adding: frozenset[str] = frozenset()
        # Policy banners already shown; the package and MCP phases share them.
        self._policy_banners: set[tuple[str, ...]] = set()

    def failed_install_residue(self, residue: FailedInstallResidue) -> None:
        """List native target output a failed install left behind, with one next step.

        Native target integrations are outside the install transaction, so
        this output is neither removed nor tracked by apm.lock.yaml.
        """
        files, entries = residue.files, residue.entries
        if not files and not entries:
            return
        if files:
            noun = "file" if len(files) == 1 else "files"
            self.warning(
                f"This failed install left {len(files)} native target {noun} on disk "
                "that apm.lock.yaml does not track:"
            )
            for path in files:
                self.error_detail(f"  {path}")
        if entries:
            noun = "entry" if len(entries) == 1 else "entries"
            lead = "also wrote" if files else "wrote"
            self.warning(
                f"This failed install {lead} {len(entries)} native target {noun} "
                "that apm.lock.yaml does not track (not plain files):"
            )
            for entry in entries:
                self.error_detail(f"  {entry}")
        command = "apm install -g" if residue.user_scope else "apm install"
        # Never a --force hint: it also deploys despite critical findings (RA-008).
        if files:
            self.info(f"Remove these files, then re-run '{command}'.")
        else:
            self.info(f"Re-run '{command}'.")
        total = len(files) + len(entries)
        self._failed_install_residue_note = (
            f"{total} native target {'item' if total == 1 else 'items'} listed above "
            f"{'remains' if total == 1 else 'remain'} in place."
        )

    def kept_overwrites(self, kept: int, written: Sequence[str]) -> None:
        """Name the files an install wrote beside the managed files it kept (#4360)."""
        if written:
            self.info(f"This install wrote {len(written)} file(s), recorded in apm.lock.yaml:")
            for path in written:
                self.error_detail(f"  {path}")
        # The lock is rewritten for the files written; the kept files' entries
        # are carried over unchanged (RA-009), so say both.
        if written:
            note = (
                f"apm.lock.yaml records the {len(written)} written file(s) and keeps the "
                f"previous entries of the {kept} kept file(s)."
            )
        elif self._lockfile_written:
            # Nothing was written, but the lock was (an older format upgraded,
            # or hash copies normalized to the deployment ledger): say so.
            note = (
                "apm.lock.yaml is rewritten in the current format; it records the "
                f"{kept} kept file(s) as APM last deployed them."
            )
        else:
            note = f"apm.lock.yaml keeps the previous entries of the {kept} kept file(s)."
        self._failed_install_residue_note = note

    def revision_pins_retained(self, skips: Sequence[RevisionPinSkip]) -> None:
        """Report retained revision pins once, with per-pin verbose detail."""
        count = len(skips)
        if count == 0:
            return
        noun = "pin" if count == 1 else "pins"
        self.warning(
            f"Retained {count} revision {noun}: no eligible stable annotated semver tag was found."
        )
        self.info(
            "Keeping the current SHA. Publish an annotated stable tag matching a "
            "supported pattern to refresh it."
        )
        if not self.verbose:
            self.info("Run with --verbose to see retained pins.")
        for skipped in skips:
            self.verbose_detail(f"  {skipped.display_name}: {skipped.old_sha[:8]}")

    def revision_pin_resolution_failed(self, error: Exception) -> None:
        """Render a safe-stop error with verbose parser context."""
        self.error(str(error))
        if error.__cause__ is not None:
            self.verbose_detail(f"  Parser cause: {error.__cause__}")
        self.info(
            "No files changed. Retry the update; report the upstream response if it persists."
        )
        if not self.verbose:
            self.info("Run with --verbose for parser context.")

    # --- Validation phase ---

    def validation_start(self, count: int):
        """Log start of package validation."""
        noun = "package" if count == 1 else "packages"
        _rich_info(f"Validating {count} {noun}...", symbol="gear")

    def validation_pass(
        self,
        canonical: str,
        already_present: bool,
        updated: bool = False,
        dry_run: bool = False,
    ):
        """Log a package that passed validation."""
        if updated:
            verb = "would update" if dry_run else "updated"
            _rich_echo(f"{canonical} ({verb} ref in apm.yml)", color="dim", symbol="check")
        elif already_present:
            _rich_echo(f"{canonical} (already in apm.yml)", color="dim", symbol="check")
        else:
            _rich_success(canonical, symbol="check")

    def validation_fail(self, package: str, reason: str):
        """Log a package that failed validation."""
        _rich_error(f"{package} -- {reason}", symbol="error")

    def validation_summary(self, outcome: _ValidationOutcome):
        """Log validation summary and decide whether to continue.

        Returns True if install should continue, False if all packages failed.
        """
        if outcome.all_failed:
            _rich_error("All packages failed validation. Nothing to install.")
            return False

        if outcome.has_failures:
            failed_count = len(outcome.invalid)
            noun = "package" if failed_count == 1 else "packages"
            _rich_warning(f"{failed_count} {noun} failed validation and will be skipped.")

        return True

    # --- Resolution phase ---

    def resolution_start(
        self,
        to_install_count: int,
        lockfile_count: int,
        update_count: int = 0,
    ):
        """Log start of dependency resolution."""
        if self.partial:
            noun = "package" if to_install_count == 1 else "packages"
            if self.dry_run:
                update_note = ""
                if update_count:
                    update_noun = "update" if update_count == 1 else "updates"
                    update_note = f" ({update_count} ref {update_noun})"
                _rich_info(
                    f"Previewing {to_install_count} {noun}{update_note}...",
                    symbol="running",
                )
            else:
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

    def record_dry_run_apm_updates(self, count: int) -> None:
        """Record how many selected APM refs the preview would update."""
        self._dry_run_apm_update_count = count

    def record_dry_run_incomplete(self, incomplete: bool) -> None:
        """Record whether offline metadata can support a complete preview."""
        self._dry_run_incomplete = incomplete

    def record_lockfile_write(self) -> None:
        """Keep metadata-only persistence out of the unchanged summary."""
        self._lockfile_written = True

    def record_local_content_updated(self) -> None:
        """Record that the project's own .apm/ content changed deployed files (#4184)."""
        self._local_content_updated = True

    def record_mcp_removed(self, names) -> None:
        """Record MCP servers this install removed, so the summary counts them."""
        self._mcp_removed.update(str(name) for name in names)

    def nothing_to_install(
        self,
        lockfile_present: bool = False,
        update_mode: bool = False,
    ):
        """Log when there's nothing to install -- context-aware message.

        Args:
            lockfile_present: True when apm.lock.yaml exists on disk at
                the time of the no-op.  When True (and we're not in
                update mode) we append the standard hint pointing at
                ``apm update`` -- this is the #1203 nudge that keeps
                users from believing ``apm install`` checks for newer
                versions.
            update_mode: True when this run was invoked with
                ``--update`` or via ``apm update``.  Suppresses the
                hint -- the user already asked to refresh.
        """
        if self.partial:
            _rich_info("Requested packages are already installed.", symbol="check")
        else:
            _rich_success("All dependencies are up to date.", symbol="check")
        if lockfile_present and not update_mode:
            _rich_info(f"Lockfile already satisfied -- {LOCKED_REFS_SATISFIED.clause()}.")

    # --- Download phase ---

    def download_start(self, dep_name: str, cached: bool):
        """Log start of a package download."""
        if cached:
            self.verbose_detail(f"  Using cached: {dep_name}")
        elif self.verbose:
            _rich_info(f"  Downloading: {dep_name}", symbol="download")

    def resolving_heartbeat(self, dep_name: str):
        """Emit a per-dependency progress heartbeat during BFS resolve.

        Surfaces an immediate ``[>] Resolving <name>...`` line so the
        user sees the install moving forward instead of staring at
        silence while transitive lookups happen behind the scenes
        (F1, microsoft/apm#1116). The line is static (not a Rich
        transient progress bar) so it survives in CI logs and behind
        ``2>&1 | tee`` pipelines, which the duck critique flagged as
        the must-survive surface.

        Called from the MAIN thread by the resolver/download callback
        BEFORE network work begins; F7's parallel BFS keeps emission
        on the main thread so output ordering is deterministic even
        when downloads are dispatched to a worker pool. A ``--dry-run``
        preview keeps it verbose-only so it lists only what it would install.
        """
        if self.dry_run:
            self.verbose_detail(f"Resolving {dep_name}...")
            return
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
            ref_suffix: DEPRECATED — legacy callers still pass this.
        """
        if ref and dep_name.endswith(f"#{ref}"):
            # The header appends the ref itself; never print it twice.
            dep_name = dep_name[: -len(ref) - 1]
        msg = f"  [+] {dep_name}"
        if ref_suffix:
            # Legacy path — pass-through until all callers are migrated
            msg += f" ({ref_suffix})"
        else:
            if ref and sha:
                msg += f" #{ref} @{sha}"
            elif ref:
                msg += f" #{ref}"
            elif sha:
                msg += f" @{sha}"
            if cached:
                msg += " (cached)"
        self._emit_or_hold(partial(_rich_echo, msg, color="green"))

    @contextmanager
    def held_package_header(self) -> Iterator[list[Callable[[], None]]]:
        """Hold one package's header lines until its integration output starts.

        Install acquires every package before deploying any, so a header
        printed at acquisition would stack above another package's tree.
        """
        held: list[Callable[[], None]] = []
        self._held_header = held
        try:
            yield held
        finally:
            self._held_header = None

    def _emit_or_hold(self, emit: Callable[[], None]) -> None:
        held = getattr(self, "_held_header", None)
        if held is None:
            emit()
        else:
            held.append(emit)

    def download_failed(self, dep_name: str, error: str):
        """Log a download failure."""
        _rich_error(f"  [x] {dep_name} -- {error}")

    # --- Verbose sub-item methods (install-specific) ---

    def lockfile_entry(self, key: str, ref: str = "", sha: str = ""):
        """Log a lockfile entry in verbose mode.

        Omits the line entirely for unpinned deps (no ref, no sha).
        """
        if not self.verbose:
            return
        if sha:
            _rich_echo(f"    {key}: locked at {sha}", color="dim")
        elif ref:
            _rich_echo(f"    {key}: pinned to {ref}", color="dim")
        # Unpinned → omit entirely (nothing useful to show)

    def package_auth(self, source: str, token_type: str = ""):
        """Log auth source for a package (verbose only). 4-space indent."""
        if not self.verbose:
            return
        type_str = f" ({token_type})" if token_type else ""
        self._emit_or_hold(partial(_rich_echo, f"    Auth: {source}{type_str}", color="dim"))

    def package_type_info(self, type_label: str):
        """Log detected package type (verbose only). 4-space indent."""
        if not self.verbose:
            return
        self._emit_or_hold(partial(_rich_echo, f"    Package type: {type_label}", color="dim"))

    # --- Performance diagnostics (perf #1433) ---

    def subdir_download_start(
        self,
        dep_name: str,
        cache_state: str,
        sha_short: str = "",
        sparse_paths: list[str] | None = None,
    ):
        """Log the start of a subdirectory dep download (verbose only).

        Names the dep, the bare-cache state (e.g. ``cold`` / ``warm`` /
        ``persistent`` / ``shared-bare``), the resolved SHA (short),
        and the sparse paths being requested. Surfaces enough state to
        diagnose a perf regression from one log line.
        """
        if not self.verbose:
            return
        sha_part = f" @{sha_short}" if sha_short else ""
        paths_part = f" sparse={','.join(sparse_paths)}" if sparse_paths else " sparse=<none>"
        _rich_echo(
            f"    [i] perf: subdir {dep_name}{sha_part} cache={cache_state}{paths_part}",
            color="dim",
        )

    def bare_clone_strategy(self, strategy: str, elapsed_ms: int):
        """Log the bare-clone strategy and wall time (verbose only).

        ``strategy`` is the human-readable command shape, e.g.
        ``--depth=1 --branch main`` or ``init+fetch --depth=1 <sha>``.
        ``elapsed_ms`` lets readers spot a network-bound regression
        without re-running with a profiler.
        """
        if not self.verbose:
            return
        _rich_echo(
            f"    [i] perf: bare clone strategy={strategy} took={elapsed_ms}ms",
            color="dim",
        )

    def materialize_result(self, dep_name: str, sparse_applied: bool, consumer_size_bytes: int):
        """Log materialization outcome and consumer dir size (verbose only).

        ``sparse_applied`` tells the reader whether sparse-cone fired
        on this consumer dir (sparse_paths were passed and accepted by
        git). ``consumer_size_bytes`` is the on-disk size of the
        working tree handed off to the integrator; a regression here
        is the leading indicator that sparse-cone silently fell back.
        """
        if not self.verbose:
            return
        size_mb = consumer_size_bytes / (1024 * 1024)
        applied = "yes" if sparse_applied else "no"
        _rich_echo(
            f"    [i] perf: materialize {dep_name} sparse={applied} size={size_mb:.2f} MB",
            color="dim",
        )

    def tier_summary(self, stats: dict[str, int]):
        """Log the tiered ref resolver hit counts (verbose only).

        Emitted at the end of the resolve phase so the reader can see
        how many ref->SHA lookups hit each tier (L0 per-run cache,
        L1 commits API, L2 bare rev-parse, L3 legacy clone) without
        wiring a debugger. A run dominated by L3 is the canonical
        signal that ref-resolution is paying full clone cost.
        """
        if not self.verbose or not stats:
            return
        non_zero = {k: v for k, v in stats.items() if v}
        if not non_zero:
            return
        parts = " ".join(f"{k}={v}" for k, v in non_zero.items())
        _rich_echo(f"    [i] perf: ref-resolver tiers: {parts}", color="dim")

    # --- Cleanup phase (stale and orphan file removal) ---

    def stale_cleanup(self, dep_key: str, cleanup: CleanupResult) -> int:
        """Name per-package stale cleanup at default verbosity.

        Stale-file deletion is a destructive operation in the user's
        tracked workspace (unlike npm's ``node_modules``); every removed
        path must be visible without ``--verbose``.
        """
        count = super().stale_cleanup(dep_key, cleanup)
        self._stale_cleaned_total += count
        return count

    def frozen_orphans_kept(self, dep_keys: list[str]) -> None:
        """Name undeclared packages a frozen install left in place (#3662)."""
        _rich_info(
            "Kept packages no longer in apm.yml because --frozen does not change "
            f"apm.lock.yaml: {', '.join(dep_keys)}. {ORPHANED_PACKAGES.sentence()}",
            symbol="info",
        )

    def frozen_stale_kept(self, paths: list[str]) -> None:
        """Name locked files a frozen install left because this version no longer renders them."""
        _rich_info(
            f"Left {len(paths)} file(s) recorded in apm.lock.yaml that this APM version "
            f"no longer deploys, because --frozen does not change apm.lock.yaml: "
            f"{', '.join(paths)}. {LEGACY_RENDER_MIGRATE.sentence()}",
            symbol="info",
        )

    def orphan_cleanup(self, cleanups: Sequence[CleanupResult]) -> int:
        """Name post-install orphan cleanup at default verbosity.

        Same visibility rationale as :meth:`stale_cleanup`: file deletion
        in the user's workspace must be visible by default.
        """
        paths = [path for cleanup in cleanups for path in cleanup.announced_paths]
        count = self._removed_paths(
            paths, lambda noun: f"{noun} from packages no longer in apm.yml"
        )
        self._stale_cleaned_total += count
        return count

    @property
    def stale_cleaned_total(self) -> int:
        """Total files removed by stale + orphan cleanup during this install."""
        return self._stale_cleaned_total

    def cleanup_skipped_user_edit(self, rel_path: str, dep_key: str):
        """Log a stale-file deletion that was skipped because the user
        edited the file after APM deployed it.

        Yellow inline at default verbosity -- the user needs to know APM
        kept the file and a manual decision is pending.
        """
        _rich_warning(
            f"  Kept user-edited file {rel_path} (from {_dependency_label(dep_key)}); "
            "delete manually if no longer needed",
            symbol="warning",
        )

    # --- Policy phase ---

    def policy_resolved(
        self,
        source: str,
        cached: bool,
        enforcement: str,
        age_seconds: int | None = None,
        subgroup_scoped: bool = False,
        chain: list[str] | None = None,
        explicit: bool = False,
    ):
        """Log policy discovery outcome.

        Verbose by default; always shown when ``enforcement == "block"``
        (users must know blocking is active) or when ``subgroup_scoped``
        is set and enforcement is not ``off`` -- a GitLab subgroup policy
        composed with its ancestors names its ordered extends chain even at
        default verbosity (#2753). An ``explicit`` ``--policy`` source is
        likewise always named so the user sees it was applied (#1925).

        Format: ``[i] Policy: <source> (cached, fetched 5m ago) -- enforcement=block``
        """
        if not self._first_policy_banner("resolved", source, enforcement):
            return
        parts = [f"Policy: {source}"]
        if chain and len(chain) > 1:
            # The leaf is already the source; show its ancestors root first.
            parts.append(f"(extends chain: {' -> '.join(chain[:-1])})")

        if cached:
            cache_detail = "cached"
            if age_seconds is not None:
                if age_seconds < 60:
                    cache_detail += f", fetched {age_seconds}s ago"
                else:
                    minutes = age_seconds // 60
                    unit = "m" if minutes < 60 else "h"
                    value = minutes if minutes < 60 else minutes // 60
                    cache_detail += f", fetched {value}{unit} ago"
            parts.append(f"({cache_detail})")
        parts.append(f"-- enforcement={enforcement}")

        message = " ".join(parts)

        if enforcement == "block":
            # Always visible — blocking installs is a big deal
            _rich_warning(message, symbol="warning")
        elif self.verbose or explicit or (subgroup_scoped and enforcement != "off"):
            # GitLab subgroup composition: show the chain even in non-verbose
            # warn mode so the dev sees which namespaces applied (#2753);
            # an ``off`` policy needs no action and stays silent.
            _rich_info(message, symbol="info")
        # Non-verbose + non-block + top-level: silent (no noise for warn/off)

    def policy_discovery_miss(
        self,
        outcome: str,
        source: str = "",
        error: str | None = None,
        host_org: str | None = None,
        hint: str | None = None,
    ):
        """Log a policy-discovery non-success outcome.

        Single canonical helper that routes all 7 non-found / non-disabled
        outcomes through one wording table.  Replaces the per-call-site
        ``_rich_info`` / ``_rich_warning`` invocations in ``policy_gate``
        and ``install_preflight`` (Logging C1 / C2, UX F1 / F2 / F4 / F5).

        Args:
            outcome: One of ``"absent"``, ``"no_git_remote"``, ``"empty"``,
                ``"malformed"``, ``"cache_miss_fetch_fail"``,
                ``"garbage_response"``, ``"cached_stale"``.
            source: Policy source string (e.g. ``"org:acme/.github"``).
            error: Optional error string (used for malformed,
                cache_miss_fetch_fail, garbage_response, cached_stale).
            host_org: Optional org slug for ``absent`` outcome (verbose
                hint).  Auto-derived from ``source`` when not provided.
            hint: Optional discovery diagnostic (``PolicyFetchResult.
                discovery_hint``), shown with ``absent`` in verbose mode only.
        """
        if not self._first_policy_banner("miss", outcome, source):
            return
        err_text = error or "unknown"

        if outcome == "absent":
            # Verbose-only: the vast majority of users have no org policy
            # and don't need to see a line for it on every install (UX F1).
            if not self.verbose:
                return
            org = host_org or _strip_source_prefix(source) or "this project"
            _rich_info(f"No org policy found for {org}", symbol="info")
            if hint:
                _rich_info(hint, symbol="info")
            return

        if outcome == "no_git_remote":
            # UX F2: this is a normal state for fresh `git init`, unpacked
            # bundles, or temp dirs -- info, not a warning.  Verbose-gated
            # for the same reason as ``absent`` (#832): the vast majority
            # of users have no org policy configured and don't need to
            # see a line for it on every install (fresh checkouts, CI
            # environments, unpacked tarballs).
            if not self.verbose:
                return
            _rich_info(
                "Could not determine org from git remote; policy auto-discovery skipped",
                symbol="info",
            )
            return

        if outcome == "empty":
            src = source or "this project"
            _rich_warning(
                f"Org policy at {src} is present but empty; no enforcement applied",
                symbol="warning",
            )
            return

        if outcome == "malformed":
            _rich_warning(
                f"Policy at {source} is malformed: {err_text}. "
                "Contact your org admin to fix the policy file.",
                symbol="warning",
            )
            return

        if outcome == "cache_miss_fetch_fail":
            # UX F5: explicit posture -- enforcement skipped.
            _rich_warning(
                f"Could not fetch org policy from {source} ({err_text}); "
                "proceeding without policy enforcement. "
                "Retry, check connectivity, or run `apm policy status` to diagnose.",
                symbol="warning",
            )
            return

        if outcome == "garbage_response":
            # UX F4: server IS reachable; "check VPN/firewall" is wrong
            # advice.  Point at the org admin instead.
            _rich_warning(
                f"Policy response from {source} is not valid YAML "
                f"({err_text}); proceeding without policy enforcement. "
                "Contact your org admin to fix the policy source.",
                symbol="warning",
            )
            return

        if outcome == "cached_stale":
            # UX F5: explicit posture -- enforcement still applies.
            _rich_warning(
                f"Using stale cached policy (refresh failed: {err_text}); "
                "enforcement still applies from cached policy.",
                symbol="warning",
            )
            return

        if outcome == "hash_mismatch":
            # #827: always-error posture -- pinned policy.hash does not
            # match fetched bytes. Show both expected and actual via the
            # error message so the admin can compare without re-fetching.
            _rich_error(
                f"Policy hash mismatch: pinned hash does not match fetched "
                f"policy ({err_text}). Update apm.yml policy.hash or "
                "contact your org admin.",
                symbol="error",
            )
            return

        # Defensive: unknown outcome -- emit a conservative warning
        if error:
            _rich_warning(
                f"Policy discovery issue: {err_text}",
                symbol="warning",
            )

    def policy_violation(
        self,
        dep_ref: str,
        reason: str,
        severity: str,
        source: str | None = None,
        *,
        remedy: PolicyRemedy | None = None,
        declared_by: str | None = None,
        rule: str | None = None,
    ):
        """Record a policy violation for a dependency.

        Pushes to ``DiagnosticCollector`` under ``CATEGORY_POLICY`` for
        the end-of-install summary.  When ``severity == "block"``, also
        prints an inline error so the user sees the failure immediately
        (before the summary), followed by a dim secondary line with the
        actionable next-step (CLI logging C3).

        Args:
            dep_ref: Dependency reference (e.g. ``"acme/evil-pkg"``).
            reason: Actionable reason text per rubber-duck I9.
            severity: ``"block"`` or ``"warn"``.
            source: Optional policy source (used for block-mode next-step
                hint).  When provided, a dim secondary line with
                remediation guidance is rendered under the inline error.
            remedy: apm.yml edit the hint suggests, a
                :class:`~apm_cli.policy.models.PolicyRemedy` member. ``None``
                (the default) or any other value suggests no apm.yml edit.
            declared_by: Direct apm.yml dependency (lock key) that brings
                in a transitive MCP server; the hint names it instead of
                *dep_ref*.
            rule: Policy check (category) that fired, named in the hint
                so the user can find the policy field.
        """

        # F9 dedupe: some callers pass reason with a "{dep_ref}: " prefix
        # (the detail strings produced by policy_checks.py do this).
        # Strip it defensively so the inline error reads cleanly.
        prefix = f"{dep_ref}: "
        if reason.startswith(prefix):
            reason = reason[len(prefix) :]

        self.diagnostics.policy(
            message=reason,
            package=dep_ref,
            severity=severity,
        )

        if severity == "block":
            self.refusal(f"Policy violation: {dep_ref} -- {reason}")
            if source:
                _rich_echo(
                    "  "
                    + self._policy_reason_blocked(
                        dep_ref,
                        source,
                        remedy=remedy,
                        declared_by=declared_by,
                        rule=rule,
                        refused_add=self._refused_add(declared_by, dep_ref),
                    ),
                    color="dim",
                    err=True,
                )

    def record_adding(self, packages: Iterable[str]) -> None:
        """Name the packages this ``apm install <pkg>`` adds to apm.yml (RA-015b)."""
        self._adding = frozenset(filter(None, map(_dependency_identity, packages)))

    def _refused_add(self, *refs: str | None) -> bool:
        return any(_dependency_identity(ref) in self._adding for ref in refs if ref)

    def _first_policy_banner(self, *key: str) -> bool:
        """Record a policy banner; False when this invocation already showed it.

        The package phase and the MCP phase both route the same policy
        outcome, so every policy banner is printed once per invocation.
        """
        if key in self._policy_banners:
            return False
        self._policy_banners.add(key)
        return True

    def policy_source_unavailable(
        self, source: str, error: str, blocking: bool = False, explicit: bool = False
    ):
        """Log a composed policy source that could not be loaded (#1925).

        Always visible: a source that silently drops out would hide a gap in
        enforcement. The line names whether the explicit ``--policy`` source
        or the discovered policy failed. With ``blocking`` the caller fails
        closed right after, so the line is an error instead of a warning; an
        explicit source always blocks.
        """
        kind = "Explicit" if explicit else "Discovered"
        if not self._first_policy_banner("unavailable", kind, source, error, str(blocking)):
            return
        if blocking:
            _rich_error(
                f"{kind} policy source {source} could not be loaded: {error}", symbol="error"
            )
            return
        _rich_warning(
            f"{kind} policy source {source} could not be loaded ({error}); continuing "
            "without it. Set policy.fetch_failure_default: block in apm.yml to fail closed.",
            symbol="warning",
        )

    def policy_disabled(self, reason: str):
        """Log a loud warning that policy enforcement is disabled.

        Emitted when ``--no-policy`` or ``APM_POLICY_DISABLE=1`` is
        active.  Always visible (never silenceable) -- matches the
        ``--allow-insecure`` pattern.  Shown once per invocation even when
        several install phases check the escape hatch.
        """
        if not self._first_policy_banner("disabled", reason):
            return
        _rich_warning(
            f"Policy enforcement disabled by {reason} for this invocation. "
            "This does NOT bypass apm audit --ci. "
            "CI will still fail the PR for the same policy violation.",
            symbol="warning",
        )

    # --- Policy violation reason helpers ---

    @staticmethod
    def _policy_reason_auth(source: str) -> str:
        """Actionable reason for auth failure during policy fetch."""
        return (
            f"Could not authenticate to fetch policy from {source} "
            "-- check `gh auth status` and `GITHUB_APM_PAT`"
        )

    @staticmethod
    def _policy_reason_unreachable(source: str) -> str:
        """Actionable reason for unreachable policy source; never a bypass (RA-015)."""
        return (
            f"Policy source {source} is unreachable -- retry, check VPN/firewall, "
            "or run `apm policy status` to diagnose"
        )

    @staticmethod
    def _policy_reason_malformed(source: str) -> str:
        """Actionable reason for malformed policy file."""
        return f"Policy at {source} is malformed -- contact your org admin to fix the policy file"

    @staticmethod
    def _policy_reason_blocked(
        dep_ref: str,
        source: str,
        *,
        remedy: PolicyRemedy | None = None,
        declared_by: str | None = None,
        rule: str | None = None,
        refused_add: bool = False,
    ) -> str:
        """Actionable reason for a blocked dependency; never a policy bypass (RA-015).

        Only :class:`~apm_cli.policy.models.PolicyRemedy` members render an
        apm.yml edit; ``None`` or an unknown remedy renders none. A package
        this command was adding is not in apm.yml, so its remedy is to choose
        another package (RA-015b).
        """
        from apm_cli.policy.models import PolicyRemedy

        if refused_add:
            edit = "Choose a package the policy allows, or contact"
        elif remedy == PolicyRemedy.ADD:
            edit = f"Add `{dep_ref}` to apm.yml, or contact"
        elif remedy == PolicyRemedy.PIN:
            edit = f"Pin `{dep_ref}` to a semver range, tag, or SHA in apm.yml, or contact"
        elif remedy == PolicyRemedy.REMOVE and declared_by:
            edit = f"Remove `{declared_by}` (it brings in `{dep_ref}`) from apm.yml, or contact"
        elif remedy == PolicyRemedy.REMOVE:
            edit = f"Remove `{dep_ref}` from apm.yml, or contact"
        else:
            edit = "Contact"
        rule_part = f" ({rule})" if rule and rule != dep_ref else ""
        return (
            f"Blocked by org policy at {source}{rule_part}. Nothing was changed. "
            f"{edit} your policy admin to allow it. "
            "Run `apm policy status` to see the active policy."
        )

    # --- Install summary ---

    def install_summary(
        self,
        apm_count: int,
        mcp_count: int,
        lsp_count: int = 0,
        errors: int = 0,
        stale_cleaned: int = 0,
        elapsed_seconds: float | None = None,
        disposition: InstallDisposition = InstallDisposition.SUCCESS,
    ):
        """Log final install summary.

        Args:
            apm_count: Number of APM dependencies installed.
            mcp_count: Number of MCP servers installed.
            lsp_count: Number of LSP servers installed.
            errors: Number of errors collected during install.
            stale_cleaned: Total stale + orphan files removed during
                this install. Reported as a parenthetical so existing
                callers and assertion patterns continue to work.
            elapsed_seconds: Wall-clock duration of the install command.
                When provided, appended as `` in {x:.1f}s`` before the
                terminating period so the user can see how long the
                whole command took (F5, microsoft/apm#1116).
        """
        parts = []
        if apm_count > 0:
            noun = "dependency" if apm_count == 1 else "dependencies"
            parts.append(f"{apm_count} APM {noun}")
        if mcp_count > 0:
            noun = "server" if mcp_count == 1 else "servers"
            parts.append(f"{mcp_count} MCP {noun}")
        if lsp_count > 0:
            noun = "server" if lsp_count == 1 else "servers"
            parts.append(f"{lsp_count} LSP {noun}")

        cleanup_suffix = ""
        if stale_cleaned > 0:
            file_noun = "file" if stale_cleaned == 1 else "files"
            cleanup_suffix = f" ({stale_cleaned} stale {file_noun} cleaned)"

        timing_suffix = ""
        if elapsed_seconds is not None:
            timing_suffix = f" in {elapsed_seconds:.1f}s"

        if disposition is InstallDisposition.DRY_RUN:
            if self._dry_run_incomplete:
                noun = "dependency" if apm_count == 1 else "dependencies"
                _rich_warning(
                    f"Dry run incomplete: {apm_count} locally known APM {noun}. "
                    f"{DRY_RUN_INCOMPLETE.sentence()}",
                    symbol="warning",
                )
                return
            dry_parts = []
            if apm_count > 0:
                noun = "dependency" if apm_count == 1 else "dependencies"
                dry_parts.append(f"install {apm_count} APM {noun}")
            if mcp_count > 0:
                noun = "server" if mcp_count == 1 else "servers"
                dry_parts.append(f"configure {mcp_count} MCP {noun}")
            if lsp_count > 0:
                noun = "server" if lsp_count == 1 else "servers"
                dry_parts.append(f"configure {lsp_count} LSP {noun}")
            update_count = min(self._dry_run_apm_update_count, apm_count)
            if update_count:
                update_noun = "update" if update_count == 1 else "updates"
                dry_parts = [f"{update_count} APM dependency {update_noun}"]
                install_count = apm_count - update_count
                if install_count:
                    noun = "dependency" if install_count == 1 else "dependencies"
                    dry_parts.append(f"install {install_count} APM {noun}")
                if mcp_count:
                    noun = "server" if mcp_count == 1 else "servers"
                    dry_parts.append(f"configure {mcp_count} MCP {noun}")
                if lsp_count:
                    noun = "server" if lsp_count == 1 else "servers"
                    dry_parts.append(f"configure {lsp_count} LSP {noun}")
                summary = " and ".join(dry_parts)
                _rich_info(
                    f"Dry run completed: would apply {summary}{timing_suffix}.",
                    symbol="info",
                )
                return
            summary = " and ".join(dry_parts) if dry_parts else "make no changes"
            _rich_info(
                f"Dry run completed: would {summary}{timing_suffix}.",
                symbol="info",
            )
        elif disposition is InstallDisposition.FAILED:
            error_suffix = f" with {errors} error(s)" if errors > 0 else ""
            state_note = (
                self._failed_install_residue_note
                or "No install transaction changes were committed."
            )
            _rich_error(
                f"Installation failed{error_suffix}{timing_suffix}. {state_note}",
                symbol="error",
            )
        elif parts:
            summary = " and ".join(parts)
            if self._local_content_updated:
                summary = f"{summary} and updated local content"
            if errors > 0:
                _rich_warning(
                    f"Installed {summary}{cleanup_suffix}{timing_suffix} with {errors} error(s).",
                    symbol="warning",
                )
            elif disposition is InstallDisposition.PARTIAL_SUCCESS:
                _rich_warning(
                    f"Installed {summary}{cleanup_suffix}{timing_suffix}; "
                    "some requested packages were skipped.",
                    symbol="warning",
                )
            else:
                _rich_success(
                    f"Installed {summary}{cleanup_suffix}{timing_suffix}.",
                    symbol="sparkles",
                )
        elif errors > 0:
            _rich_error(
                f"Installation failed with {errors} error(s){timing_suffix}.",
                symbol="error",
            )
        elif self._local_content_updated:
            _rich_success(
                f"Updated local content{cleanup_suffix}{timing_suffix}.",
                symbol="sparkles",
            )
        elif stale_cleaned > 0:
            file_noun = "file" if stale_cleaned == 1 else "files"
            _rich_success(
                f"Cleaned {stale_cleaned} stale {file_noun}{timing_suffix}.",
                symbol="sparkles",
            )
        elif self._mcp_removed:
            count = len(self._mcp_removed)
            noun = "server" if count == 1 else "servers"
            _rich_success(
                f"Removed {count} stale MCP {noun}{timing_suffix}.",
                symbol="sparkles",
            )
        elif self._lockfile_written:
            _rich_success(f"Updated apm.lock.yaml{timing_suffix}.", symbol="success")
        else:
            _rich_info(
                f"No changes -- install state already up to date{timing_suffix}.",
                symbol="info",
            )

    def install_interrupted(self, elapsed_seconds: float):
        """Log a minimal elapsed-time line when the normal summary did
        not render (errors, KeyboardInterrupt, click.UsageError).

        Emitted from the outer ``finally`` in ``commands.install.install``
        so users always see how long the failed/interrupted command ran
        (F5, microsoft/apm#1116). Best-effort: callers swallow any
        exception so a render failure cannot mask the original error.
        """
        _rich_warning(
            f"Install interrupted after {elapsed_seconds:.1f}s.",
            symbol="warning",
        )

    def install_failed(self, elapsed_seconds: float) -> None:
        """Render a handled terminal failure when no full summary ran."""
        _rich_error(
            f"Install failed after {elapsed_seconds:.1f}s.",
            symbol="error",
        )
