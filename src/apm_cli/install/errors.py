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
    from apm_cli.policy.models import CIAuditResult


class DirectDependencyError(RuntimeError):
    """Raised when one or more direct dependencies fail validation or integration.

    Bypasses the broad ``except Exception`` wrapper in ``pipeline.py`` so the
    original message reaches ``commands/install.py`` without being double-wrapped
    as ``"Failed to resolve APM dependencies: ..."`` (same pattern as
    :class:`PolicyViolationError`).
    """


class InstallFailureAlreadyRendered(RuntimeError):
    """Signal a failed install whose user-facing diagnostics are complete."""


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

    ``tip`` is the remediation line the CLI prints, carried on the error
    because the two conditions have different remedies and both Click
    handlers render this exception the same way.
    """

    DEFAULT_TIP = "Tip: run 'apm outdated' to see what changed, then 'apm update'."

    def __init__(
        self,
        message: str,
        *,
        reasons: list[str] | None = None,
        tip: str = DEFAULT_TIP,
    ):
        super().__init__(message)
        self.reasons = list(reasons or [])
        self.tip = tip


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
