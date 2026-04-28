"""Canonical exception types for the install pipeline.

Centralises typed errors raised by the install machinery so call sites
in ``commands/install.py``, ``install/pipeline.py``, ``install/phases/``,
and ``policy/install_preflight.py`` can ``except`` a single class.

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

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from apm_cli.policy.models import CIAuditResult


class DirectDependencyError(RuntimeError):
    """Raised when one or more direct dependencies fail validation or integration.

    Bypasses the broad ``except Exception`` wrapper in ``pipeline.py`` so the
    original message reaches ``commands/install.py`` without being double-wrapped
    as ``"Failed to resolve APM dependencies: ..."`` (same pattern as
    :class:`PolicyViolationError`).
    """


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
        audit_result: "Optional[CIAuditResult]" = None,
        policy_source: str = "",
    ):
        super().__init__(message)
        self.audit_result = audit_result
        self.policy_source = policy_source
