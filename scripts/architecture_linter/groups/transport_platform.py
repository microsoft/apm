"""Thin rule catalog for transport/platform architecture checks."""

from scripts.architecture_linter.checks.transport_auth_outcome import (
    COLLECTORS as _AUTH_OUTCOME_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_auth_outcome import (
    RULES as _AUTH_OUTCOME_RULES,
)
from scripts.architecture_linter.checks.transport_auth_platform import (
    COLLECTORS as _AUTH_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_auth_platform import RULES as _AUTH_RULES
from scripts.architecture_linter.checks.transport_authenticated_egress import (
    RULES as _EGRESS_RULES,
)
from scripts.architecture_linter.checks.transport_cache_cleanup import RULES as _CLEANUP_RULES
from scripts.architecture_linter.checks.transport_cache_identity import (
    COLLECTORS as _CACHE_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_cache_identity import RULES as _CACHE_RULES
from scripts.architecture_linter.checks.transport_gitlab_sparse import (
    RULES as _GITLAB_SPARSE_RULES,
)
from scripts.architecture_linter.checks.transport_ls_remote_tags import (
    COLLECTORS as _LS_REMOTE_TAG_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_ls_remote_tags import (
    RULES as _LS_REMOTE_TAG_RULES,
)
from scripts.architecture_linter.checks.transport_network_and_runtime import (
    COLLECTORS as _NETWORK_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_network_and_runtime import (
    RULES as _NETWORK_RULES,
)
from scripts.architecture_linter.checks.transport_origin_bounded_redirect import (
    RULES as _REDIRECT_RULES,
)
from scripts.architecture_linter.checks.transport_revision_pins import (
    COLLECTORS as _REVISION_PIN_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_revision_pins import (
    RULES as _REVISION_PIN_RULES,
)
from scripts.architecture_linter.checks.transport_sparse_and_updates import (
    COLLECTORS as _SPARSE_COLLECTORS,
)
from scripts.architecture_linter.checks.transport_sparse_and_updates import (
    RULES as _SPARSE_RULES,
)

RULES = (
    _AUTH_RULES
    + _AUTH_OUTCOME_RULES
    + _EGRESS_RULES
    + _CACHE_RULES
    + _CLEANUP_RULES
    + _SPARSE_RULES
    + _GITLAB_SPARSE_RULES
    + _REVISION_PIN_RULES
    + _LS_REMOTE_TAG_RULES
    + _NETWORK_RULES
    + _REDIRECT_RULES
)
COLLECTORS = (
    _AUTH_COLLECTORS
    + _AUTH_OUTCOME_COLLECTORS
    + _CACHE_COLLECTORS
    + _SPARSE_COLLECTORS
    + _REVISION_PIN_COLLECTORS
    + _LS_REMOTE_TAG_COLLECTORS
    + _NETWORK_COLLECTORS
)

__all__ = ["COLLECTORS", "RULES"]
