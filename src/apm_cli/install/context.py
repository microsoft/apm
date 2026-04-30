"""Mutable state passed between install pipeline phases.

Each phase is a function ``def run(ctx: InstallContext) -> None`` that reads
the inputs already populated by earlier phases and writes its own outputs to
the context.  Keeping shared state on a single typed object turns implicit
shared lexical scope (the legacy 1444-line ``_install_apm_dependencies``)
into explicit data flow that is easy to audit and to test phase-by-phase.

Fields are added to this dataclass incrementally as phases are extracted from
the legacy entry point.  A field belongs here if and only if it is read or
written by more than one phase.  Phase-local state should stay local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401, UP035


@dataclass
class InstallContext:
    """State shared across install pipeline phases.

    Required-on-construction fields go above the ``field(default=...)``
    barrier; outputs accumulated by phases use ``field(default_factory=...)``.

    Fields are grouped by the phase that first populates them.  A trailing
    comment ``# <phase>`` marks the originating phase for auditability.
    """

    # ------------------------------------------------------------------
    # Required on construction (caller supplies before any phase runs)
    # ------------------------------------------------------------------
    project_root: Path
    apm_dir: Path

    # ------------------------------------------------------------------
    # Inputs: populated by the caller from CLI args / APMPackage
    # ------------------------------------------------------------------
    apm_package: Any = None  # APMPackage
    update_refs: bool = False
    scope: Any = None  # InstallScope (defaults to PROJECT)
    auth_resolver: Any = None  # AuthResolver
    marketplace_provenance: dict[str, Any] | None = None
    parallel_downloads: int = 4
    logger: Any = None  # InstallLogger
    target_override: str | None = None  # CLI --target value
    allow_insecure: bool = False
    allow_insecure_hosts: tuple[str, ...] = ()

    dry_run: bool = False
    force: bool = False
    verbose: bool = False
    dev: bool = False
    only_packages: list[str] | None = None
    protocol_pref: Any = None  # ProtocolPreference (NONE/SSH/HTTPS) for shorthand transport
    allow_protocol_fallback: bool | None = None  # None => read APM_ALLOW_PROTOCOL_FALLBACK env

    # ------------------------------------------------------------------
    # Resolve phase outputs
    # ------------------------------------------------------------------
    # Direct dependencies declared in apm.yml (regular + dev), NOT the
    # full transitive closure. Transitive deps are discovered later by
    # the resolver and recorded on `deps_to_install` /
    # `dependency_graph`. Treat `all_apm_deps` as "what the project
    # author wrote" -- iterate `deps_to_install` for the full set of
    # packages that will be installed.
    all_apm_deps: list[Any] = field(default_factory=list)  # resolve
    root_has_local_primitives: bool = False  # resolve
    deps_to_install: list[Any] = field(default_factory=list)  # resolve
    dependency_graph: Any = None  # resolve
    existing_lockfile: Any = None  # resolve
    lockfile_path: Path | None = None  # resolve
    apm_modules_dir: Path | None = None  # resolve
    downloader: Any = None  # resolve (GitHubPackageDownloader)
    callback_downloaded: dict[str, Any] = field(default_factory=dict)  # resolve
    callback_failures: set[str] = field(default_factory=set)  # resolve
    transitive_failures: list[tuple[str, str]] = field(default_factory=list)  # resolve

    # ------------------------------------------------------------------
    # Targets phase outputs
    # ------------------------------------------------------------------
    targets: list[Any] = field(default_factory=list)  # targets
    integrators: dict[str, Any] = field(default_factory=dict)  # targets

    # ------------------------------------------------------------------
    # Download phase outputs
    # ------------------------------------------------------------------
    pre_download_results: dict[str, Any] = field(default_factory=dict)  # download
    pre_downloaded_keys: set[str] = field(default_factory=set)  # download

    # ------------------------------------------------------------------
    # Pre-integrate inputs (populated by caller before integrate phase)
    # ------------------------------------------------------------------
    diagnostics: Any = None  # DiagnosticCollector
    registry_config: Any = None  # RegistryConfig
    managed_files: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Integrate phase outputs (written by integrate, read by cleanup/lockfile/summary)
    # ------------------------------------------------------------------
    intended_dep_keys: set[str] = field(default_factory=set)
    package_deployed_files: dict[str, list[str]] = field(default_factory=dict)
    package_types: dict[str, str] = field(default_factory=dict)
    package_hashes: dict[str, str] = field(default_factory=dict)
    installed_count: int = 0  # integrate
    unpinned_count: int = 0  # integrate
    installed_packages: list[Any] = field(default_factory=list)  # integrate
    total_prompts_integrated: int = 0  # integrate
    total_agents_integrated: int = 0  # integrate
    total_skills_integrated: int = 0  # integrate
    total_sub_skills_promoted: int = 0  # integrate
    total_instructions_integrated: int = 0  # integrate
    total_commands_integrated: int = 0  # integrate
    total_hooks_integrated: int = 0  # integrate
    total_links_resolved: int = 0  # integrate
    direct_dep_failed: bool = False  # integrate -- set when any direct dep fails

    # ------------------------------------------------------------------
    # policy_gate
    # ------------------------------------------------------------------
    policy_fetch: Any = None  # Optional[PolicyFetchResult] from discovery
    policy_enforcement_active: bool = False
    no_policy: bool = False  # W2-escape-hatch will wire --no-policy here
    skill_subset: tuple[str, ...] | None = None  # --skill filter for SKILL_BUNDLE packages
    skill_subset_from_cli: bool = False  # True when user passed --skill (even --skill '*')
    early_lockfile: Any = None  # LockFile read before pipeline phases (avoids re-read)
    direct_mcp_deps: list[Any] | None = None  # Direct MCP deps from apm.yml for policy gate

    # ------------------------------------------------------------------
    # Post-deps local content tracking (F3)
    # ------------------------------------------------------------------
    old_local_deployed: list[str] = field(default_factory=list)  # pipeline setup
    local_deployed_files: list[str] = field(default_factory=list)  # integrate (root)
    local_content_errors_before: int = 0  # integrate (pre-root)

    # ------------------------------------------------------------------
    # Cowork integration state
    # ------------------------------------------------------------------
    cowork_nonsupported_warned: bool = False  # integrate (once-per-run guard)
