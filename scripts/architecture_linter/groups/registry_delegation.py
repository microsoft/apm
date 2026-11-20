"""Thin rule catalog for registry/delegation architecture checks.

Composed from cohesive check modules -- owner-registry-guarded checks
(including the scope manifest owner), the CLI startup import budget owner,
and guard-less semantic checks -- plus their shared helper module, so no
check module outgrows the module size budget.
"""

from scripts.architecture_linter.checks.registry_cli_startup import (
    COLLECTORS as _CLI_STARTUP_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_cli_startup import RULES as _CLI_STARTUP_RULES
from scripts.architecture_linter.checks.registry_owner_guards import (
    COLLECTORS as _OWNER_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_owner_guards import RULES as _OWNER_RULES
from scripts.architecture_linter.checks.registry_scope_manifest import (
    COLLECTORS as _SCOPE_MANIFEST_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_scope_manifest import (
    RULES as _SCOPE_MANIFEST_RULES,
)
from scripts.architecture_linter.checks.registry_semantic_rules import (
    COLLECTORS as _SEMANTIC_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_semantic_rules import RULES as _SEMANTIC_RULES

RULES = _OWNER_RULES + _CLI_STARTUP_RULES + _SCOPE_MANIFEST_RULES + _SEMANTIC_RULES
COLLECTORS = (
    _OWNER_COLLECTORS + _CLI_STARTUP_COLLECTORS + _SCOPE_MANIFEST_COLLECTORS + _SEMANTIC_COLLECTORS
)

__all__ = ["COLLECTORS", "RULES"]
