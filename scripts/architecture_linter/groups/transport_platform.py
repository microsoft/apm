"""Thin rule catalog for transport/platform architecture checks.

Composed from four cohesive check-family modules -- auth/URL-path/Windows,
repository cache identity, sparse-checkout/self-update, and
network/ref-resolution/runtime-safety -- plus their shared helper module, so
no single check module outgrows the module size budget.
"""

from scripts.architecture_linter.checks.transport_auth_platform import (
    COLLECTORS as _AUTH_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_auth_platform import RULES as _AUTH_RULES
from scripts.architecture_linter.checks.transport_cache_identity import (
    COLLECTORS as _CACHE_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_cache_identity import RULES as _CACHE_RULES
from scripts.architecture_linter.checks.transport_network_and_runtime import (
    COLLECTORS as _NETWORK_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_network_and_runtime import (
    RULES as _NETWORK_RULES,
)
from scripts.architecture_linter.checks.transport_sparse_and_updates import (
    COLLECTORS as _SPARSE_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_sparse_and_updates import (
    RULES as _SPARSE_RULES,
)

RULES = _AUTH_RULES + _CACHE_RULES + _SPARSE_RULES + _NETWORK_RULES
COLLECTORS = _AUTH_COLLECTORS + _CACHE_COLLECTORS + _SPARSE_COLLECTORS + _NETWORK_COLLECTORS

__all__ = ["COLLECTORS", "RULES"]
