"""Canonical exception types for the install pipeline.

Centralises typed errors raised by the install machinery so call sites
in ``commands/install.py``, ``install/pipeline.py``, ``install/phases/``,
and ``policy/install_preflight.py`` can ``except`` a single class.

Exception hierarchy
-------------------
* :class:`DirectDependencyError` -- one or more deps failed validation.
* :class:`PolicyViolationError` -- org-policy enforcement halted install.
* :class:`AuthenticationError`  -- remote-host auth failure (PAT rejected,
  bearer rejected, no credentials available).  Carries a pre-rendered
  ``diagnostic_context`` produced by
  :meth:`~apm_cli.core.auth.AuthResolver.build_error_context` so the
  renderer in ``commands/install.py`` can display actionable guidance on
  the **default** output path (not ``--verbose``-gated).  Added in #1015.

Historical note
---------------
Two classes carried the same semantic until #832: ``PolicyViolationError``
(raised from ``install/phases/policy_gate.py``) and ``PolicyBlockError``
(raised from ``policy/install_preflight.py``).  They are now consolidated
on :class:`PolicyViolationError` here.  ``PolicyBlockError`` remains as
a deprecated alias re-exported from ``policy/install_preflight`` so any
external callers keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.policy.models import CIAuditResult
    from apm_cli.security.trust import McpAdmission


class DirectDependencyError(RuntimeError):
    """Raised when one or more direct dependencies fail validation or integration.

    Bypasses the broad ``except Exception`` wrapper in ``pipeline.py`` so the
    original message reaches ``commands/install.py`` without being double-wrapped
    as ``"Failed to resolve APM dependencies: ..."`` (same pattern as
    :class:`PolicyViolationError`).
    """


class PreviewIncomplete(Exception):
    """``--dry-run`` stops where install would refuse; errors are on the context."""


class InstallFailureAlreadyRendered(RuntimeError):
    """Signal a failed install whose user-facing diagnostics are complete."""


class DependencyResolutionError(RuntimeError):
    """Dependency resolution failed; *unprinted* lists the errors not yet shown (#5272).

    The resolve phase prints each error with its remedy as it raises, so the
    install summary repeats only the ones it did not print.
    """

    def __init__(self, errors: list[str], *, unprinted: list[str]) -> None:
        super().__init__(f"Dependency resolution failed: {'; '.join(errors)}")
        self.unprinted = tuple(unprinted)
        self.printed = len(errors) - len(self.unprinted)

    def summary(self) -> str:
        """Name the failure without repeating an error already printed above."""
        if self.unprinted:
            return f"Dependency resolution failed: {'; '.join(self.unprinted)}"
        noun = "error" if self.printed == 1 else "errors"
        return f"Dependency resolution failed (see the {noun} above)."


class MCPNativeConflictError(InstallFailureAlreadyRendered):
    """Native connections were preserved; actual nonconflicting writes may remain."""

    def __init__(self, message: str, *, wrote_connections: bool) -> None:
        super().__init__(message)
        self.wrote_connections = wrote_connections


class LSPUserEntryConflictError(InstallFailureAlreadyRendered):
    """Same-name LSP entries APM did not write were preserved (#4628), or
    managed entries a user edited were kept with no collector to count them (#4626).

    *preserved* maps each runtime to the server names left as the user wrote
    them; every other server in the install was still configured, and
    *wrote_servers* says whether any of them changed a config.
    """

    def __init__(
        self, message: str, *, preserved: dict[str, set[str]], wrote_servers: bool = True
    ) -> None:
        super().__init__(message)
        self.preserved = preserved
        self.wrote_servers = wrote_servers


class LSPUserEntryRefusal(LSPUserEntryConflictError):
    """An LSP user-entry conflict that wrote no server: the install commits nothing (#4697)."""

    # A check refusal: the lockfile writer scope commits nothing (#4085).
    refuses_commit = True


class RequiredIntegrationError(RuntimeError):
    """Raised when a declared MCP or LSP write cannot be completed."""


class AuthenticationError(RuntimeError):
    """Raised when a remote host rejects credentials or none are available.

    Parameters
    ----------
    message:
        Short summary suitable for the ``_rich_error`` header line
        (e.g. ``"Authentication failed for dev.azure.com"``).
    diagnostic_context:
        Pre-rendered multi-line guidance produced by
        :meth:`~apm_cli.core.auth.AuthResolver.build_error_context`.
        Embedded at raise time so the renderer never re-resolves.
    """

    def __init__(self, message: str, *, diagnostic_context: str = ""):
        super().__init__(message)
        self.diagnostic_context = diagnostic_context


class FrozenInstallError(RuntimeError):
    """Raised when ``apm install --frozen`` cannot proceed.

    Three trigger conditions:

    * Lockfile (``apm.lock.yaml``) is missing entirely.
    * Lockfile is structurally out of sync with ``apm.yml`` -- a direct
      dependency declared in the manifest has no entry in the lockfile.
      In that case ``reasons`` carries one human-readable line per
      missing dep so the renderer can list them.
    * The install deploys files the committed lockfile does not record, so
      honouring req-lk-006's "never written or rewritten" would have left
      the project claiming less than it deploys -- and unclaimed files are
      outside the audit's content checks.  ``reasons`` names those paths.

    The first two are structural and run before the pipeline.  Drift in
    transitive deps or removed deps is allowed, mirroring how ``uv``
    treats ``--frozen`` and how ``npm ci`` only enforces direct-deps
    presence; the third follows the same rule and ignores claims the
    install would *drop*.

    ``tip`` overrides the drift remediation line the CLI prints, carried on
    the error because the no-rewrite check has a different remedy and both
    Click handlers render this exception the same way.
    """

    def __init__(
        self,
        message: str,
        *,
        reasons: list[str] | None = None,
        tip: str | None = None,
    ):
        super().__init__(message)
        self.reasons = list(reasons or [])
        self.tip = tip


def symlinked_deploy_cause(error: BaseException) -> Exception | None:
    """The symlinked-deploy refusal behind *error*, however it was wrapped (#5287)."""
    from apm_cli.utils.path_security import SymlinkedDeployError

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, SymlinkedDeployError):
            return current
        seen.add(id(current))
        # An implicit context counts only when not suppressed (``raise ... from None``).
        context = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or context
    return None


def install_failure_message(error: Exception) -> str:
    """Keep canonical lockfile guidance intact and frame other install failures."""
    from apm_cli.deps.lockfile import LockfileConflictError, LockfileRefusal

    if (refusal := symlinked_deploy_cause(error)) is not None:
        return str(refusal)
    return (
        str(error)
        if isinstance(error, (LockfileConflictError, LockfileRefusal))
        else f"Error installing dependencies: {error}"
    )


def log_frozen_install_error(error: FrozenInstallError, logger: InstallLogger) -> None:
    """Render the same frozen diagnostic and optional tip in both CLI handlers."""
    logger.error(str(error))
    for reason in error.reasons:
        logger.error_detail(reason)
    if tip := frozen_install_tip(error):
        logger.info(tip)


def log_authentication_error(error: AuthenticationError, logger: InstallLogger) -> None:
    """Render auth diagnostics on the default path, not only under --verbose (#1015)."""
    logger.error(str(error))
    if error.diagnostic_context:
        logger.error_detail(error.diagnostic_context)
    logger.info("Tip: run 'apm doctor' to diagnose auth and connectivity.")


def render_apm_dependency_failure(
    error: Exception, logger: InstallLogger, *, verbose: bool
) -> InstallFailureAlreadyRendered:
    """Render a failed APM dependency phase once; the caller raises the returned marker.

    Auth and frozen errors use their own renderers. A native plugin settings refusal
    or the LSP capability gate is already actionable, so it prints verbatim with any
    raw decoder detail behind --verbose. A policy violation prints as a refusal
    without nesting (#832); anything else gets the "Failed to install" prefix.
    """
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError
    from apm_cli.copilot_plugins.settings import CopilotSettingsCollisionError
    from apm_cli.utils.path_security import SymlinkedDeployError

    if isinstance(error, AuthenticationError):
        log_authentication_error(error, logger)
        return InstallFailureAlreadyRendered(str(error))
    if isinstance(error, FrozenInstallError):
        log_frozen_install_error(error, logger)
        return InstallFailureAlreadyRendered(str(error))
    error = symlinked_deploy_cause(error) or error
    verbatim = (
        CopilotSettingsCollisionError,
        ClaudeNativePluginError,
        RequiredIntegrationError,
        SymlinkedDeployError,
    )
    if isinstance(error, verbatim):
        logger.error(str(error))
        if detail := getattr(error, "detail", None):
            logger.verbose_detail(detail)
        return InstallFailureAlreadyRendered(str(error))
    if isinstance(error, PolicyViolationError):
        message = str(error)
        logger.refusal(message)
    else:
        cause = error.summary() if isinstance(error, DependencyResolutionError) else error
        message = f"Failed to install APM dependencies: {cause}"
        logger.error(message)
    if not verbose:
        logger.progress("Run with --verbose for detailed diagnostics")
    return InstallFailureAlreadyRendered(message)


def frozen_install_tip(error: FrozenInstallError) -> str:
    """Return recovery guidance tailored to package or MCP lock drift.

    An explicit empty tip suppresses drift advice when the error already
    names its own action, including missing and unreadable lockfiles.
    """
    if error.tip is not None:
        return error.tip  # the no-rewrite check carries its own remedy
    has_mcp_drift = any("MCP server" in reason for reason in error.reasons)
    has_package_drift = any("MCP server" not in reason for reason in error.reasons)
    if has_mcp_drift and has_package_drift:
        lock_state = "package and MCP lock state"
    elif has_mcp_drift:
        lock_state = "MCP lock state"
    else:
        lock_state = "package lock state"
    from apm_cli.core.remediation import FROZEN_LOCK_STATE_DRIFT

    return f"Tip: {FROZEN_LOCK_STATE_DRIFT.bind(lock_state=lock_state).sentence()}"


class PolicyViolationError(RuntimeError):
    """Raised when org-policy enforcement halts an install.

    Attributes
    ----------
    audit_result:
        Optional :class:`~apm_cli.policy.models.CIAuditResult` containing
        the failed checks that triggered the block.  ``None`` when the
        block stems from a discovery-level failure (hash_mismatch, fetch
        failure under ``fetch_failure_default=block``) rather than from
        per-dependency check evaluation.
    policy_source:
        Human-readable origin string (e.g. ``"org:acme/.github"``).  May
        be empty when discovery failed before a source was resolved.
    """

    # A check refusal: the lockfile writer scope commits nothing (#4085).
    refuses_commit = True

    def __init__(
        self,
        message: str,
        *,
        audit_result: CIAuditResult | None = None,
        policy_source: str = "",
    ):
        super().__init__(message)
        self.audit_result = audit_result
        self.policy_source = policy_source


def policy_transitive_refusals(view: CurrentMcpConfigView) -> list[str]:
    """Transitive servers the trust owner withheld under org ``mcp.trust_transitive``."""
    from apm_cli.security.trust import REASON_POLICY_TRANSITIVE

    return sorted(
        name
        for name, evidence in view.omissions.items()
        if isinstance(evidence, dict) and evidence.get("reason") == REASON_POLICY_TRANSITIVE
    )


def refuse_policy_transitive(view: CurrentMcpConfigView, admission: McpAdmission) -> None:
    """Fail the install when an org policy that blocks refuses transitive servers.

    Under ``mcp.trust_transitive: false`` the trust owner parks every
    transitive server ``mcp.allow`` does not list; with ``enforcement: block``
    the install stops before any write, as the direct mcp-allowlist check does.
    """
    refused = policy_transitive_refusals(view)
    policy = admission.org_policy()
    if not refused or getattr(policy, "enforcement", None) != "block":
        return
    raise PolicyViolationError(
        "Install blocked by org policy: mcp.trust_transitive is false and mcp.allow does "
        f"not list transitive MCP server(s) {', '.join(refused)}. Ask the policy owner to "
        "add them to mcp.allow or set mcp.trust_transitive: true."
    )


def frozen_unrecorded_reason(service: str, name: str, target: str) -> str:
    """One ``--frozen`` refusal line for an unrecorded service claim."""
    return f"  - {service} server {name!a} target {target!a} ownership is not recorded in apm.lock.yaml"
