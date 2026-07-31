"""MCP, compilation, manifest, and unmanaged-files policy checks.

Leaf module -- does NOT import ``policy_checks.py`` at module scope.
All symbols that tests import from ``apm_cli.policy.policy_checks``
are re-exported from there with the ``NAME as NAME`` redundant-alias form.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CheckResult

if TYPE_CHECKING:
    from .schema import (
        CompilationPolicy,
        ManifestPolicy,
        McpPolicy,
    )

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw manifest loader
# ---------------------------------------------------------------------------


def _load_raw_apm_yml(project_root: Path) -> dict | None:
    """Load raw apm.yml as a dict for policy checks that inspect raw fields.

    This helper is called **after** :pymethod:`APMPackage.from_apm_yml` has
    already succeeded in :func:`run_policy_checks`.  The primary security
    gate is ``from_apm_yml()`` -- if it fails, the audit aborts with a
    ``manifest-parse`` check result and this function is never reached.

    Returning ``None`` here is therefore **defence-in-depth**: it covers
    edge cases (TOCTOU race, transient I/O error) where the file becomes
    unreadable between the two calls.  Callers that receive ``None``
    gracefully skip supplementary raw-field checks (e.g.
    ``compilation-target``, ``extensions-present``) rather than hard-failing.

    Returns ``None`` when the file is absent, unreadable, malformed YAML,
    or not a mapping -- but logs a warning so the failure is visible
    rather than silently swallowed.
    """
    import yaml

    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return None
    try:
        with open(apm_yml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        # TOCTOU: file disappeared between exists() check and open(); normal condition.
        return None
    except yaml.YAMLError as exc:
        _logger.warning("Malformed YAML in %s: %s", apm_yml_path, exc)
        return None
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", apm_yml_path, exc)
        return None
    except UnicodeDecodeError as exc:
        _logger.warning("Cannot decode %s as UTF-8: %s", apm_yml_path, exc)
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "apm.yml is not a YAML mapping (got %s) -- skipping raw-field checks",
            type(data).__name__,
        )
        return None
    return data


# ---------------------------------------------------------------------------
# MCP checks (7-10)
# ---------------------------------------------------------------------------


def _check_mcp_allowlist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 7: MCP server names match allow list."""
    from .matcher import check_mcp_allowed

    if policy.allow is None:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="No MCP allow list configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="All MCP servers match allow list",
        )
    return CheckResult(
        name="mcp-allowlist",
        passed=False,
        message=f"{len(violations)} MCP server(s) not in allow list",
        details=violations,
    )


def _check_mcp_denylist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 8: no MCP server matches deny list."""
    from .matcher import check_mcp_allowed

    if not policy.deny:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP deny list configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP servers match deny list",
        )
    return CheckResult(
        name="mcp-denylist",
        passed=False,
        message=f"{len(violations)} MCP server(s) match deny list",
        details=violations,
    )


def _check_mcp_transport(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 9: MCP transport values match policy allow list."""
    allowed_transports = policy.transport.allow
    if allowed_transports is None:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="No MCP transport restrictions configured",
        )

    violations: list[str] = []
    for mcp in mcp_deps:
        if mcp.transport and mcp.transport not in allowed_transports:
            violations.append(
                f"{mcp.name}: transport '{mcp.transport}' not in allowed {allowed_transports}"
            )

    if not violations:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="All MCP transports comply with policy",
        )
    return CheckResult(
        name="mcp-transport",
        passed=False,
        message=f"{len(violations)} MCP transport violation(s)",
        details=violations,
    )


def _check_mcp_self_defined(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 10: self-defined MCP servers comply with policy."""
    self_defined_policy = policy.self_defined
    if self_defined_policy == "allow":
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="Self-defined MCP servers allowed",
        )

    self_defined = [m for m in mcp_deps if m.registry is False]
    if not self_defined:
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="No self-defined MCP servers found",
        )

    details = [f"{m.name}: self-defined server" for m in self_defined]
    if self_defined_policy == "deny":
        return CheckResult(
            name="mcp-self-defined",
            passed=False,
            message=f"{len(self_defined)} self-defined MCP server(s) denied by policy",
            details=details,
        )
    # warn -- pass but with details
    return CheckResult(
        name="mcp-self-defined",
        passed=True,
        message=f"{len(self_defined)} self-defined MCP server(s) (warn)",
        details=details,
    )


# ---------------------------------------------------------------------------
# Compilation checks (11-13)
# ---------------------------------------------------------------------------


def _check_compilation_target(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 11: compilation target matches policy."""
    enforce = policy.target.enforce
    allow = policy.target.allow

    if not enforce and allow is None:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target restrictions configured",
        )

    target = (raw_yml or {}).get("target")
    if not target:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target set in manifest",
        )

    # Normalize target to a list for uniform checking
    target_list = target if isinstance(target, list) else [target]

    if enforce:
        if enforce not in target_list:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Enforced target '{enforce}' not present in {target_list}",
                details=[f"target: {target}, enforced: {enforce}"],
            )
    elif allow is not None:
        allow_set = set(allow) if isinstance(allow, (list, tuple)) else {allow}
        disallowed = [t for t in target_list if t not in allow_set]
        if disallowed:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Target(s) {disallowed} not in allowed list {sorted(allow_set)}",
                details=[f"target: {target}, allowed: {sorted(allow_set)}"],
            )

    return CheckResult(
        name="compilation-target",
        passed=True,
        message="Compilation target compliant",
    )


def _check_compilation_strategy(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 12: compilation strategy matches policy."""
    enforce = policy.strategy.enforce
    if not enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy enforced",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    strategy = compilation.get("strategy") if isinstance(compilation, dict) else None
    if not strategy:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy set in manifest",
        )

    if strategy != enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=False,
            message=f"Strategy '{strategy}' does not match enforced '{enforce}'",
            details=[f"strategy: {strategy}, enforced: {enforce}"],
        )
    return CheckResult(
        name="compilation-strategy",
        passed=True,
        message="Compilation strategy compliant",
    )


def _check_source_attribution(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 13: source attribution enabled if policy requires."""
    if not policy.source_attribution:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution not required by policy",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    attribution = compilation.get("source_attribution") if isinstance(compilation, dict) else None
    if attribution is True:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution enabled",
        )
    return CheckResult(
        name="source-attribution",
        passed=False,
        message="Source attribution required by policy but not enabled in manifest",
        details=["Set compilation.source_attribution: true in apm.yml"],
    )


# ---------------------------------------------------------------------------
# Manifest checks (14-15 + explicit-includes)
# ---------------------------------------------------------------------------


def _check_required_manifest_fields(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 14: all required fields are present with non-empty values."""
    if not policy.required_fields:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="No required manifest fields configured",
        )

    data = raw_yml or {}
    missing: list[str] = []
    for field_name in policy.required_fields:
        value = data.get(field_name)
        if not value:  # None, empty string, missing
            missing.append(field_name)

    if not missing:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="All required manifest fields present",
        )
    return CheckResult(
        name="required-manifest-fields",
        passed=False,
        message=f"{len(missing)} required manifest field(s) missing",
        details=missing,
    )


def _check_includes_explicit(
    manifest_includes,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check: manifest declares an explicit ``includes:`` list when policy requires it.

    ``manifest_includes`` is the parsed value of the manifest's ``includes:``
    field as exposed by :class:`APMPackage` -- one of ``None`` (field
    absent), the literal string ``"auto"``, or a list of repo-relative
    path strings.

    Violation when ``policy.require_explicit_includes`` is True and
    ``manifest_includes`` is ``None`` or ``"auto"``.
    """
    if not policy.require_explicit_includes:
        return CheckResult(
            name="explicit-includes",
            passed=True,
            message="Explicit includes not required by policy",
        )

    if manifest_includes is None:
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but none are "
                "declared. Add 'includes: [<path>, ...]' to apm.yml with "
                "the paths you intend to publish."
            ),
            details=[
                "includes: <absent>, require_explicit_includes: true",
            ],
        )

    if manifest_includes == "auto":
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but manifest "
                "uses 'includes: auto'. Replace with an explicit list of "
                "paths."
            ),
            details=[
                "includes: 'auto', require_explicit_includes: true",
            ],
        )

    return CheckResult(
        name="explicit-includes",
        passed=True,
        message="Manifest declares explicit includes paths",
    )


def _check_scripts_policy(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 15: scripts section absent if policy denies it."""
    if policy.scripts != "deny":
        return CheckResult(
            name="scripts-policy",
            passed=True,
            message="Scripts allowed by policy",
        )

    scripts = (raw_yml or {}).get("scripts")
    if scripts:
        return CheckResult(
            name="scripts-policy",
            passed=False,
            message="Scripts section present but denied by policy",
            details=list(scripts.keys()) if isinstance(scripts, dict) else ["scripts"],
        )
    return CheckResult(
        name="scripts-policy",
        passed=True,
        message="No scripts section (compliant with deny policy)",
    )


# End of _policy_checks_mcp.py
