"""Thin rule catalog for contract/test architecture checks.

All eight rules -- five owner guards, the executable-contract-authorities
and lifecycle-partition structural rules, and the legacy-shell splice --
are declared together in :mod:`contracts_test_taxonomy` so their relative
order stays exactly as registered. The heavy structural-authority helper
trees live in sibling modules purely so no single check module outgrows the
module size budget; they own no RULES of their own.
"""

from scripts.architecture_linter.checks.contracts_dependency_policy import RULES as POLICY_RULES
from scripts.architecture_linter.checks.contracts_test_taxonomy import COLLECTORS
from scripts.architecture_linter.checks.contracts_test_taxonomy import RULES as EXISTING_RULES

RULES = (*EXISTING_RULES, *POLICY_RULES)

__all__ = ["COLLECTORS", "RULES"]
