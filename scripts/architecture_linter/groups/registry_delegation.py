"""Thin rule catalog for registry/delegation architecture checks.

Composed from cohesive check modules -- owner-registry-guarded checks,
guard-less semantic checks, and the manifest ``target:`` deprecation
guard -- plus their shared helper module, so
neither check module outgrows the module size budget.
"""

from scripts.architecture_linter.checks.registry_manifest_target_deprecation import (
    COLLECTORS as _MANIFEST_TARGET_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_manifest_target_deprecation import (
    RULES as _MANIFEST_TARGET_RULES,
)
from scripts.architecture_linter.checks.registry_owner_guards import (
    COLLECTORS as _OWNER_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_owner_guards import RULES as _OWNER_RULES
from scripts.architecture_linter.checks.registry_semantic_rules import (
    COLLECTORS as _SEMANTIC_COLLECTORS,
)
from scripts.architecture_linter.checks.registry_semantic_rules import RULES as _SEMANTIC_RULES

RULES = _OWNER_RULES + _SEMANTIC_RULES + _MANIFEST_TARGET_RULES
COLLECTORS = _OWNER_COLLECTORS + _SEMANTIC_COLLECTORS + _MANIFEST_TARGET_COLLECTORS

__all__ = ["COLLECTORS", "RULES"]
