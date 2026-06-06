"""InstallLogger — install-specific phased logging for the APM CLI.

Extracted from command_logger.py (Strangler Stage 2, #1078).
Re-exported from apm_cli.core.command_logger as ``InstallLogger``.

Rule B: All output is routed through ``CommandLogger`` base-class methods
(``self.info``, ``self.warning``, ``self.error``, ``self.success``,
``self.verbose_detail``, ``self.tree_item``, ``self.dim_check_item``,
``self.dim``) so that test patches on ``apm_cli.core.command_logger._rich_*``
are correctly intercepted.
"""

from apm_cli.core.command_logger import CommandLogger, _strip_source_prefix, _ValidationOutcome


class InstallLogger(CommandLogger):
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
        self.info(f"Validating {count} {noun}...", symbol="gear")

    def validation_pass(self, canonical: str, already_present: bool, updated: bool = False):
        """Log a package that passed validation."""
        if updated:
            self.dim_check_item(f"{canonical} (updated ref in apm.yml)")
        elif already_present:
            self.dim_check_item(f"{canonical} (already in apm.yml)")
        else:
            self.success(canonical, symbol="check")

    def validation_fail(self, package: str, reason: str):
        """Log a package that failed validation."""
        self.error(f"{package} -- {reason}")

    def validation_summary(self, outcome: _ValidationOutcome):
        """Log validation summary and decide whether to continue.

        Returns True if install should continue, False if all packages failed.
        """
        if outcome.all_failed:
            self.error("All packages failed validation. Nothing to install.")
            return False

        if outcome.has_failures:
            failed_count = len(outcome.invalid)
            noun = "package" if failed_count == 1 else "packages"
            self.warning(f"{failed_count} {noun} failed validation and will be skipped.")

        return True

    # --- Resolution phase ---

    def resolution_start(self, to_install_count: int, lockfile_count: int):
        """Log start of dependency resolution."""
        if self.partial:
            noun = "package" if to_install_count == 1 else "packages"
            self.start(f"Installing {to_install_count} new {noun}...")
            if lockfile_count > 0 and self.verbose:
                self.verbose_detail(f"  ({lockfile_count} existing dependencies in lockfile)")
        else:
            self.start("Installing dependencies from apm.yml...")
            if lockfile_count > 0:
                self.info(f"Using apm.lock.yaml ({lockfile_count} locked dependencies)")

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
            self.info("Requested packages are already installed.", symbol="check")
        else:
            self.success("All dependencies are up to date.", symbol="check")
        if lockfile_present and not update_mode:
            self.info("Lockfile already satisfied -- run 'apm update' to resolve latest refs.")

    # --- Download phase ---

    def download_start(self, dep_name: str, cached: bool):
        """Log start of a package download."""
        if cached:
            self.verbose_detail(f"  Using cached: {dep_name}")
        elif self.verbose:
            self.info(f"  Downloading: {dep_name}", symbol="download")

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
        when downloads are dispatched to a worker pool.
        """
        self.start(f"Resolving {dep_name}...")

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
        self.tree_item(msg)

    def download_failed(self, dep_name: str, error: str):
        """Log a download failure."""
        self.error(f"  [x] {dep_name} -- {error}")

    # --- Verbose sub-item methods (install-specific) ---

    def lockfile_entry(self, key: str, ref: str = "", sha: str = ""):
        """Log a lockfile entry in verbose mode.

        Omits the line entirely for unpinned deps (no ref, no sha).
        """
        if not self.verbose:
            return
        if sha:
            self.verbose_detail(f"    {key}: locked at {sha}")
        elif ref:
            self.verbose_detail(f"    {key}: pinned to {ref}")
        # Unpinned → omit entirely (nothing useful to show)

    def package_auth(self, source: str, token_type: str = ""):
        """Log auth source for a package (verbose only). 4-space indent."""
        if not self.verbose:
            return
        type_str = f" ({token_type})" if token_type else ""
        self.verbose_detail(f"    Auth: {source}{type_str}")

    def package_type_info(self, type_label: str):
        """Log detected package type (verbose only). 4-space indent."""
        if not self.verbose:
            return
        self.verbose_detail(f"    Package type: {type_label}")

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
        self.verbose_detail(
            f"    [i] perf: subdir {dep_name}{sha_part} cache={cache_state}{paths_part}"
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
        self.verbose_detail(f"    [i] perf: bare clone strategy={strategy} took={elapsed_ms}ms")

    def materialize_result(self, sparse_applied: bool, consumer_size_bytes: int):
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
        self.verbose_detail(f"    [i] perf: materialize sparse={applied} size={size_mb:.2f} MB")

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
        self.verbose_detail(f"    [i] perf: ref-resolver tiers: {parts}")

    # --- Cleanup phase (stale and orphan file removal) ---

    def stale_cleanup(self, dep_key: str, count: int):
        """Log per-package stale-file cleanup outcome at default verbosity.

        Stale-file deletion is a destructive operation in the user's
        tracked workspace (unlike npm's ``node_modules``); it must be
        visible without ``--verbose``. Rendered as an info line so it
        groups visually with other phase messages, not as a tree item
        (the originating package line was emitted earlier in the install
        sequence and is no longer adjacent).
        """
        if count <= 0:
            return
        self._stale_cleaned_total += count
        noun = "file" if count == 1 else "files"
        self.info(f"Cleaned {count} stale {noun} from {dep_key}")

    def orphan_cleanup(self, count: int):
        """Log post-install orphan-file cleanup outcome at default verbosity.

        Same visibility rationale as :meth:`stale_cleanup`: file deletion
        in the user's workspace must be visible by default.
        """
        if count <= 0:
            return
        self._stale_cleaned_total += count
        noun = "file" if count == 1 else "files"
        self.info(f"Cleaned {count} {noun} from packages no longer in apm.yml")

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
        self.warning(
            f"  Kept user-edited file {rel_path} (from {dep_key}); "
            "delete manually if no longer needed"
        )

    # --- Policy phase ---

    def policy_resolved(
        self,
        source: str,
        cached: bool,
        enforcement: str,
        age_seconds: int | None = None,
    ):
        """Log policy discovery outcome.

        Verbose by default; always shown when ``enforcement == "block"``
        (users must know blocking is active).

        Format: ``[i] Policy: <source> (cached, fetched 5m ago) -- enforcement=block``
        """
        parts = [f"Policy: {source}"]

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
            self.warning(message)
        elif self.verbose:
            self.info(message)
        # Non-verbose + non-block: silent (no noise for warn/off)

    def policy_discovery_miss(
        self,
        outcome: str,
        source: str = "",
        error: str | None = None,
        host_org: str | None = None,
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
        """
        err_text = error or "unknown"

        # Merge the two verbose-only early-exit outcomes to stay within the
        # PLR0911 return-statement budget (absent + no_git_remote share the
        # same "silent when not verbose" guard).
        if outcome in ("absent", "no_git_remote"):
            if not self.verbose:
                return
            if outcome == "absent":
                org = host_org or _strip_source_prefix(source) or "this project"
                self.info(f"No org policy found for {org}")
            else:
                # UX F2: normal state for fresh `git init`, unpacked bundles, etc.
                self.info("Could not determine org from git remote; policy auto-discovery skipped")
            return

        if outcome == "empty":
            src = source or "this project"
            self.warning(f"Org policy at {src} is present but empty; no enforcement applied")
            return

        if outcome == "malformed":
            self.warning(
                f"Policy at {source} is malformed: {err_text}. "
                "Contact your org admin to fix the policy file."
            )
            return

        if outcome == "cache_miss_fetch_fail":
            # UX F5: explicit posture -- enforcement skipped.
            self.warning(
                f"Could not fetch org policy from {source} ({err_text}); "
                "proceeding without policy enforcement. "
                "Retry, check connectivity, or use --no-policy to bypass."
            )
            return

        if outcome == "garbage_response":
            # UX F4: server IS reachable; "check VPN/firewall" is wrong advice.
            self.warning(
                f"Policy response from {source} is not valid YAML "
                f"({err_text}); proceeding without policy enforcement. "
                "Contact your org admin or use --no-policy."
            )
            return

        if outcome == "cached_stale":
            # UX F5: explicit posture -- enforcement still applies.
            self.warning(
                f"Using stale cached policy (refresh failed: {err_text}); "
                "enforcement still applies from cached policy."
            )
            return

        if outcome == "hash_mismatch":
            # #827: always-error posture -- pinned policy.hash does not match.
            self.error(
                f"Policy hash mismatch: pinned hash does not match fetched "
                f"policy ({err_text}). Update apm.yml policy.hash or "
                "contact your org admin."
            )
            return

        # Defensive: unknown outcome -- emit a conservative warning
        if error:
            self.warning(f"Policy discovery issue: {err_text}")

    def policy_violation(
        self,
        dep_ref: str,
        reason: str,
        severity: str,
        source: str | None = None,
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
            self.error(f"Policy violation: {dep_ref} -- {reason}")
            if source:
                self.dim(f"  {self._policy_reason_blocked(dep_ref, source)}")

    def policy_disabled(self, reason: str):
        """Log a loud warning that policy enforcement is disabled.

        Emitted when ``--no-policy`` or ``APM_POLICY_DISABLE=1`` is
        active.  Always visible (never silenceable) -- matches the
        ``--allow-insecure`` pattern.
        """
        self.warning(
            f"Policy enforcement disabled by {reason} for this invocation. "
            "This does NOT bypass apm audit --ci. "
            "CI will still fail the PR for the same policy violation."
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
        """Actionable reason for unreachable policy source."""
        return (
            f"Policy source {source} is unreachable "
            "-- retry, check VPN/firewall, or use `--no-policy` to bypass"
        )

    @staticmethod
    def _policy_reason_malformed(source: str) -> str:
        """Actionable reason for malformed policy file."""
        return f"Policy at {source} is malformed -- contact your org admin to fix the policy file"

    @staticmethod
    def _policy_reason_blocked(dep_ref: str, source: str) -> str:
        """Actionable reason for a blocked dependency."""
        return (
            f"Blocked by org policy at {source} "
            f"-- remove `{dep_ref}` from apm.yml, contact admin to update policy, "
            "or use `--no-policy` for one-off bypass"
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

        if parts:
            summary = " and ".join(parts)
            if errors > 0:
                self.warning(
                    f"Installed {summary}{cleanup_suffix}{timing_suffix} with {errors} error(s)."
                )
            else:
                self.success(
                    f"Installed {summary}{cleanup_suffix}{timing_suffix}.",
                    symbol="sparkles",
                )
        elif errors > 0:
            self.error(f"Installation failed with {errors} error(s){timing_suffix}.")
        else:
            self.info(f"No changes -- install state already up to date{timing_suffix}.")

    def install_interrupted(self, elapsed_seconds: float):
        """Log a minimal elapsed-time line when the normal summary did
        not render (errors, KeyboardInterrupt, click.UsageError).

        Emitted from the outer ``finally`` in ``commands.install.install``
        so users always see how long the failed/interrupted command ran
        (F5, microsoft/apm#1116). Best-effort: callers swallow any
        exception so a render failure cannot mask the original error.
        """
        self.warning(f"Install interrupted after {elapsed_seconds:.1f}s.")
