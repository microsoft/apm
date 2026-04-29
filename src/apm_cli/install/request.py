"""Typed inputs for the install pipeline (Application Service input).

Bundles the 11 kwargs previously passed to ``run_install_pipeline`` into a
single immutable record that the Click handler builds from CLI args and
the ``InstallService`` consumes.  This is the typed-IO companion to
``InstallResult`` (the Service output, defined in ``apm_cli.models.results``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.models.apm_package import APMPackage


@dataclass(frozen=True)
class InstallRequest:
    """User intent for one install invocation.

    Frozen: never mutated by the pipeline.  Built once by the Click
    handler (or test harness) and handed to ``InstallService.run()``.
    """

    apm_package: "APMPackage"
    update_refs: bool = False
    verbose: bool = False
    only_packages: Optional[List[str]] = None
    force: bool = False
    parallel_downloads: int = 4
    logger: Optional["InstallLogger"] = None
    scope: Optional["InstallScope"] = None
    auth_resolver: Optional["AuthResolver"] = None
    target: Optional[str] = None
    allow_insecure: bool = False
    allow_insecure_hosts: Tuple[str, ...] = ()
    marketplace_provenance: Optional[Dict[str, Any]] = None
    protocol_pref: Any = None  # ProtocolPreference (NONE/SSH/HTTPS) for shorthand transport
    allow_protocol_fallback: Optional[bool] = None  # None => read APM_ALLOW_PROTOCOL_FALLBACK env
    no_policy: bool = False  # W2-escape-hatch: skip org policy enforcement
    skill_subset: Optional[Tuple[str, ...]] = None  # --skill filter for SKILL_BUNDLE packages
    skill_subset_from_cli: bool = False  # True when user passed --skill (even --skill '*')
