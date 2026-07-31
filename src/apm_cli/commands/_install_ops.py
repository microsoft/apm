"""Private helpers extracted from commands/install.py.

Extracted to keep install.py within the 800-line per-file guardrail.
Helpers that touch module-level symbols tests may monkeypatch route those
lookups back through the install facade per RULE B.
"""

from typing import TYPE_CHECKING

import click

from ..models.results import InstallDisposition, InstallResult

if TYPE_CHECKING:
    from ..install.errors import FrozenInstallError


def _make_fail_result(exc, transaction):
    """Return a FAILED InstallResult, delegating to transaction.fail if available.

    Centralises the repeated pattern in the install command's except handlers
    so each handler stays at one result-assignment line.
    """
    if transaction is not None:
        return transaction.fail(exc)
    return InstallResult(disposition=InstallDisposition.FAILED, exit_code=1, error=exc)


def _normalize_skill_subset(skill_names, mcp_name):
    """Resolve --skill to a frozen tuple, or None when all skills are requested.

    Returns None when skill_names is empty or contains the '*' wildcard.
    Raises click.UsageError when --skill is combined with --mcp.
    """
    if not skill_names:
        return None
    if mcp_name is not None:
        raise click.UsageError("--skill cannot be combined with --mcp.")
    if not any(s == "*" for s in skill_names):
        return tuple(skill_names)
    return None


def _create_transaction(manifest_path, scope, logger):
    """Create an InstallTransaction for the resolved install scope.

    Uses a lazy import so module load order is not disturbed. InstallTransaction
    is not patched in unit tests, so the direct import is safe (no facade needed).
    """
    from apm_cli.core.scope import get_modules_dir as _get_modules_dir

    from ..install.transaction import InstallTransaction

    return InstallTransaction(
        manifest_path=manifest_path,
        apm_modules_dir=_get_modules_dir(scope),
        validation=None,
        logger=logger,
    )


def _resolve_audit_override(no_audit, audit_mode):
    """Wrap resolve_audit_override_from_cli, converting ValueError to UsageError."""
    from ..core.install_audit import resolve_audit_override_from_cli

    try:
        return resolve_audit_override_from_cli(no_audit, audit_mode)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def _frozen_install_tip(error: "FrozenInstallError") -> str:
    """Return recovery guidance tailored to package or MCP lock drift."""
    has_mcp_drift = any("MCP server" in reason for reason in error.reasons)
    has_package_drift = any("MCP server" not in reason for reason in error.reasons)
    if has_mcp_drift and has_package_drift:
        return (
            "Tip: run 'apm outdated' to inspect package drift, then run "
            "'apm install' without --frozen to repair package and MCP lock state."
        )
    if has_mcp_drift:
        return "Tip: run 'apm install' without --frozen to create or repair MCP lock state."
    return "Tip: run 'apm outdated' to see what changed, then 'apm update'."
