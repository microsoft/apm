"""InstallContext dataclass: parameter bundle for the APM install CLI command."""

import builtins
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from collections.abc import Callable

    from apm_cli.install.plan import UpdatePlan


@dataclasses.dataclass
class InstallContext:
    """Bundles install command state to reduce function signatures.

    Created by :func:`install` after argument parsing and scope resolution,
    then threaded through :func:`_install_apm_packages` and
    :func:`_post_install_summary` to avoid long parameter lists.
    """

    scope: Any  # InstallScope
    manifest_path: "Path"
    manifest_display: str
    apm_dir: "Path"
    project_root: "Path"
    logger: Any  # InstallLogger
    auth_resolver: Any  # AuthResolver
    verbose: bool
    force: bool
    dry_run: bool
    update: bool
    dev: bool
    runtime: str | None
    exclude: str | None
    target: str | None
    parallel_downloads: int
    allow_insecure: bool
    allow_insecure_hosts: tuple
    protocol_pref: Any  # ProtocolPreference
    allow_protocol_fallback: bool
    trust_transitive_mcp: bool
    no_policy: bool
    install_mode: Any  # InstallMode
    packages: tuple  # Original Click packages
    refresh: bool = False
    only_packages: builtins.list | None = None
    manifest_snapshot: bytes | None = None
    snapshot_manifest_path: Optional["Path"] = None
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: "Callable[[UpdatePlan], bool] | None" = None
    skill_subset: "builtins.tuple[str, ...] | None" = None
    skill_subset_from_cli: bool = False
    audit_override: str | None = None
