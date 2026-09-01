"""Thin rule catalog for mutation/write architecture checks.

Composed from three cohesive check-family modules -- neutral hook contract,
MCP target/scope/launcher, and hook membership/cleanup -- plus their shared
helper module, so no single check module outgrows the module size budget.
"""

from scripts.architecture_linter.checks.mutation_hook_contract import (
    COLLECTORS as _HOOK_CONTRACT_COLLECTORS,
)
from scripts.architecture_linter.checks.mutation_hook_contract import RULES as _HOOK_CONTRACT_RULES
from scripts.architecture_linter.checks.mutation_hook_membership import (
    COLLECTORS as _HOOK_MEMBERSHIP_COLLECTORS,
)
from scripts.architecture_linter.checks.mutation_hook_membership import (
    RULES as _HOOK_MEMBERSHIP_RULES,
)
from scripts.architecture_linter.checks.mutation_mcp_target_and_scope import (
    COLLECTORS as _MCP_SCOPE_COLLECTORS,
)
from scripts.architecture_linter.checks.mutation_mcp_target_and_scope import (
    RULES as _MCP_SCOPE_RULES,
)

RULES = _HOOK_CONTRACT_RULES + _MCP_SCOPE_RULES + _HOOK_MEMBERSHIP_RULES
COLLECTORS = _HOOK_CONTRACT_COLLECTORS + _MCP_SCOPE_COLLECTORS + _HOOK_MEMBERSHIP_COLLECTORS

__all__ = ["COLLECTORS", "RULES"]
