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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.helpers.ref_reuse import RefResolverCacheKey
    from apm_cli.security.executables import ExecTrustContext


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
    # Source root for reads (``apm.yml``, ``.apm/``, local-path
    # packages).  Equal to ``project_root`` unless ``apm install --root``
    # redirects writes -- then ``source_root`` stays at ``$PWD`` while
    # ``project_root`` is the override.
    #
    # Resolved at the CLI boundary (``run_install_pipeline``).  When a
    # caller does not pass it, ``__post_init__`` defaults it to
    # ``project_root`` -- the correct value whenever ``--root`` is absent.
    # Phases always read ``ctx.source_root`` (never re-derive from
    # ``project_root``); only the ``--root`` path makes the two diverge.
    source_root: Path | None = None
    # ------------------------------------------------------------------
    apm_package: Any = None  # APMPackage
    update_refs: bool = False
    scope: Any = None  # InstallScope (defaults to PROJECT)
    auth_resolver: Any = None  # AuthResolver
    marketplace_provenance: dict[str, Any] | None = None
    parallel_downloads: int = 4
    logger: Any = None  # InstallLogger
    target_override: str | list[str] | None = None  # effective --target value
    # Provenance label for ``target_override`` when it did NOT come from the CLI.
    # None means an explicit CLI ``--target`` selector. When the value is
    # populated from the configured default (``apm config target``), this is
    # set to "apm config target" so provenance output is not misattributed.
    target_override_source: str | None = None
    allow_insecure: bool = False
    allow_insecure_hosts: tuple[str, ...] = ()
    transaction: Any = None  # InstallTransaction

    dry_run: bool = False
    lockfile_only: bool = False
    force: bool = False
    verbose: bool = False
    refresh: bool = False
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
    update_plan_complete_dep_keys: set[str] = field(default_factory=set)  # resolve
    dependency_graph: Any = None  # resolve
    existing_lockfile: Any = None  # resolve
    lockfile_path: Path | None = None  # resolve
    apm_modules_dir: Path | None = None  # resolve
    downloader: Any = None  # resolve (GitHubPackageDownloader)
    ref_resolver: Any = None  # resolve (TieredRefResolver | None) -- #1369 fast-path
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
    registry_config: Any = None  # RegistryConfig (proxy registry; pre-existing)
    registry_resolver: Any = None  # RegistryPackageResolver -- dedicated registry resolver
    # Per-dep git-source semver resolutions (issue #1488). Keyed by
    # dep_key (DependencyReference.get_unique_key()), populated by the
    # BFS download_callback when a git-source dep has ref_kind == "semver",
    # consumed by install/sources.py to plumb the resolution into the
    # lockfile via InstalledPackage.git_semver_resolution.
    git_semver_resolutions: dict[str, Any] = field(default_factory=dict)
    # Run-scoped cache of RefResolver instances keyed by (host, token-fingerprint)
    # so semver deps sharing an upstream repo reuse one ``git ls-remote`` tag
    # listing (RefResolver memoizes per instance) instead of one per dep.
    # Populated lazily in _maybe_resolve_git_semver during the resolve phase.
    ref_resolver_cache: dict[RefResolverCacheKey, Any] = field(default_factory=dict)
    managed_files: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Integrate phase outputs (written by integrate, read by cleanup/lockfile/summary)
    # ------------------------------------------------------------------
    intended_dep_keys: set[str] = field(default_factory=set)
    package_deployed_files: dict[str, list[str]] = field(default_factory=dict)
    # Cleanup refusals retain the original lockfile hash, not a hash of
    # user-edited bytes. Lockfile assembly consumes this after cleanup.
    package_cleanup_retained: dict[str, dict[str, str | None]] = field(default_factory=dict)
    orphan_cleanup_retained: dict[str, dict[str, str | None]] = field(default_factory=dict)
    package_types: dict[str, str] = field(default_factory=dict)
    package_hashes: dict[str, str] = field(default_factory=dict)
    # Declared-license provenance (issue #1777, U6): maps dep_key -> the SPDX
    # expression the dependency's manifest DECLARED at resolve time (apm.yml
    # ``license:`` or plugin.json ``license``). Populated next to
    # ``package_types`` at acquire sites in install/sources.py and attached to
    # the lockfile by LockfileBuilder._attach_declared_licenses. Keys absent
    # from this map mean "not declared" (NOASSERTION) -- a missing entry is
    # never backfilled with a sentinel.
    package_declared_licenses: dict[str, str] = field(default_factory=dict)
    content_hash_verified_deps: set[str] = field(default_factory=set)
    # Deps whose content hash is expected to change legitimately:
    # populated by _resolve_download_strategy in phases/integrate.py
    # (branch-ref `remote_drifted` guard and v<=0.12.2 self-heal block),
    # and by the BFS callback in phases/resolve.py (spec-drift detection
    # via detect_ref_change).  Consumed by
    # FreshDependencySource.acquire() in install/sources.py:~624 to
    # suppress the supply-chain hard-block when a fresh-download
    # content_hash legitimately differs from the lockfile-recorded
    # content_hash (drift / recovery, not a supply-chain attack).
    expected_hash_change_deps: set[str] = field(default_factory=set)
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
    blocked_executables: list[Any] = field(default_factory=list)  # integrate
    # #1873 executable-trust: the resolved trust context (built once per
    # install) and the per-dependency lockfile exec_status computed at the gate.
    exec_trust_ctx: ExecTrustContext | None = None  # lazily built in template
    exec_allow_map: dict[str, dict[str, bool]] | None = None  # None means gate disabled
    package_exec_status: dict[str, str] = field(default_factory=dict)  # dep_key -> exec_status

    # ------------------------------------------------------------------
    # policy_gate
    # ------------------------------------------------------------------
    policy_fetch: Any = None  # Optional[PolicyFetchResult] from discovery
    policy_enforcement_active: bool = False
    no_policy: bool = False  # W2-escape-hatch will wire --no-policy here
    audit_override: str | None = None  # --audit/--no-audit CLI override (off|warn|block)
    skill_subset: tuple[str, ...] | None = None  # --skill filter for SKILL_BUNDLE packages
    skill_subset_from_cli: bool = False  # True when user passed --skill (even --skill '*')
    early_lockfile: Any = None  # LockFile read before pipeline phases (avoids re-read)
    direct_mcp_deps: list[Any] | None = None  # Direct MCP deps from apm.yml for policy gate
    direct_lsp_deps: list[Any] | None = None  # Direct LSP deps from apm.yml for LSP integration

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

    # ------------------------------------------------------------------
    # TUI controller (PR #1116, workstream B): one Live region for the
    # whole pipeline.  Phases call ``ctx.tui.start_phase(...)`` /
    # ``ctx.tui.task_started(...)`` / ``ctx.tui.task_completed(...)``;
    # when the controller is disabled (CI, dumb terminal,
    # ``APM_PROGRESS=never``) every method is a no-op.  Pipeline owns
    # the context-manager lifecycle (``with ctx.tui:``) so individual
    # phases never need to enter / exit it.
    # ------------------------------------------------------------------
    tui: Any = None  # InstallTui

    # ------------------------------------------------------------------
    # Legacy skill paths opt-out (convergence §3)
    # ------------------------------------------------------------------
    legacy_skill_paths: bool = False  # --legacy-skill-paths flag or APM_LEGACY_SKILL_PATHS env

    def __post_init__(self) -> None:
        # ``source_root`` defaults to ``project_root`` (the correct value
        # whenever ``apm install --root`` is not used).  Only the --root
        # CLI path passes a distinct source_root; every other caller and
        # test gets source_root == project_root for free.
        if self.source_root is None:
            self.source_root = self.project_root
