"""Parameter bundle the ``apm install`` command threads through its helpers.

Moved from ``commands/install.py`` (#3879) so install modules can type
against it without importing the commands layer;
``apm_cli.commands.install.InstallContext`` re-exports it.
"""

import builtins
import dataclasses
from typing import TYPE_CHECKING, Any

from apm_cli.models.results import InstallResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from apm_cli.core.target_detection import EffectiveTargetDecision
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
    transaction: Any = None  # InstallTransaction; default preserves direct-call compatibility
    refresh: bool = False
    only_packages: builtins.list | None = None
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: "Callable[[UpdatePlan], bool] | None" = None
    skill_subset: "builtins.tuple[str, ...] | None" = None
    skill_subset_from_cli: bool = False
    audit_override: str | None = None
    install_result: InstallResult | None = None
    target_decision: "EffectiveTargetDecision | None" = None
    trust_bin: bool | None = None
    exec_allow_map: builtins.dict[str, builtins.dict[str, bool]] | None = None
    exec_allow_resolved: bool = False
    # The package pipeline's commit gate already enforced merged MCP policy.
    mcp_policy_enforced: bool = False
    create_config: bool = True
