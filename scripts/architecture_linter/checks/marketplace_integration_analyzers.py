"""Thin rule catalog for marketplace/integration architecture checks.

Owns no check-function bodies of its own: composes the five cohesive
check-family modules (tag/version, catalog/contract, component/admission,
package/registration, bundle/audit) in their original registration order.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.marketplace_bundle_and_audit import (
    COLLECTORS as _BUNDLE_AUDIT_COLLECTORS,
)
from scripts.architecture_linter.checks.marketplace_bundle_and_audit import (
    RULES as _BUNDLE_AUDIT_RULES,
)
from scripts.architecture_linter.checks.marketplace_catalog_and_contract import (
    COLLECTORS as _CATALOG_CONTRACT_COLLECTORS,
)
from scripts.architecture_linter.checks.marketplace_catalog_and_contract import (
    RULES as _CATALOG_CONTRACT_RULES,
)
from scripts.architecture_linter.checks.marketplace_component_and_admission import (
    COLLECTORS as _COMPONENT_ADMISSION_COLLECTORS,
)
from scripts.architecture_linter.checks.marketplace_component_and_admission import (
    RULES as _COMPONENT_ADMISSION_RULES,
)
from scripts.architecture_linter.checks.marketplace_package_and_registration import (
    COLLECTORS as _PACKAGE_REGISTRATION_COLLECTORS,
)
from scripts.architecture_linter.checks.marketplace_package_and_registration import (
    RULES as _PACKAGE_REGISTRATION_RULES,
)
from scripts.architecture_linter.checks.marketplace_tag_and_version import (
    COLLECTORS as _TAG_VERSION_COLLECTORS,
)
from scripts.architecture_linter.checks.marketplace_tag_and_version import (
    RULES as _TAG_VERSION_RULES,
)

RULES = (
    _TAG_VERSION_RULES
    + _CATALOG_CONTRACT_RULES
    + _COMPONENT_ADMISSION_RULES
    + _PACKAGE_REGISTRATION_RULES
    + _BUNDLE_AUDIT_RULES
)
COLLECTORS = (
    _TAG_VERSION_COLLECTORS
    + _CATALOG_CONTRACT_COLLECTORS
    + _COMPONENT_ADMISSION_COLLECTORS
    + _PACKAGE_REGISTRATION_COLLECTORS
    + _BUNDLE_AUDIT_COLLECTORS
)

__all__ = ["COLLECTORS", "RULES"]
