"""Shared constants and frozen dataclasses for the executable-trust subsystem.

Extracted from ``executables.py`` to break circular imports between sibling
helpers. No business logic here -- only declarations.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Executable-type constants
# ---------------------------------------------------------------------------

# Executable type constants used as keys in the allowExecutables block.
EXEC_TYPE_HOOKS = "hooks"
EXEC_TYPE_MCP = "mcp"
EXEC_TYPE_BIN = "bin"
EXEC_TYPE_CANVAS = "canvas"

# Types with active enforcement in the install gate.
ENFORCED_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_BIN, EXEC_TYPE_MCP, EXEC_TYPE_CANVAS)

# All recognised exec-type keys (for manifest validation).
ALL_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_MCP, EXEC_TYPE_BIN, EXEC_TYPE_CANVAS)

# ---------------------------------------------------------------------------
# Trust-state and layer constants
# ---------------------------------------------------------------------------

# trust_state values (also the lockfile exec_status field domain).
TRUST_DEPLOYED = "deployed"  # allowed and (will be) materialised
TRUST_GATED = "gated_pending_approval"  # not yet approved; approvable
TRUST_DENIED = "denied"  # an explicit deny rule forbids it
TRUST_ABSENT = "absent"  # package not present at all (audit-only)

# deciding_layer labels (which rung of the ladder decided).
LAYER_GATE_DISABLED = "gate-disabled"
LAYER_ORG_DENY_ALL = "org-deny-all"
LAYER_ORG_DENY = "org-deny"
LAYER_USER_DENY = "user-deny"
LAYER_PROJECT_DENY = "project-deny"
LAYER_ENFORCE_DEGRADED = "org-enforce-degraded"  # v2 mandate, v1 fail-safe
LAYER_PROJECT_ALLOW = "project-allow"
LAYER_USER_ALLOW = "user-allow"
LAYER_ORG_RECOMMEND = "org-recommend"
LAYER_DEFAULT_DENY = "default-deny"

# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecDecision:
    """The resolved trust decision for one (package, exec_type) pair.

    Attributes:
        allowed: Whether the executable may run / be materialised.
        deciding_layer: Which precedence rung decided (one of the
            ``LAYER_*`` constants) -- surfaced by ``apm policy explain``.
        trust_state: One of ``TRUST_*`` for the lockfile ``exec_status``.
        shadowed_layers: Lower-authority layers that held a contrary
            opinion but were overridden (for ``apm policy explain`` honesty).
    """

    allowed: bool
    deciding_layer: str
    trust_state: str
    shadowed_layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecTrustContext:
    """Resolved trust inputs across the org / project / user layers.

    The org fields are package-name sets (version-blind, mirroring the
    package-level ``bin_deploy`` semantics). ``org_bin_deny*`` carry the
    DEPRECATED ``bin_deploy`` block, honored as a ``bin``-scoped deny
    alias for one minor cycle. The project / user maps keep the granular
    ``{package_key: {exec_type: bool}}`` shape.
    """

    gate_enabled: bool
    org_deny_all: bool
    org_deny: frozenset[str]
    org_require: frozenset[str]
    org_recommend: frozenset[str]
    org_enforce: frozenset[str]
    org_bin_deny_all: bool
    org_bin_deny: frozenset[str]
    project_allow: dict[str, dict[str, bool]]
    project_deny: dict[str, dict[str, bool]]
    user_allow: dict[str, dict[str, bool]]
    user_deny: dict[str, dict[str, bool]]
