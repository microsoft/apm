"""Frozen dataclasses modeling the full apm-policy.yml schema.

Every field maps 1:1 to a concrete ``apm audit`` check.

Allow-list semantics:
  * ``None``  -- "no opinion" (transparent during inheritance merge).
  * ``()``    -- "explicitly empty" (after merge: nothing is allowed).
  * ``(...)`` -- "allow only matching patterns".

Deny/require list semantics:
  * ``None``  -- "no opinion" (transparent during inheritance merge).
  * ``()``    -- "explicitly empty" (overrides parent in merge).
  * ``(...)`` -- union-merged with parent during inheritance.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PolicyCache:
    """Cache configuration for remote policy resolution."""

    ttl: int = 3600  # seconds, default 1 hour


@dataclass(frozen=True)
class DependencyPolicy:
    """Rules governing which APM dependencies are permitted."""

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] | None = None  # None = no opinion; () = explicit empty
    require: tuple[str, ...] | None = None  # None = no opinion; () = explicit empty
    require_resolution: str = "project-wins"  # project-wins | policy-wins | block
    max_depth: int = 50
    # When True, every direct APM dep must declare a bounded constraint
    # (exact version, caret/tilde range, bounded range, literal tag,
    # SHA, or local path). Unbounded refs ('*', bare '>=X', missing ref,
    # bare branch name) are reported as policy violations and routed
    # through ``policy.enforcement`` (off | warn | block). See
    # ``policy/_constraint_pinning.py`` for classification rules.
    require_pinned_constraint: bool = False

    @property
    def effective_deny(self) -> tuple[str, ...]:
        """Resolved deny list for runtime checks (None -> ())."""
        return self.deny if self.deny is not None else ()

    @property
    def effective_require(self) -> tuple[str, ...]:
        """Resolved require list for runtime checks (None -> ())."""
        return self.require if self.require is not None else ()


@dataclass(frozen=True)
class McpTransportPolicy:
    """Allowed MCP transport protocols."""

    allow: tuple[str, ...] | None = None  # stdio, sse, http, streamable-http


@dataclass(frozen=True)
class McpPolicy:
    """Rules governing MCP server references."""

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()
    transport: McpTransportPolicy = field(default_factory=McpTransportPolicy)
    self_defined: str = "warn"  # deny | warn | allow
    trust_transitive: bool = False


@dataclass(frozen=True)
class CompilationTargetPolicy:
    """Allowed compilation targets."""

    allow: tuple[str, ...] | None = None  # vscode, claude, all
    enforce: str | None = None


@dataclass(frozen=True)
class CompilationStrategyPolicy:
    """Compilation strategy constraints."""

    enforce: str | None = None  # distributed | single-file


@dataclass(frozen=True)
class CompilationPolicy:
    """Rules governing prompt compilation."""

    target: CompilationTargetPolicy = field(default_factory=CompilationTargetPolicy)
    strategy: CompilationStrategyPolicy = field(default_factory=CompilationStrategyPolicy)
    source_attribution: bool = False


@dataclass(frozen=True)
class ManifestPolicy:
    """Rules governing apm.yml manifest content."""

    required_fields: tuple[str, ...] = ()
    scripts: str = "allow"  # allow | deny
    content_types: dict | None = None  # {"allow": [...]}
    require_explicit_includes: bool = False


@dataclass(frozen=True)
class UnmanagedFilesPolicy:
    """Rules for files not tracked in apm.lock.

    ``action=None`` and ``directories=None`` together mean the policy file
    expressed no ``unmanaged_files:`` section (or an empty mapping); during
    :func:`~apm_cli.policy.inheritance.merge_policies` the child is transparent
    and the parent block is inherited unchanged.

    When either field is set (including ``directories=()`` with a declared
    ``directories`` key), the merge applies escalation / union rules.
    ``action`` is then one of ``ignore`` | ``warn`` | ``deny``.
    """

    action: str | None = None  # None | ignore | warn | deny
    directories: tuple[str, ...] | None = None  # None -> no opinion; () explicit

    @property
    def effective_action(self) -> str:
        """Resolved action for runtime checks (None -> 'ignore')."""
        return self.action if self.action is not None else "ignore"


@dataclass(frozen=True)
class RegistrySourcePolicy:
    """Rules governing which registries APM dependencies may use.

    ``require``: registry names that MUST be the source for all deps.
    ``allow_non_registry``: when ``False``, any dep that is not
    registry-sourced (git, local, etc.) is blocked. Applied transitively
    across the full resolved dep graph.
    """

    require: tuple[str, ...] = ()
    allow_non_registry: bool = True


@dataclass(frozen=True)
class ScannerGovernance:
    """Per-scanner governance applied at install-time audit (org floor).

    Restrict-only by design: policy may tighten what a user/CLI can do, but it
    never injects argv tokens of its own.

      * ``allow_args`` -- ``False`` forbids *all* extra-args passthrough for the
        scanner (a governance kill-switch: user/CLI ``--external-args`` and the
        ``external.<name>.args`` config are stripped to ``()``). ``None``/``True``
        permit the (allowlist-validated) passthrough.

    ``llm`` mandation is intentionally NOT modelled in v1: forcing outbound LLM
    egress from a project-shipped ``apm-policy.yml`` would be a trust-domain
    change, so orgs forbid LLM by not enabling it / denylisting the scanner.
    """

    allow_args: bool | None = None


@dataclass(frozen=True)
class AuditPolicy:
    """Rules governing the ``apm audit`` content scan, including at install time.

    ``on_install`` semantics (mirrors the ``None = no opinion`` convention so
    inheritance merge stays transparent):
      * ``None``    -- no opinion (inherit parent / fall through to config).
      * ``"off"``   -- never run audit during ``apm install``.
      * ``"warn"``  -- run audit at install, surface findings, never block.
      * ``"block"`` -- run audit at install, fail the install on critical findings.

    ``external`` lists external SARIF scanner names (see
    ``security/external/registry.SUPPORTED_SCANNERS``) that MUST run as part of
    the install-time audit.  ``None`` = no opinion; ``()`` = explicitly none.
    Requires the ``external_scanners`` experimental flag to take effect.

    ``scanners`` carries optional per-scanner governance (see
    :class:`ScannerGovernance`) as a tuple of ``(name, governance)`` pairs to
    stay frozen/hashable, consistent with the other tuple-typed policy fields.
    ``None`` = no opinion. Enforced only at the install-time audit phase and
    only while the ``external_scanners`` flag is enabled.
    """

    on_install: str | None = None  # None | off | warn | block
    external: tuple[str, ...] | None = None  # required external scanners at install
    scanners: tuple[tuple[str, ScannerGovernance], ...] | None = None
    # When True, ``apm audit`` exits non-zero if the workspace content drifts
    # from the lockfile. Default off preserves today's advisory behavior (bare
    # ``apm audit`` renders drift but exits 0). Tighten-not-relax on merge.
    fail_on_drift: bool = False


@dataclass(frozen=True)
class IntegrityPolicy:
    """Rules governing supply-chain integrity of locked dependencies.

    ``require_hashes``: when ``True``, every non-local lockfile entry MUST carry
    a content hash. A missing or empty hash is a FAILURE (fail-closed), never a
    silent pass -- an unhashed entry could let a tampered lockfile redirect a
    download without detection. Default off preserves today's behavior. The key
    only asserts hash-presence on the lockfile entry; it does not add a second
    filesystem hash pass (the install pipeline already computes+records hashes).
    """

    require_hashes: bool = False


@dataclass(frozen=True)
class SecurityPolicy:
    """Rules governing APM's security checks (content audit and scanners)."""

    audit: AuditPolicy = field(default_factory=AuditPolicy)
    integrity: IntegrityPolicy = field(default_factory=IntegrityPolicy)


@dataclass(frozen=True)
class BinDeployPolicy:
    """Policy controls for marketplace_plugin bin/ deployment.

    ``deny_all``: when ``True``, bin/ deployment is suppressed for all
    marketplace_plugin packages regardless of the ``deny`` list.

    ``deny``: package canonical dependency strings (e.g. ``owner/repo``)
    whose bin/ executables must NOT be deployed. Matched as exact strings.
    """

    deny_all: bool = False
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApmPolicy:
    """Top-level APM policy model."""

    name: str = ""
    version: str = ""
    extends: str | None = None  # "org", "<owner>/<repo>", or URL
    enforcement: str = "warn"  # warn | block | off
    fetch_failure: str = "warn"  # warn | block (closes #829)
    cache: PolicyCache = field(default_factory=PolicyCache)
    dependencies: DependencyPolicy = field(default_factory=DependencyPolicy)
    mcp: McpPolicy = field(default_factory=McpPolicy)
    compilation: CompilationPolicy = field(default_factory=CompilationPolicy)
    manifest: ManifestPolicy = field(default_factory=ManifestPolicy)
    unmanaged_files: UnmanagedFilesPolicy = field(default_factory=UnmanagedFilesPolicy)
    registry_source: RegistrySourcePolicy = field(default_factory=RegistrySourcePolicy)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    bin_deploy: BinDeployPolicy = field(default_factory=BinDeployPolicy)
