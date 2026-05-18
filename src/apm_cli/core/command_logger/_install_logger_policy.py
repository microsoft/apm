"""Policy-phase mixin for :class:`~.__init__.InstallLogger`.

Extracted from :mod:`__init__` to keep that module under 400 lines.
:class:`InstallLogger` gains these methods via multiple inheritance:
``class InstallLogger(CommandLogger, _PolicyLoggingMixin)``.
"""

from __future__ import annotations

from ._messages import _policy_miss_spec, _policy_resolved_msg


def _rich_info(*args, **kwargs):
    from . import _rich_info as package_rich_info

    return package_rich_info(*args, **kwargs)


def _rich_warning(*args, **kwargs):
    from . import _rich_warning as package_rich_warning

    return package_rich_warning(*args, **kwargs)


def _rich_error(*args, **kwargs):
    from . import _rich_error as package_rich_error

    return package_rich_error(*args, **kwargs)


def _rich_echo(*args, **kwargs):
    from . import _rich_echo as package_rich_echo

    return package_rich_echo(*args, **kwargs)


class _PolicyLoggingMixin:
    """Policy-phase logging methods for :class:`InstallLogger`.

    Accesses ``self.verbose`` and ``self.diagnostics`` which are provided
    by :class:`CommandLogger` through the MRO.
    """

    # --- Policy phase ---

    def policy_resolved(
        self,
        source: str,
        cached: bool,
        enforcement: str,
        age_seconds: int | None = None,
    ):
        """Log policy discovery outcome.

        Always visible when ``enforcement == "block"``.  Verbose-only for
        ``warn`` and ``off`` to avoid noise on most installs.
        """
        message = _policy_resolved_msg(source, cached, enforcement, age_seconds)
        if enforcement == "block":
            _rich_warning(message, symbol="warning")
        elif self.verbose:
            _rich_info(message, symbol="info")
        # Non-verbose + non-block: silent (no noise for warn/off)

    def policy_discovery_miss(
        self,
        outcome: str,
        source: str = "",
        error: str | None = None,
        host_org: str | None = None,
    ):
        """Log a policy-discovery non-success outcome.

        Routes all 7 non-found / non-disabled outcomes through a single
        wording table.  ``absent`` and ``no_git_remote`` are verbose-only.

        Args:
            outcome: One of ``"absent"``, ``"no_git_remote"``, ``"empty"``,
                ``"malformed"``, ``"cache_miss_fetch_fail"``,
                ``"garbage_response"``, ``"cached_stale"``,
                ``"hash_mismatch"``.
            source: Policy source string (e.g. ``"org:acme/.github"``).
            error: Optional error detail for malformed / fetch-fail /
                garbage / stale outcomes.
            host_org: Optional org slug for ``absent`` verbose hint.
        """
        spec = _policy_miss_spec(outcome, source, error or "unknown", host_org, self.verbose)
        if spec is None:
            return
        style, message = spec
        if style == "info":
            _rich_info(message, symbol="info")
        elif style == "warning":
            _rich_warning(message, symbol="warning")
        elif style == "error":
            _rich_error(message, symbol="error")

    def policy_violation(
        self,
        dep_ref: str,
        reason: str,
        severity: str,
        source: str | None = None,
    ):
        """Record a policy violation for a dependency.

        Pushes to ``DiagnosticCollector`` under ``CATEGORY_POLICY``.  When
        ``severity == "block"``, also prints an inline error and a dim
        secondary line with remediation guidance.

        Args:
            dep_ref: Dependency reference (e.g. ``"acme/evil-pkg"``).
            reason: Actionable reason text.
            severity: ``"block"`` or ``"warn"``.
            source: Optional policy source for block-mode hint.
        """
        # F9 dedupe: some callers pass reason with a "{dep_ref}: " prefix.
        # Strip it so the inline error reads cleanly.
        prefix = f"{dep_ref}: "
        if reason.startswith(prefix):
            reason = reason[len(prefix) :]

        self.diagnostics.policy(
            message=reason,
            package=dep_ref,
            severity=severity,
        )

        if severity == "block":
            _rich_error(f"Policy violation: {dep_ref} -- {reason}", symbol="error")
            if source:
                _rich_echo(
                    f"  {self._policy_reason_blocked(dep_ref, source)}",
                    color="dim",
                )

    def policy_disabled(self, reason: str):
        """Log a loud warning that policy enforcement is disabled.

        Always visible (never silenceable) -- matches ``--allow-insecure``.
        """
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
