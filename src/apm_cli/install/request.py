"""Typed inputs for the install pipeline (Application Service input).

Bundles the kwargs previously passed to ``run_install_pipeline`` into a
single immutable record that the Click handler builds from CLI args and
the ``InstallService`` consumes.  This is the typed-IO companion to
``InstallResult`` (the Service output, defined in ``apm_cli.models.results``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple  # noqa: F401, UP035

if TYPE_CHECKING:
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.plan import UpdatePlan
    from apm_cli.models.apm_package import APMPackage


@dataclass(frozen=True)
class InstallRequest:
    """User intent for one install invocation.

    Frozen: never mutated by the pipeline.  Built once by the Click
    handler (or test harness) and handed to ``InstallService.run()``.
    """

    apm_package: APMPackage
    update_refs: bool = False
    verbose: bool = False
    only_packages: list[str] | None = None
    force: bool = False
    parallel_downloads: int = 4
    logger: InstallLogger | None = None
    scope: InstallScope | None = None
    auth_resolver: AuthResolver | None = None
    target: str | None = None
    allow_insecure: bool = False
    allow_insecure_hosts: tuple[str, ...] = ()
    marketplace_provenance: dict[str, Any] | None = None
    protocol_pref: Any = None  # ProtocolPreference (NONE/SSH/HTTPS) for shorthand transport
    allow_protocol_fallback: bool | None = None  # None => read APM_ALLOW_PROTOCOL_FALLBACK env
    no_policy: bool = False  # W2-escape-hatch: skip org policy enforcement
    skill_subset: tuple[str, ...] | None = None  # --skill filter for SKILL_BUNDLE packages
    skill_subset_from_cli: bool = False  # True when user passed --skill (even --skill '*')
    legacy_skill_paths: bool = False  # --legacy-skill-paths / APM_LEGACY_SKILL_PATHS
    frozen: bool = False
    plan_callback: Callable[[UpdatePlan], bool] | None = None


@dataclass(frozen=True)
class InstallApmDependenciesOptions:
    """Compatibility options for the legacy commands.install wrapper."""

    force: bool = False
    parallel_downloads: int = 4
    logger: InstallLogger | None = None
    scope: InstallScope | None = None
    auth_resolver: AuthResolver | None = None
    target: str | None = None
    allow_insecure: bool = False
    allow_insecure_hosts: tuple[str, ...] = ()
    marketplace_provenance: dict[str, Any] | None = None
    protocol_pref: Any | None = None
    allow_protocol_fallback: bool | None = None
    no_policy: bool = False
    skill_subset: tuple[str, ...] | None = None
    skill_subset_from_cli: bool = False
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: Callable[[UpdatePlan], bool] | None = None

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> InstallApmDependenciesOptions:
        known = {field.name for field in fields(cls)}
        unknown = set(kwargs) - known
        if unknown:
            unknown_list = ", ".join(sorted(unknown))
            raise TypeError(f"unexpected install option(s): {unknown_list}")
        return cls(**kwargs)
