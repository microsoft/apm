"""MCP-specific CI checks extracted from ci_checks.py."""

from __future__ import annotations

from .models import CheckResult


def _check_config_consistency(
    manifest,
    lock,
) -> CheckResult:
    """Verify MCP server configs match lockfile baseline."""
    from ..drift import detect_config_drift
    from ..integration.mcp_integrator import MCPIntegrator

    mcp_deps = manifest.get_mcp_dependencies()
    current_configs = MCPIntegrator.get_server_configs(mcp_deps)
    stored_configs = lock.mcp_configs or {}

    # No MCP deps at all -- nothing to check
    if not current_configs and not stored_configs:
        return CheckResult(
            name="config-consistency",
            passed=True,
            message="No MCP configs to check",
        )

    details: list[str] = []

    # Detect drift on servers that exist in both sets
    drifted = detect_config_drift(current_configs, stored_configs)
    for name in sorted(drifted):
        details.append(f"{name}: config differs from lockfile baseline")

    # Servers in lockfile but not in manifest (orphaned MCP)
    for name in sorted(stored_configs):
        if name not in current_configs:
            details.append(f"{name}: in lockfile but not in manifest")

    # Servers in manifest but not in lockfile (new, not installed)
    for name in sorted(current_configs):
        if name not in stored_configs:
            details.append(f"{name}: in manifest but not in lockfile")

    if not details:
        return CheckResult(
            name="config-consistency",
            passed=True,
            message="MCP configs match lockfile baseline",
        )
    return CheckResult(
        name="config-consistency",
        passed=False,
        message=(f"{len(details)} MCP config inconsistenc(ies) -- run 'apm install' to reconcile"),
        details=details,
    )


def _check_includes_consent(
    manifest,
    lock,
) -> CheckResult:
    """Advisory check: nudge toward declaring 'includes:' when local content is deployed.

    This check never hard-fails -- it always returns ``passed=True``.  When
    the lockfile records local content but the manifest does not declare an
    ``includes:`` field, the result message advises the maintainer to add
    ``includes: auto`` (or an explicit list) for governance clarity.  The
    ``[+]`` rendered by the CI table is intentional: this is informational,
    not a violation.  Use ``manifest.require_explicit_includes`` policy to
    promote this to a hard block.
    """
    if not lock.local_deployed_files:
        return CheckResult(
            name="includes-consent",
            passed=True,
            message="No local content deployed -- includes consent check skipped",
        )

    if manifest.includes is None:
        return CheckResult(
            name="includes-consent",
            passed=True,
            message=(
                "Local content deployed but 'includes:' not declared in "
                "apm.yml -- consider adding 'includes: auto' for explicit consent"
            ),
        )

    return CheckResult(
        name="includes-consent",
        passed=True,
        message="'includes:' declared -- local content deployment is explicitly consented",
    )
