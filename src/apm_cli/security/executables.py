"""Executable primitive approval gate (npm v12-inspired opt-in model).

APM packages can declare five kinds of executable primitives -- hooks,
MCP servers, LSP servers, bin/ executables, and canvas extensions -- that run arbitrary
code on the developer's machine.  When the consuming project declares an
``allowExecutables`` block in its ``apm.yml``, this module enforces a
deny-by-default policy: none of these primitives are deployed unless
explicitly approved.  Projects that omit the block entirely get
backward-compatible behaviour (all executables deployed).

The design mirrors npm v12's ``allowScripts`` (shipping July 2026):
version-pinned per-package approval, interactive prompts at install
time, and hard errors in non-interactive (CI) environments.

See also: ``apm approve`` / ``apm deny`` CLI commands.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import sys
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from apm_cli.core.scope import manifest_file

from ..core.remediation import (
    EXECUTABLES_IDENTITY_UNLOCKED,
    EXECUTABLES_PIN_MALFORMED,
    MCP_EXECUTABLES_UNTRUSTED,
)

# Executable type constants used as keys in the allowExecutables block.
EXEC_TYPE_HOOKS = "hooks"
EXEC_TYPE_MCP = "mcp"
EXEC_TYPE_BIN = "bin"
EXEC_TYPE_CANVAS = "canvas"
EXEC_TYPE_LSP = "lsp"

# Types with active enforcement in the install gate.
ENFORCED_EXEC_TYPES = (
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_BIN,
    EXEC_TYPE_MCP,
    EXEC_TYPE_CANVAS,
    EXEC_TYPE_LSP,
)

# All recognised exec-type keys (for manifest validation).
ALL_EXEC_TYPES = (
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    EXEC_TYPE_BIN,
    EXEC_TYPE_CANVAS,
    EXEC_TYPE_LSP,
)


@dataclass(frozen=True)
class ExecutableDeclaration:
    """Describes the executable primitives declared by a single package.

    Attributes:
        package_key: Approval key for this package (e.g. ``owner/repo#v1.0``
            or ``name@marketplace#1.2.0``).
        package_name: Human-readable package name.
        is_transitive: Whether this package is a transitive dependency.
        parent_name: Name of the direct dependency that pulled this in
            (only set when *is_transitive* is True).
        original_identity: Locked identity a ``replace:`` rule redirected,
            so commands can find the package by its declared name while the
            grant names the selected source.
        hook_count: Number of hook files discovered.
        mcp_count: Number of MCP server entries discovered.
        lsp_count: Number of LSP server entries discovered.
        bin_count: Number of bin/ executables discovered.
        canvas_count: Number of canvas extensions discovered.
        hook_details: Per-hook summaries for ``inspect`` display.
        mcp_details: Per-MCP-server summaries.
        lsp_details: Per-LSP-server summaries.
        bin_details: Per-binary summaries.
        canvas_details: Per-canvas summaries.
    """

    package_key: str
    package_name: str
    is_transitive: bool = False
    parent_name: str | None = None
    original_identity: str | None = None
    hook_count: int = 0
    mcp_count: int = 0
    lsp_count: int = 0
    bin_count: int = 0
    canvas_count: int = 0
    hook_details: list[str] = field(default_factory=list)
    mcp_details: list[str] = field(default_factory=list)
    lsp_details: list[str] = field(default_factory=list)
    bin_details: list[str] = field(default_factory=list)
    canvas_details: list[str] = field(default_factory=list)
    # Where the package is installed and whether its bytes still match the
    # lock; ``apm approve`` pins only verified content (#3902).
    install_path: str | None = None
    depth: int = 1
    content_verified: bool = True
    # The lockfile row the scanner read this package from (its unique key),
    # so org dependency policy judges that row (#5407).
    lock_key: str | None = None
    # A natively loaded plugin is approved as one item over its whole root.
    native_plugin: bool = False
    # The native gate's type set (trust owner), which the counts above miss
    # for native layouts such as an Agent Plugin's root mcp.json.
    native_exec_types: tuple[str, ...] = ()

    @property
    def has_executables(self) -> bool:
        """Return True if this package declares enforced executable primitives."""
        return bool(
            self.hook_count > 0
            or self.bin_count > 0
            or self.mcp_count > 0
            or self.canvas_count > 0
            or self.lsp_count > 0
            or self.native_exec_types
        )

    @property
    def exec_types(self) -> list[str]:
        """Return the list of enforced executable types this package declares."""
        types: list[str] = []
        if self.hook_count > 0:
            types.append(EXEC_TYPE_HOOKS)
        if self.mcp_count > 0:
            types.append(EXEC_TYPE_MCP)
        if self.bin_count > 0:
            types.append(EXEC_TYPE_BIN)
        if self.canvas_count > 0:
            types.append(EXEC_TYPE_CANVAS)
        if self.lsp_count > 0:
            types.append(EXEC_TYPE_LSP)
        types.extend(t for t in self.native_exec_types if t not in types)
        return types

    def summary_line(self) -> str:
        """One-line summary for the interactive prompt (enforced types only)."""
        parts: list[str] = []
        if self.hook_count:
            parts.append(f"{self.hook_count} hook(s)")
        if self.mcp_count:
            parts.append(f"{self.mcp_count} MCP server(s)")
        if self.bin_count:
            parts.append(f"{self.bin_count} bin executable(s)")
        if self.canvas_count:
            parts.append(f"{self.canvas_count} canvas extension(s)")
        if self.lsp_count:
            parts.append(f"{self.lsp_count} LSP server(s)")
        native_only = self._native_only_types()
        if native_only:
            parts.append(f"native plugin ({', '.join(native_only)})")
        return ", ".join(parts)

    def _native_only_types(self) -> list[str]:
        """Native gate types no count above reports."""
        counted = {
            EXEC_TYPE_HOOKS: self.hook_count,
            EXEC_TYPE_MCP: self.mcp_count,
            EXEC_TYPE_BIN: self.bin_count,
            EXEC_TYPE_CANVAS: self.canvas_count,
            EXEC_TYPE_LSP: self.lsp_count,
        }
        return [t for t in self.native_exec_types if not counted.get(t)]


# -------------------------------------------------------------------
# Approval checking
# -------------------------------------------------------------------


def is_package_approved(
    allow_executables: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Check whether *package_key* is approved for *exec_type*.

    Args:
        allow_executables: The parsed ``allowExecutables`` block from the
            consuming project's ``apm.yml``.  ``None`` means no block
            exists (nothing approved).
        package_key: The approval key (e.g. ``owner/repo#v1.0``).
        exec_type: One of ``hooks``, ``mcp``, ``lsp``, ``bin``, ``canvas``.

    Returns:
        ``True`` only when the block contains a matching entry with
        ``{exec_type}: true``.
    """
    if not allow_executables:
        return False
    entry = allow_executables.get(package_key)
    if not entry or not isinstance(entry, dict):
        return False
    # Only a literal ``true`` is a type-level grant; a pinned item map never is.
    return entry.get(exec_type, False) is True


def is_any_type_approved(
    allow_executables: dict[str, dict[str, bool]] | None,
    package_key: str,
) -> bool:
    """Return True if *package_key* is approved for at least one exec type."""
    if not allow_executables:
        return False
    entry = allow_executables.get(package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return any(entry.get(t, False) is True for t in ALL_EXEC_TYPES)


# -------------------------------------------------------------------
# Unified executable-trust resolver (issue #1873)
# -------------------------------------------------------------------
#
# One deny-wins, first-match-wins precedence ladder, shared by the
# install gate AND the policy audit so the two never guess independently.

# trust_state values (also the lockfile exec_status field domain).
TRUST_DEPLOYED = "deployed"  # allowed and (will be) materialised
TRUST_GATED = "gated_pending_approval"  # not yet approved; approvable
TRUST_DENIED = "denied"  # an explicit deny rule forbids it
TRUST_ABSENT = "absent"  # package not present at all (audit-only)

# deciding_layer labels (which rung of the ladder decided).
LAYER_GATE_DISABLED = "gate-disabled"
LAYER_ORG_DENY_ALL = "org-deny-all"
LAYER_ORG_DENY = "org-deny"
LAYER_USER_DENY = "user-deny"
LAYER_PROJECT_DENY = "project-deny"
LAYER_ENFORCE_DEGRADED = "org-enforce-degraded"  # v2 mandate, v1 fail-safe
LAYER_PROJECT_ALLOW = "project-allow"
LAYER_USER_ALLOW = "user-allow"
LAYER_ORG_RECOMMEND = "org-recommend"
LAYER_DEFAULT_DENY = "default-deny"


# Deny-wins severity ladder for the lockfile ``exec_status`` field, shared by
# every writer so no path can silently downgrade a worse-case verdict.
_EXEC_STATUS_SEVERITY = {
    None: 0,
    TRUST_ABSENT: 0,
    TRUST_DEPLOYED: 1,
    TRUST_GATED: 2,
    TRUST_DENIED: 3,
}


def more_severe_exec_status(current: str | None, candidate: str | None) -> str | None:
    """Return the MORE severe of two lockfile ``exec_status`` values.

    Severity follows the canonical deny-wins ladder: ``denied`` >
    ``gated_pending_approval`` > ``deployed`` > ``absent``/``None``. Ties keep
    *current* (stable). An unknown value ranks above every known one so a
    surprise status never silently downgrades trust. Owning the ordering here
    keeps every writer -- the exec gate and native plugin admission -- folding
    against one ladder instead of guessing independently.
    """
    ceiling = max(_EXEC_STATUS_SEVERITY.values()) + 1

    def _rank(value: str | None) -> int:
        return _EXEC_STATUS_SEVERITY.get(value, ceiling)

    return candidate if _rank(candidate) > _rank(current) else current


# The ONLY ``exec_status`` values that may reach native Copilot registration.
# This is an ALLOWLIST on purpose. The severity ladder above ranks an unknown
# status ABOVE ``denied`` (fail closed), so a denylist of
# ``(TRUST_DENIED, TRUST_GATED)`` would disagree with it and let a status the
# ladder calls worse-than-denied through the registration gate on forward
# version skew -- e.g. a future APM writing ``quarantined`` that an older CLI
# reads back. Both registration gates read this one definition.
REGISTRABLE_EXEC_STATUSES: frozenset[str | None] = frozenset({None, TRUST_ABSENT, TRUST_DEPLOYED})


@dataclass(frozen=True)
class ExecDecision:
    """The resolved trust decision for one (package, exec_type) pair.

    Attributes:
        allowed: Whether the executable may run / be materialised.
        deciding_layer: Which precedence rung decided (one of the
            ``LAYER_*`` constants) -- surfaced by ``apm policy explain``.
        trust_state: One of ``TRUST_*`` for the lockfile ``exec_status``.
        shadowed_layers: Lower-authority layers that held a contrary
            opinion but were overridden (for ``apm policy explain`` honesty).
    """

    allowed: bool
    deciding_layer: str
    trust_state: str
    shadowed_layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecTrustContext:
    """Resolved trust inputs across the org / project / user layers.

    The org fields are package-name sets (version-blind, mirroring the
    package-level ``bin_deploy`` semantics). ``org_bin_deny*`` carry the
    DEPRECATED ``bin_deploy`` block, honored as a ``bin``-scoped deny
    alias for one minor cycle. The project / user maps keep the granular
    ``{package_key: {exec_type: bool}}`` shape.
    """

    gate_enabled: bool
    org_deny_all: bool
    org_deny: frozenset[str]
    org_require: frozenset[str]
    org_recommend: frozenset[str]
    org_enforce: frozenset[str]
    org_bin_deny_all: bool
    org_bin_deny: frozenset[str]
    project_allow: dict[str, dict[str, bool]]
    project_deny: dict[str, dict[str, bool]]
    user_allow: dict[str, dict[str, bool]]
    user_deny: dict[str, dict[str, bool]]
    # Content-pinned item grants ``{package: {exec_type: {item: fingerprint}}}``
    # (#3902). Kept apart from the bool maps so no type-level lookup can read a
    # pinned grant as "any content"; only security/trust.py consumes them.
    project_items: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    user_items: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)


def _strip_version(package_key: str) -> str:
    """Return the version-blind canonical name from an approval key."""
    return package_key.split("#", 1)[0]


def _is_content_bound_key(package_key: str) -> bool:
    """Return whether an approval key identifies one exact content digest."""
    return "@sha256:" in package_key


# Placeholder tokens let glob deny patterns pass through DependencyReference
# parsing; they are plain lowercase words that never occur in real identities.
_GLOB_PLACEHOLDERS = (("*", "apmglobstar"), ("?", "apmglobqmark"))


def canonical_exec_identity(package_key: object) -> str:
    """Return the version-blind package identity used for deny matching.

    Shorthand, host-qualified, and URL spellings of one package map to its
    lockfile identity; GitHub-family and Azure DevOps identities are
    case-folded because those hosts resolve owner/repo case-insensitively. The
    port is dropped (``build_port_blind_dependency_key``), so a deny cannot be
    bypassed by naming another port. Glob wildcards survive normalization;
    content-bound keys and spellings that do not parse are returned unchanged.
    """
    name = _strip_version(str(package_key or "").strip())
    if not name or _is_content_bound_key(name):
        return name
    probe = name
    for char, token in _GLOB_PLACEHOLDERS:
        probe = probe.replace(char, token)
    try:
        from apm_cli.models.apm_package import DependencyReference
        from apm_cli.models.dependency.identity import build_port_blind_dependency_key
        from apm_cli.utils.github_host import is_azure_devops_hostname, is_github_hostname

        reference = DependencyReference.parse(probe)
        canonical = build_port_blind_dependency_key(reference)
    except ValueError:
        return name
    if is_github_hostname(reference.host) or is_azure_devops_hostname(reference.host):
        # GitHub-family and Azure DevOps hosts resolve owner/repo case-insensitively.
        canonical = canonical.lower()
    for char, token in _GLOB_PLACEHOLDERS:
        canonical = canonical.replace(token, char)
    return canonical


def normalize_bin_deploy_deny_key(value: object) -> str:
    """Normalize package identity for ``bin_deploy.deny`` storage and lookup."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from apm_cli.models.apm_package import DependencyReference

        raw = DependencyReference.parse(raw).get_canonical_dependency_string()
    except ValueError:
        raw = raw.removesuffix(".git")
    return raw.rstrip("/").lower()


def _map_grants(
    grant_map: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Return True if *grant_map* grants *exec_type* for *package_key*.

    Ordinary package grants are version-blind. Content-bound local-bundle
    grants match only the exact digest key.
    """
    if not grant_map:
        return False
    name = _strip_version(package_key)
    for stored_key, entry in grant_map.items():
        if not isinstance(entry, dict):
            continue
        content_bound = _is_content_bound_key(package_key) or _is_content_bound_key(stored_key)
        matches = (
            stored_key == package_key
            if content_bound
            else stored_key in (package_key, name) or _strip_version(stored_key) == name
        )
        if matches and entry.get(exec_type, False) is True:
            return True
    return False


def _map_denies(
    deny_map: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Return True if *deny_map* denies *exec_type* for any spelling of the package."""
    if _map_grants(deny_map, package_key, exec_type):
        return True
    if not deny_map or _is_content_bound_key(package_key):
        return False
    identity = canonical_exec_identity(package_key)
    return any(
        isinstance(entry, dict)
        and entry.get(exec_type, False) is True
        and not _is_content_bound_key(stored_key)
        and canonical_exec_identity(stored_key) == identity
        for stored_key, entry in deny_map.items()
    )


def _deny_glob_match(name: str, patterns: Any) -> bool:
    """Return True if *name* matches any DENY *pattern* (exact, glob, or parent).

    A pattern also covers sub-packages below it (``owner/repo`` covers
    ``owner/repo/skills/x``). Deny is the org ceiling, so in v1 it supports ``fnmatch`` globs such as
    ``evil/*`` to block a whole publisher fleet-wide. Allow / recommend /
    require remain exact-match only -- widening the GRANT side with a glob is
    a larger blast radius (a typo over-trusts), whereas broad denial is
    safety-positive (#1873).
    """
    if name in patterns:
        return True
    return any(
        fnmatch.fnmatchcase(name, p) or name.startswith(f"{str(p).rstrip('/')}/")
        for p in patterns
        if str(p).rstrip("/")
    )


def _org_denies(ctx: ExecTrustContext, name: str, exec_type: str) -> tuple[bool, str | None]:
    """Return ``(denied, layer)`` for the org DENY ceiling (rule 1)."""
    if ctx.org_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if exec_type == EXEC_TYPE_BIN and ctx.org_bin_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if _deny_glob_match(name, ctx.org_deny) or _deny_glob_match(
        canonical_exec_identity(name), ctx.org_deny
    ):
        return True, LAYER_ORG_DENY
    if exec_type == EXEC_TYPE_BIN:
        normalized_name = normalize_bin_deploy_deny_key(name)
        if _deny_glob_match(normalized_name, ctx.org_bin_deny):
            return True, LAYER_ORG_DENY
    return False, None


def resolve_exec_decision(
    ctx: ExecTrustContext,
    package_key: str,
    exec_type: str,
) -> ExecDecision:
    """Resolve the trust decision for one (package, exec_type) pair.

    Implements the #1873 deny-wins, first-match-wins ladder:

      1. ORG deny_all / deny           -> DENIED (absolute ceiling)
      2. USER deny                     -> DENIED (narrowing)
         PROJECT deny                  -> DENIED (committed narrowing)
      3/4. ORG enforce                 -> v1 fail-safe degrade to recommend
      5. PROJECT allow                 -> ALLOWED
      6. USER allow                    -> ALLOWED
      7. ORG recommend                 -> ALLOWED (user-overridable)
      8. (no match)                    -> DENIED, secure-by-default (approvable)

    v1 NEVER force-executes: ``enforce`` carries no provenance check and
    degrades to ``recommend`` so it stays overridable by a USER deny.
    """
    if not ctx.gate_enabled:
        return ExecDecision(True, LAYER_GATE_DISABLED, TRUST_DEPLOYED)

    name = _strip_version(package_key)

    # 1. ORG deny ceiling (absolute).
    denied, layer = _org_denies(ctx, name, exec_type)
    if denied:
        return ExecDecision(False, layer, TRUST_DENIED, _shadowed_grants(ctx, name, exec_type))

    # 2. USER deny / PROJECT deny (narrowing; both win over any grant).
    if _map_denies(ctx.user_deny, package_key, exec_type):
        return ExecDecision(
            False, LAYER_USER_DENY, TRUST_DENIED, _shadowed_grants(ctx, name, exec_type)
        )
    if _map_denies(ctx.project_deny, package_key, exec_type):
        return ExecDecision(
            False, LAYER_PROJECT_DENY, TRUST_DENIED, _shadowed_grants(ctx, name, exec_type)
        )

    enforce_active = name in ctx.org_enforce

    # 5. PROJECT allow (overridable only by an upstream deny, handled above).
    if _map_grants(ctx.project_allow, package_key, exec_type):
        return ExecDecision(True, LAYER_PROJECT_ALLOW, TRUST_DEPLOYED)

    # 6. USER allow.
    if _map_grants(ctx.user_allow, package_key, exec_type):
        return ExecDecision(True, LAYER_USER_ALLOW, TRUST_DEPLOYED)

    # 7. ORG recommend (or degraded enforce). Both default-allow, overridable.
    if name in ctx.org_recommend or enforce_active:
        layer = (
            LAYER_ENFORCE_DEGRADED
            if enforce_active and name not in ctx.org_recommend
            else LAYER_ORG_RECOMMEND
        )
        return ExecDecision(True, layer, TRUST_DEPLOYED)

    # 8. Secure-by-default: denied but approvable (gated, not hard-denied).
    return ExecDecision(False, LAYER_DEFAULT_DENY, TRUST_GATED)


def _shadowed_grants(ctx: ExecTrustContext, name: str, exec_type: str) -> tuple[str, ...]:
    """Return lower-authority grant layers overridden by a deny decision."""
    shadowed: list[str] = []
    if _map_grants(ctx.project_allow, name, exec_type):
        shadowed.append(LAYER_PROJECT_ALLOW)
    if _map_grants(ctx.user_allow, name, exec_type):
        shadowed.append(LAYER_USER_ALLOW)
    if name in ctx.org_recommend or name in ctx.org_enforce:
        shadowed.append(LAYER_ORG_RECOMMEND)
    return tuple(shadowed)


# -------------------------------------------------------------------
# Approval key construction
# -------------------------------------------------------------------


def build_approval_key(package_name: str, version: str) -> str:
    """Build the ``allowExecutables`` key for a resolved package.

    Uses the format ``<name>#<version>`` which works for all package
    sources (marketplace, git, registry).  The caller is responsible for
    providing the canonical *package_name* (e.g. ``owner/repo`` for git,
    ``name@marketplace`` for marketplace packages).
    """
    if not version:
        return package_name
    return f"{package_name}#{version}"


@dataclass(frozen=True)
class LockedApproval:
    """The trust principal a locked dependency is granted under.

    Attributes:
        identity: Approval name the install gate checks.
        version: Version bound into the grant key; empty for a replaced row,
            whose identity already carries the selected ``with:`` ref.
        original_identity: Locked identity a ``replace:`` rule redirected,
            or None for a row that was not replaced.
    """

    identity: str
    version: str
    original_identity: str | None = None

    @property
    def key(self) -> str:
        """Return the full approval key (``identity#version``)."""
        return build_approval_key(self.identity, self.version)


def locked_dependency_approval(dependency: Any, version: str | None = None) -> LockedApproval:
    """Return the trust principal for a locked dependency.

    A replaced row is keyed by its selected source, exactly as the install
    gate keys the resolved reference, so an upstream grant never unlocks the
    replacement and ``apm approve`` writes the key the gate checks. *version*
    overrides the locked version for a row that was not replaced; ``apm
    approve`` passes the package's ``apm.yml`` version there, as it always has.
    """
    if getattr(dependency, "replacement", None) is not None:
        return LockedApproval(
            identity=dependency_approval_key(dependency.to_dependency_ref()),
            version="",
            original_identity=dependency.get_unique_key(),
        )
    if version is None:
        version = dependency.version or ""
    return LockedApproval(identity=dependency.get_unique_key(), version=version)


def locked_dependency_approval_keys(dependency: Any) -> tuple[str, ...]:
    """Return trust keys derived only from a locked package identity."""
    approval = locked_dependency_approval(dependency)
    if approval.original_identity is not None:
        return tuple(dict.fromkeys((approval.key, _strip_version(approval.key))))
    return tuple(dict.fromkeys((approval.identity, approval.key)))


class ApprovalSpecError(ValueError):
    """A CLI spec names no single installed package, so no store key is derived."""


def canonical_approval_spec(spec: str, lockfile: Any, *, selected_key: str | None = None) -> str:
    """Return the approval-store identity a spec names (#4189, #4245).

    The single owner shared by ``apm approve``, ``apm deny``, ``apm policy
    explain`` and the store reader. *selected_key* is the ``apm.lock.yaml``
    row the dependency selector (``select_installed_lock_key``, the owner
    ``apm uninstall`` and ``apm audit PACKAGE`` use) matched; its locked
    approval identity is the store key. Without a selection, a marketplace
    spec (``plugin@marketplace[#ref]``) resolves through the lock's recorded
    provenance, and any other spec is already an identity spelling.

    Raises:
        ApprovalSpecError: A marketplace spec names no installed package or
            more than one, so no store key may be derived from it.
    """
    from apm_cli.marketplace.resolver import parse_marketplace_ref

    rows = lockfile.dependencies if lockfile is not None else {}
    if selected_key is not None and selected_key in rows:
        return _strip_version(locked_dependency_approval(rows[selected_key]).identity)
    try:
        parsed = parse_marketplace_ref(spec)
    except ValueError as exc:
        raise ApprovalSpecError(f"{spec}: {exc}") from exc
    if parsed is None:
        return spec
    plugin, marketplace, _ref = parsed
    matches = lockfile.marketplace_rows(marketplace, plugin) if lockfile is not None else []
    if len(matches) != 1:
        why = (
            "no installed package came from" if not matches else "several installed packages match"
        )
        raise ApprovalSpecError(
            f"{spec}: {why} {plugin}@{marketplace}. Name the package as "
            "'apm approve --pending' lists it."
        )
    return _strip_version(locked_dependency_approval(matches[0]).identity)


_CANONICALIZED_KEYS_NOTED: set[tuple[str, str]] = set()


def _readable_lockfile(root: Path) -> Any:
    """The scope's lockfile for store-key migration, or None when unreadable.

    The lockfile's own reader reports a malformed or conflicted file to the
    command that needs it; migration simply does not apply then.
    """
    from apm_cli.deps.lockfile import LockFile, LockfileFormatError, get_lockfile_path

    try:
        return LockFile.read(get_lockfile_path(root))
    except (LockfileFormatError, OSError):
        return None


def canonicalize_store_keys(
    grants: dict[str, Any], lockfile: Any, *, store: str, drop_raw: bool = False
) -> dict[str, Any]:
    """Fold store keys written as an install spec onto the package identity (#4189).

    A store key spelled as an install spec (``plugin@marketplace``) applies to
    the identity :func:`canonical_approval_spec` resolves: a deny merges its
    types, an allow never overrides an identity grant. Each mapping is
    announced once.
    """
    result = dict(grants)
    for key, value in grants.items():
        try:
            canonical = canonical_approval_spec(key, lockfile)
        except ApprovalSpecError:
            continue
        if canonical == key:
            continue
        existing = result.get(canonical)
        if isinstance(existing, dict) and isinstance(value, dict) and store.endswith("deny"):
            result[canonical] = {**value, **existing}
        else:
            result.setdefault(canonical, value)
        if drop_raw:
            result.pop(key, None)
        if (key, canonical) not in _CANONICALIZED_KEYS_NOTED:
            _CANONICALIZED_KEYS_NOTED.add((key, canonical))
            from apm_cli.utils.console import _rich_warning

            _rich_warning(
                f"{store} entry '{key}' names an install spec; it applies to {canonical}. "
                "The next apm approve or apm deny rewrites it under that identity."
            )
    return result


def dependency_approval_key(dependency_ref: Any) -> str:
    """Return the trust key for a resolved dependency reference.

    Uses the lockfile identity domain that ``apm approve`` writes (bare
    ``owner/repo`` for github.com), bound to the selected source so a
    replacement never inherits upstream approval. A replacement is keyed by
    the ``with:`` source the consumer declared; its absolute local anchor is
    a materialization coordinate, not a trust principal.
    """
    source = dependency_ref.source_reference
    if dependency_ref.replacement is not None:
        source = replace(source, anchored_local_path=None)
    return build_approval_key(source.get_unique_key(), source.reference or "")


def local_bundle_approval_key(
    package_id: str,
    version: str,
    source_dir: Path,
    lockfile: dict[str, Any] | None = None,
) -> str:
    """Bind local-bundle executable consent to every deployable content byte."""
    from .trust_fingerprint import local_bundle_digest

    digest = local_bundle_digest(source_dir, lockfile)
    artifact_version = version or "local"
    return f"{package_id}#{artifact_version}@{digest}"


def local_bundle_approval_hint(approval_key: str, exec_type: str, *, user_scope: bool) -> str:
    """Name the ``apm approve`` command that grants *approval_key*, then its manifest entry.

    Grants live in the scope's manifest: the project ``apm.yml``, or the user
    manifest (``$APM_HOME/apm.yml``) that ``apm install -g`` reads. The command
    and its next step come from the remediation catalog (#4436); the entry is
    the same grant written by hand.
    """
    from apm_cli.core.remediation import LOCAL_BUNDLE_EXECUTABLES_UNAPPROVED
    from apm_cli.core.scope import InstallScope, get_manifest_path
    from apm_cli.security.trust import approve_command

    target = (
        f"the user manifest {get_manifest_path(InstallScope.USER)}" if user_scope else "apm.yml"
    )
    install = "apm install -g <bundle-path>" if user_scope else "apm install <bundle-path>"
    remedy = LOCAL_BUNDLE_EXECUTABLES_UNAPPROVED.bind(
        key=approval_key, exec_type=exec_type, install=install
    ).with_command(approve_command(approval_key, user_scope=user_scope))
    return (
        f"To {remedy.fixes_promise}, run:\n"
        f"    {remedy.command}\n"
        f"then {remedy.then}. The same grant as this entry in {target}:\n"
        "executables:\n"
        "  allow:\n"
        f'    "{approval_key}":\n'
        f"      {exec_type}: true"
    )


# -------------------------------------------------------------------
# Package scanning
# -------------------------------------------------------------------


def scan_package_executables(
    install_path: Path,
    package_name: str,
    package_version: str,
    *,
    approval_identity: str | None = None,
    manifest_data: dict[str, Any] | None = None,
    is_transitive: bool = False,
    parent_name: str | None = None,
    original_identity: str | None = None,
) -> ExecutableDeclaration:
    """Scan a materialised package directory for executable primitives.

    Checks for:
    - ``.apm/hooks/*.json`` and ``hooks/*.json`` -- hook definitions
      (mirrors :meth:`HookIntegrator.find_hook_files`)
    - ``bin/`` directory -- bin executables
    - MCP and LSP are declared in the package's ``apm.yml`` under
      ``dependencies.mcp`` and ``dependencies.lsp`` -- so we parse those.
    - ``.apm/extensions/<name>/extension.mjs`` -- canvas extension bundles
      (mirrors :meth:`CanvasIntegrator.find_canvas_bundles`)

    Returns an :class:`ExecutableDeclaration` (may have zero counts if
    the package declares no executables).
    """
    key = build_approval_key(approval_identity or package_name, package_version)

    # 1. Hooks: .apm/hooks/*.json and hooks/*.json (aligned with
    #    HookIntegrator.find_hook_files -- only JSON files are actionable).
    hook_files: list[Path] = []
    for hook_dir in [install_path / ".apm" / "hooks", install_path / "hooks"]:
        if hook_dir.is_dir():
            hook_files.extend(
                sorted(f for f in hook_dir.glob("*.json") if f.is_file() and not f.is_symlink())
            )
    hook_details = [f.name for f in hook_files]

    # 2. Bin executables: top-level bin/ AND .apm/skills/*/bin/
    bin_files: list[Path] = []
    for bin_dir in [install_path / "bin"]:
        if bin_dir.is_dir():
            bin_files.extend(
                f for f in bin_dir.iterdir() if f.is_file() and not f.name.startswith(".")
            )
    # Also scan skill-level bin/ directories
    apm_skills = install_path / ".apm" / "skills"
    if apm_skills.is_dir():
        for skill_dir in apm_skills.iterdir():
            skill_bin = skill_dir / "bin"
            if skill_bin.is_dir():
                bin_files.extend(
                    f for f in skill_bin.iterdir() if f.is_file() and not f.name.startswith(".")
                )
    bin_files = sorted(set(bin_files))
    bin_details = [f.name for f in bin_files]

    # 3. MCP servers: parse from apm.yml dependencies.mcp
    mcp_count = 0
    mcp_details: list[str] = []
    lsp_count = 0
    lsp_details: list[str] = []
    apm_yml = manifest_file(install_path)
    data = manifest_data
    if data is None and apm_yml.is_file():
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml)
        except Exception:
            data = None
    if isinstance(data, dict):
        deps = data.get("dependencies", {})
        if isinstance(deps, dict):
            mcp_list = deps.get("mcp", [])
            if isinstance(mcp_list, list):
                mcp_count = len(mcp_list)
                for entry in mcp_list:
                    if isinstance(entry, str):
                        mcp_details.append(entry)
                    elif isinstance(entry, dict):
                        mcp_details.append(entry.get("name", str(entry)))
            lsp_list = deps.get("lsp", [])
            if isinstance(lsp_list, list):
                lsp_count = len(lsp_list)
                for entry in lsp_list:
                    if isinstance(entry, str):
                        lsp_details.append(entry)
                    elif isinstance(entry, dict):
                        lsp_details.append(entry.get("name", str(entry)))

    # 4. Canvas extensions: .apm/extensions/<name>/extension.mjs
    #    Mirrors CanvasIntegrator.find_canvas_bundles marker detection.
    canvas_marker = "extension.mjs"
    canvas_dirs: list[Path] = []
    extensions_root = install_path / ".apm" / "extensions"
    if extensions_root.is_dir():
        for ext_dir in extensions_root.iterdir():
            if ext_dir.is_dir() and (ext_dir / canvas_marker).is_file():
                canvas_dirs.append(ext_dir)
    canvas_dirs = sorted(canvas_dirs)
    canvas_details = [d.name for d in canvas_dirs]

    return ExecutableDeclaration(
        package_key=key,
        package_name=package_name,
        is_transitive=is_transitive,
        parent_name=parent_name,
        original_identity=original_identity,
        hook_count=len(hook_files),
        mcp_count=mcp_count,
        lsp_count=lsp_count,
        bin_count=len(bin_files),
        canvas_count=len(canvas_dirs),
        hook_details=hook_details,
        mcp_details=mcp_details,
        lsp_details=lsp_details,
        bin_details=bin_details,
        canvas_details=canvas_details,
    )


# -------------------------------------------------------------------
# Interactive approval prompt
# -------------------------------------------------------------------


def _is_interactive() -> bool:
    """Return True when stdin is a TTY and not suppressed by env vars."""
    if os.environ.get("APM_NON_INTERACTIVE") or os.environ.get("CI"):
        return False
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def trust_remedy(package: str, recommend: Collection[str] = (), *, user_scope: bool = False) -> str:
    """One remediation line for parked executables.

    Offers ``apm approve --recommended`` only when the org policy declares an
    ``executables.recommend`` set; otherwise that command finds nothing. A
    user-scope install names the ``-g`` commands (#4154).
    """
    from apm_cli.security.trust import approve_command, explain_command

    bulk = (
        f"Trust the org-vetted set: {approve_command('--recommended', user_scope=user_scope)}  |  "
        if recommend
        else ""
    )
    return (
        f"{bulk}Trust one: {approve_command(package, user_scope=user_scope)}  |  "
        f"Inspect: {explain_command(package, user_scope=user_scope)}"
    )


def prompt_executable_approval(
    declarations: list[ExecutableDeclaration],
    *,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    trust_all: bool = False,
    no_executables: bool = False,
    recommend: Collection[str] = (),
) -> dict[str, dict[str, bool]]:
    """Run the interactive approval flow for packages with executables.

    Args:
        declarations: Executable declarations for packages that need
            approval (already filtered to only those with executables).
        allow_executables: Existing approvals map (project ``apm.yml``
            ``allowExecutables`` overlaid with user-local
            ``~/.apm/approvals.yml``); merged into result for packages
            already approved.
        trust_all: When True, auto-approve everything without prompting.
        no_executables: When True, deny everything without prompting.

    Returns:
        Updated approvals dict ready to persist to the user-local
        ``~/.apm/approvals.yml``.

    Raises:
        SystemExit: In non-interactive mode when unapproved executables
            exist and neither *trust_all* nor *no_executables* is set.
    """
    import click

    from ..utils.console import _rich_echo, _rich_info, _rich_warning

    result = dict(allow_executables or {})

    # Filter to only declarations that actually have executables and are
    # not already fully approved.
    pending = [d for d in declarations if d.has_executables and not _is_fully_approved(result, d)]

    if not pending:
        return result

    # --no-executables: deny everything
    if no_executables:
        return result

    # --trust-all: approve everything
    if trust_all:
        for decl in pending:
            _record_prompt_approval(result, decl)
        return result

    # Non-interactive (CI): hard error
    if not _is_interactive():
        _rich_warning(f"{len(pending)} package(s) ship executables that are not trusted yet:")
        for decl in pending:
            provenance = "(transitive)" if decl.is_transitive else "(direct)"
            _rich_echo(f"  {decl.package_key} {provenance}: {decl.summary_line()}")
        _rich_echo("")
        _rich_info(trust_remedy("<package>", recommend), symbol="info")
        sys.exit(1)

    # Interactive: prompt per-package
    _rich_warning(f"{len(pending)} package(s) declare executable primitives:")
    _rich_echo("")

    for decl in pending:
        provenance = "transitive" if decl.is_transitive else "direct dependency"
        if decl.is_transitive and decl.parent_name:
            provenance = f"transitive via {decl.parent_name}"
        _rich_echo(f"  {decl.package_key} ({provenance})")
        _rich_echo(f"    {decl.summary_line()}")
        for pin in _prompt_pins(decl):
            from .trust_fingerprint import short_fingerprint

            digest = short_fingerprint(pin.fingerprint) if pin.fingerprint else "--------"
            _rich_echo(f"      {pin.exec_type:<6} {pin.item}  {pin.summary}  {digest}")
        _rich_echo("")

    _rich_echo("  These will execute code on your machine when triggered by")
    _rich_echo("  your IDE or by 'apm run'.")
    _rich_echo("")

    for decl in pending:
        approved = click.confirm(
            f"  Trust {decl.package_name}?",
            default=False,
        )
        if approved:
            _record_prompt_approval(result, decl)
        _rich_echo("")

    return result


def _prompt_pins(decl: ExecutableDeclaration) -> list:
    """Pins for one prompted package (empty when its install path is unknown)."""
    if decl.install_path is None:
        return []
    from .trust import package_pins

    return package_pins(Path(decl.install_path), transitive=decl.is_transitive)


def _record_prompt_approval(result: dict, decl: ExecutableDeclaration) -> None:
    """Record a prompt approval pinned to the definitions just listed (#3902)."""
    from .trust import record_pins

    if decl.install_path is None:
        result[decl.package_key] = {t: True for t in decl.exec_types}
        return
    pins = [pin for pin in _prompt_pins(decl) if pin.fingerprint is not None]
    record_pins(result, _strip_version(decl.package_key), decl.exec_types, pins)


def _is_fully_approved(
    allow_executables: dict[str, dict[str, bool]],
    decl: ExecutableDeclaration,
) -> bool:
    """Return True if all exec types in *decl* are approved."""
    entry = allow_executables.get(decl.package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return all(entry.get(t, False) is True for t in decl.exec_types)


# -------------------------------------------------------------------
# Manifest read/write helpers
# -------------------------------------------------------------------


def parse_allow_executables(data: dict[str, Any]) -> dict[str, dict[str, bool]] | None:
    """Parse the ``allowExecutables`` block from raw apm.yml data.

    Returns ``None`` when the block is absent.  Raises ``ValueError``
    on schema violations (non-dict values, unknown exec types with
    non-bool values).
    """
    raw = data.get("allowExecutables")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            "allowExecutables must be a mapping of "
            "package keys to {hooks: bool, mcp: bool, lsp: bool, bin: bool, canvas: bool}"
        )

    result: dict[str, dict[str, bool]] = {}
    for pkg_key, entry in raw.items():
        if not isinstance(pkg_key, str):
            raise ValueError(f"allowExecutables key must be a string, got {type(pkg_key).__name__}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"allowExecutables[{pkg_key!r}] must be a mapping "
                f"of exec types to booleans, got {type(entry).__name__}"
            )
        parsed_entry: dict[str, bool] = {}
        for exec_type, value in entry.items():
            exec_type_str = str(exec_type)
            if exec_type_str not in ALL_EXEC_TYPES:
                raise ValueError(
                    f"allowExecutables[{pkg_key!r}]: unknown exec type "
                    f"{exec_type_str!r} (valid: {', '.join(ALL_EXEC_TYPES)})"
                )
            if not isinstance(value, bool):
                raise ValueError(
                    f"allowExecutables[{pkg_key!r}][{exec_type_str!r}] "
                    f"must be a boolean, got {type(value).__name__}"
                )
            parsed_entry[exec_type_str] = value
        result[str(pkg_key)] = parsed_entry

    return result


def write_allow_executables(
    manifest_path: Path,
    allow_executables: dict[str, dict[str, bool]],
) -> None:
    """Persist *allow_executables* back to the project's ``apm.yml``.

    Reads the existing YAML through the manifest owner, updates the
    ``allowExecutables`` key, and writes it back round-trip so comments,
    key order and anchors survive.

    Note: this writes only the project gate opt-in signal
    (``allowExecutables: {}``) and the CI/automated-context approvals that are
    intentionally committed.  Personal, machine-local consent lives in
    ``~/.apm/config.json`` under ``executables: {allow, deny}`` (see
    :func:`save_user_executables`); there is no standalone approvals file.
    """
    from ..models.manifest_io import load_manifest_roundtrip
    from ..utils.yaml_io import dump_yaml_roundtrip

    data = load_manifest_roundtrip(manifest_path)
    if not isinstance(data, dict):
        return

    if allow_executables:
        data["allowExecutables"] = allow_executables
    elif "allowExecutables" in data:
        del data["allowExecutables"]

    dump_yaml_roundtrip(data, manifest_path)


class EffectiveExecMap(dict):
    """The materialised type-level allow-map, carrying its trust context.

    The dict holds ONLY type-level grants, so a caller that knows nothing of
    pinned items sees "not granted" and fails closed. Callers that admit
    pinned items ask :mod:`apm_cli.security.trust` with ``trust_context``.
    """

    trust_context: ExecTrustContext | None = None
    # Why this map authorizes every executable type of every package, when it
    # does. Set only by :func:`authorized_exec_map`; never inferred from None.
    authorized_by: str | None = None


# The project's own content (``_local``) is first-party and always trusted.
FIRST_PARTY_PACKAGE = "_local"
# Explicit blanket authorizations. A missing or omitted map is never one.
AUTHORIZED_GATE_DISABLED = LAYER_GATE_DISABLED
AUTHORIZED_REPLAY = "replay-already-authorized"
AUTHORIZED_FIRST_PARTY = "first-party"
_BLANKET_AUTHORIZATIONS = frozenset(
    {AUTHORIZED_GATE_DISABLED, AUTHORIZED_REPLAY, AUTHORIZED_FIRST_PARTY}
)


def authorized_exec_map(reason: str) -> EffectiveExecMap:
    """Return a map that explicitly authorizes every executable for *reason*.

    ``AUTHORIZED_GATE_DISABLED``: no layer opted into the gate (no policy
    signals), so every executable deploys. ``AUTHORIZED_REPLAY``: audit replay
    re-derives content the install gate already authorized.
    ``AUTHORIZED_FIRST_PARTY``: the project's own content (``_local``).
    """
    if reason not in _BLANKET_AUTHORIZATIONS:
        raise ValueError(f"Unknown executable authorization: {reason!r}")
    allow_map = EffectiveExecMap()
    allow_map.authorized_by = reason
    return allow_map


def exec_map_authorizes_all(allow_map: object) -> bool:
    """Return whether *allow_map* explicitly authorizes every executable.

    ``None``, a plain dict, and an omitted map authorize nothing beyond their
    own literal grants.
    """
    return isinstance(allow_map, EffectiveExecMap) and allow_map.authorized_by is not None


def closed_exec_map(allow_map: object) -> object:
    """Return *allow_map*, or an empty grant map when it is missing.

    Item admission reads ``None`` as "gate off"; a missing map handed to it
    becomes a gate-on map with no grants, so it admits nothing.
    """
    return EffectiveExecMap() if allow_map is None else allow_map


def package_type_approved(allow_map: object, package_keys: Any, exec_type: str) -> bool:
    """Return the package-level approval of *exec_type* for any of *package_keys*.

    Pinned items are not read here; ``security/trust.py`` admits them.
    """
    if exec_map_authorizes_all(allow_map):
        return True
    if not isinstance(allow_map, dict):
        return False
    return any(is_package_approved(allow_map, key, exec_type) for key in package_keys)


def package_type_approvals(allow_map: object, package_keys: Any) -> dict[str, bool]:
    """Return :func:`package_type_approved` for every executable type."""
    keys = tuple(package_keys)
    return {
        exec_type: package_type_approved(allow_map, keys, exec_type) for exec_type in ALL_EXEC_TYPES
    }


def compose_trust_bin(
    bin_approved: bool,
    trust_bin: bool | None,
    *,
    non_interactive: bool,
) -> tuple[bool, str | None]:
    """Compose the bin approval with the ``--trust-bin`` posture.

    Returns ``(skip_bin, reason)``. Without the flag a non-interactive run
    does not trust bin/ executables.
    """
    if not bin_approved:
        return True, "not_approved"
    if trust_bin is False:
        return True, "not_trusted"
    if trust_bin is None and non_interactive:
        return True, "not_trusted"
    return False, None


def materialize_exec_map(ctx: ExecTrustContext) -> EffectiveExecMap:
    """Materialise the deny-wins effective allow-map from a trust context.

    Returns an explicit :data:`AUTHORIZED_GATE_DISABLED` map when the gate is
    disabled; otherwise every candidate package key is run through
    :func:`resolve_exec_decision` and only ALLOWED ``(key, exec_type)`` pairs
    are emitted, each also under its version-blind name so the gate's
    exact-membership lookup matches any installed version.
    """
    if not ctx.gate_enabled:
        return authorized_exec_map(AUTHORIZED_GATE_DISABLED)

    candidate_keys: set[str] = set()
    candidate_keys |= set(ctx.project_allow) | set(ctx.project_deny)
    candidate_keys |= set(ctx.user_allow) | set(ctx.user_deny)
    candidate_keys |= set(ctx.org_recommend) | set(ctx.org_deny) | set(ctx.org_enforce)
    candidate_keys |= set(ctx.org_bin_deny)

    result = EffectiveExecMap()
    result.trust_context = ctx
    for key in candidate_keys:
        for exec_type in ALL_EXEC_TYPES:
            if not resolve_exec_decision(ctx, key, exec_type).allowed:
                continue
            result.setdefault(key, {})[exec_type] = True
            name = _strip_version(key)
            if name != key and not _is_content_bound_key(key):
                result.setdefault(name, {})[exec_type] = True
    return result


def exec_status_for_declaration(
    ctx: ExecTrustContext,
    candidate_keys: list[str],
    exec_types: tuple[str, ...],
) -> str | None:
    """Return the lockfile ``exec_status`` for a package's declared executables.

    Resolves every declared exec type across the candidate keys and folds the
    decisions into ONE worst-case trust state for the lockfile field:

    * any declared type hard-DENIED   -> ``denied``
    * else any declared type not allowed -> ``gated_pending_approval``
    * else (all declared types allowed)  -> ``deployed``

    Returns ``None`` when the package declares no executables (the audit then
    treats it as trusted) or when the gate is disabled.
    """
    if not exec_types or not ctx.gate_enabled:
        return None

    worst = TRUST_DEPLOYED
    for exec_type in exec_types:
        best = None
        for key in candidate_keys:
            decision = resolve_exec_decision(ctx, key, exec_type)
            if decision.allowed:
                best = TRUST_DEPLOYED
                break
            # Prefer the more severe of denied/gated across candidate keys.
            if decision.trust_state == TRUST_DENIED:
                best = TRUST_DENIED
            elif best is None:
                best = TRUST_GATED
        if best == TRUST_DENIED:
            return TRUST_DENIED
        if best == TRUST_GATED:
            worst = TRUST_GATED
    return worst


def denied_exec_types(
    ctx: ExecTrustContext,
    candidate_keys: list[str],
    exec_types: tuple[str, ...] | list[str],
) -> frozenset[str]:
    """Return the exec types a deny layer refuses for every candidate key.

    A type counts as denied only when no candidate key allows it and at least
    one resolves to ``denied``; a default-deny (approvable) gate is not a deny.
    """
    denied: set[str] = set()
    for exec_type in exec_types:
        decisions = [resolve_exec_decision(ctx, key, exec_type) for key in candidate_keys]
        if any(d.allowed for d in decisions):
            continue
        if any(d.trust_state == TRUST_DENIED for d in decisions):
            denied.add(exec_type)
    return frozenset(denied)


def build_effective_exec_map(
    *,
    policy: Any | None,
    project_data: dict[str, Any] | None,
    migrate_user_legacy: bool = True,
) -> EffectiveExecMap:
    """Materialise the deny-wins effective allow-map consumed by the install gate.

    This is the #1873 replacement for the legacy ``{**project, **user}``
    user-wins merge. See :func:`materialize_exec_map` for the emission rules.

    Returns an explicit :data:`AUTHORIZED_GATE_DISABLED` map when the gate is
    disabled (backward-compatible: every executable deploys), mirroring
    :attr:`ExecTrustContext.gate_enabled`.
    """
    ctx = build_exec_trust_context(
        policy=policy,
        project_data=project_data,
        migrate_user_legacy=migrate_user_legacy,
    )
    return materialize_exec_map(ctx)


def exec_trust_context_for_project(
    manifest_path: Path,
    *,
    policy: Any | None,
    fallback_allow_executables: dict[str, dict[str, bool]] | None = None,
    logger: Any | None = None,
    migrate_user_legacy: bool = True,
    include_user: bool = True,
) -> ExecTrustContext:
    """Resolve project, user, and policy executable trust through one owner.

    *manifest_path* is the scope's ``apm.yml`` itself, never a directory the
    caller picked: under ``-g`` it is ``APM_HOME/apm.yml``, which the home
    directory (the user-scope source and deploy root) does not hold (#3954).
    """
    from apm_cli.models.manifest_io import load_manifest

    project_data: dict[str, Any] | None = None
    if manifest_path.is_file():
        data = load_manifest(manifest_path)
        if isinstance(data, dict):
            project_data = data
            if data.get("allowExecutables") is not None:
                warn_allow_executables_alias_once(logger)
    if project_data is None and isinstance(fallback_allow_executables, dict):
        project_data = {"allowExecutables": fallback_allow_executables}
    ctx = build_exec_trust_context(
        policy=policy,
        project_data=project_data,
        migrate_user_legacy=migrate_user_legacy,
        include_user=include_user,
        lockfile=_readable_lockfile(manifest_path.parent),
    )
    if logger is not None:
        from .trust import warn_content_blind_grants_once

        warn_content_blind_grants_once(ctx, logger)
    return ctx


def effective_exec_map_for_project(
    project_root: Path,
    *,
    policy: Any | None,
    fallback_allow_executables: dict[str, dict[str, bool]] | None = None,
    logger: Any | None = None,
    migrate_user_legacy: bool = True,
    user_scope: bool = False,
) -> EffectiveExecMap:
    """Materialize the canonical trust context for one project."""
    from apm_cli.core.scope import scope_manifest_path

    return materialize_exec_map(
        exec_trust_context_for_project(
            scope_manifest_path(project_root, user_scope=user_scope),
            policy=policy,
            fallback_allow_executables=fallback_allow_executables,
            logger=logger,
            migrate_user_legacy=migrate_user_legacy,
        )
    )


def resolve_project_exec_allow_map(
    project_root: Path,
    apm_package: Any,
    *,
    user_scope: bool,
    executable_context: tuple[dict[str, dict[str, bool]] | None, bool] = (None, False),
    no_policy: bool = False,
    logger: Any | None = None,
) -> EffectiveExecMap:
    """Return the deny-wins executables map for one project command.

    Reuses a map the caller already resolved (``executable_context`` is
    ``(map, resolved)``); otherwise resolves org policy (unless disabled),
    project, and user layers through :func:`effective_exec_map_for_project`.
    Org policy and the grants both come from the scope's manifest root
    (``$APM_HOME`` at user scope, which has no git remote).
    """
    allow_map, resolved = executable_context
    if resolved:
        return allow_map
    from apm_cli.core.scope import manifest_root
    from apm_cli.policy.outcome_routing import resolve_trust_policy

    policy = resolve_trust_policy(
        manifest_root(project_root, user_scope=user_scope), no_policy=no_policy, logger=logger
    )

    return effective_exec_map_for_project(
        project_root,
        user_scope=user_scope,
        policy=policy,
        fallback_allow_executables=getattr(apm_package, "allow_executables", None),
        logger=logger,
    )


def filter_lsp_by_allow_executables(
    lsp_deps: list,
    effective_allow_execs: dict | None,
    logger: Any,
) -> list:
    """Filter transitive LSP deps by their declaring package's decision."""
    return _filter_declared_services(
        lsp_deps,
        effective_allow_execs,
        logger,
        exec_type=EXEC_TYPE_LSP,
        service_label="LSP",
    )


def _filter_declared_services(
    dependencies: list,
    effective_allow_execs: dict | None,
    logger: Any,
    *,
    exec_type: str,
    service_label: str,
    gated: Any = None,
) -> list:
    """Keep root-declared services and services whose declaring package is approved."""
    if exec_map_authorizes_all(effective_allow_execs) or not dependencies:
        return dependencies
    filtered = []
    skipped: dict[str, bool] = {}
    denied_owners: set[str] = set()
    for dependency in dependencies:
        owner = getattr(dependency, "resolved_by", None)
        approval_keys = getattr(dependency, "approval_keys", ())
        if (
            owner is None
            or (gated is not None and not gated(dependency))
            or package_type_approved(effective_allow_execs, approval_keys, exec_type)
        ):
            filtered.append(dependency)
            continue
        skipped[owner] = bool(approval_keys)
        dependency_name = getattr(dependency, "name", "(unnamed)")
        trust_ctx = getattr(effective_allow_execs, "trust_context", None)
        if (
            approval_keys
            and trust_ctx is not None
            and denied_exec_types(trust_ctx, list(approval_keys), (exec_type,))
        ):
            denied_owners.add(owner)
        if owner in denied_owners:
            logger.verbose_detail(
                f"Skipping {service_label} server '{dependency_name}' from '{owner}': "
                f"denied. Run 'apm policy explain {owner}' to see which deny applies."
            )
        elif approval_keys:
            logger.verbose_detail(
                f"Skipping {service_label} server '{dependency_name}' from '{owner}': "
                f"executables not trusted. {MCP_EXECUTABLES_UNTRUSTED.bind(package=owner).sentence()}"
            )
        else:
            logger.verbose_detail(
                f"Skipping {service_label} server '{dependency_name}' from '{owner}': package "
                "identity cannot be verified without lock state. "
                f"{EXECUTABLES_IDENTITY_UNLOCKED.bind(subject='the package').sentence()}"
            )
    if len(filtered) < len(dependencies):
        skipped_count = len(dependencies) - len(filtered)
        noun = "server" if skipped_count == 1 else "servers"
        owners = ", ".join(f"'{owner}'" for owner in sorted(skipped))
        known_owners = sorted(
            owner for owner, has_keys in skipped.items() if has_keys and owner not in denied_owners
        )
        unlocked_owners = sorted(owner for owner, has_keys in skipped.items() if not has_keys)
        if denied_owners:
            # A denied package is named as denied; approving cannot lift it (#4237).
            denied_names = ", ".join(f"'{owner}'" for owner in sorted(denied_owners))
            denied_count = sum(
                1
                for dependency in dependencies
                if getattr(dependency, "resolved_by", None) in denied_owners
            )
            denied_noun = "server" if denied_count == 1 else "servers"
            explain = (
                f"Run 'apm policy explain {sorted(denied_owners)[0]}' to see which deny applies."
                if len(denied_owners) == 1
                else "Run 'apm policy explain <package>' to see which deny applies."
            )
            logger.warning(
                f"Filtered {denied_count} {service_label} {denied_noun} from {denied_names}: "
                f"declaring package {'is' if len(denied_owners) == 1 else 'are'} denied. "
                f"{explain}",
                symbol="warning",
            )
            skipped = {o: k for o, k in skipped.items() if o not in denied_owners}
            skipped_count -= denied_count
            owners = ", ".join(f"'{owner}'" for owner in sorted(skipped))
        remediation_parts = []
        if known_owners:
            remediation_parts.append(
                (f"Run 'apm policy explain {known_owners[0]}'; approve it only if policy permits.")
                if len(known_owners) == 1
                else (
                    "Run 'apm policy explain <package>' for each identified package; "
                    "approve only packages policy permits."
                )
            )
        if unlocked_owners:
            package_noun = "that package" if len(unlocked_owners) == 1 else "each package"
            remediation_parts.append(
                EXECUTABLES_IDENTITY_UNLOCKED.bind(subject=package_noun).sentence()
            )
        remediation = " " + " ".join(remediation_parts)
        package_clause = "declaring package is" if len(skipped) == 1 else "declaring packages are"
        if skipped:
            noun = "server" if skipped_count == 1 else "servers"
            logger.warning(
                f"Filtered {skipped_count} {service_label} {noun} from {owners}: "
                f"{package_clause} not trusted yet.{remediation}",
                symbol="warning",
            )
    return filtered


# -------------------------------------------------------------------
# Unified vocabulary layer (issue #1873): one noun ``executables``
# -------------------------------------------------------------------
#
# Project apm.yml: ``executables: {allow, deny}`` (the deprecated
# ``allowExecutables`` block remains a read alias for one minor cycle).
# Personal consent: ``~/.apm/config.json`` under ``executables:{allow,deny}``
# (lowest authority). The standalone ``~/.apm/approvals.yml`` is migrated on
# first read and DELETED -- net-new control-surface files = 0.


# Executable types whose grants may pin individual items to a fingerprint.
ITEM_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_MCP)
# A natively loaded plugin is approved as ONE item over its whole root:
# ``executables.allow.<pkg>.plugin: sha256:<64 hex>`` (#3902).
PLUGIN_GRANT_KEY = "plugin"
PLUGIN_ITEM = "."


def _item_error(where: str, pkg_key: str, exec_type: str, item: object) -> ValueError:
    return ValueError(
        f"apm.yml {where}.{pkg_key}.{exec_type}.{item}: expected 'sha256:<64 hex>'. "
        f"{EXECUTABLES_PIN_MALFORMED.bind(package=pkg_key).sentence()}"
    )


def _parse_item_map(raw: dict, *, where: str, pkg_key: str, exec_type: str) -> dict[str, str]:
    """Validate one ``{item: sha256:<64 hex>}`` map; item-level ``true`` is rejected."""
    from .trust_fingerprint import is_fingerprint

    items: dict[str, str] = {}
    for item, value in raw.items():
        if not isinstance(item, str) or not item.strip() or not is_fingerprint(value):
            raise _item_error(where, pkg_key, exec_type, item)
        items[item] = value
    return items


def _parse_grant_block(
    raw: Any,
    *,
    where: str,
    allow_items: bool = False,
) -> dict[str, dict[str, Any]]:
    """Validate and normalise a ``{package_key: {exec_type: bool | items}}`` map.

    With *allow_items*, ``hooks`` and ``mcp`` may hold a pinned
    ``{item: sha256:<64 hex>}`` map instead of a boolean (#3902).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} must be a mapping of package keys to "
            "{hooks: bool, mcp: bool, lsp: bool, bin: bool, canvas: bool}"
        )
    result: dict[str, dict[str, Any]] = {}
    for pkg_key, entry in raw.items():
        if not isinstance(pkg_key, str):
            raise ValueError(f"{where} key must be a string, got {type(pkg_key).__name__}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}[{pkg_key!r}] must be a mapping of exec types to "
                f"booleans, got {type(entry).__name__}"
            )
        parsed: dict[str, Any] = {}
        for exec_type, value in entry.items():
            exec_type_str = str(exec_type)
            if allow_items and exec_type_str == PLUGIN_GRANT_KEY:
                from .trust_fingerprint import is_fingerprint

                if not where.endswith(".allow") or not is_fingerprint(value):
                    raise ValueError(
                        f"apm.yml {where}.{pkg_key}.plugin: expected 'sha256:<64 hex>' under "
                        "executables.allow. "
                        f"{EXECUTABLES_PIN_MALFORMED.bind(package=pkg_key).sentence()}"
                    )
                parsed[exec_type_str] = value
                continue
            if exec_type_str not in ALL_EXEC_TYPES:
                raise ValueError(
                    f"{where}[{pkg_key!r}]: unknown exec type {exec_type_str!r} "
                    f"(valid: {', '.join(ALL_EXEC_TYPES)})"
                )
            if allow_items and exec_type_str in ITEM_EXEC_TYPES and isinstance(value, dict):
                parsed[exec_type_str] = _parse_item_map(
                    value, where=where, pkg_key=pkg_key, exec_type=exec_type_str
                )
                continue
            if not isinstance(value, bool):
                raise ValueError(
                    f"{where}[{pkg_key!r}][{exec_type_str!r}] must be a boolean, "
                    f"got {type(value).__name__}"
                )
            parsed[exec_type_str] = value
        result[pkg_key] = parsed
    return result


def split_item_grants(
    raw: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, dict[str, str]]]]:
    """Split a stored grant map into type-level booleans and pinned item maps.

    Only well-formed entries survive: anything else is dropped, so a damaged
    user store fails closed instead of widening trust.
    """
    from .trust_fingerprint import is_fingerprint

    types: dict[str, dict[str, bool]] = {}
    items: dict[str, dict[str, dict[str, str]]] = {}
    for pkg_key, entry in (raw or {}).items():
        if not isinstance(pkg_key, str) or not isinstance(entry, dict):
            continue
        for exec_type, value in entry.items():
            if exec_type == PLUGIN_GRANT_KEY:
                if is_fingerprint(value):
                    items.setdefault(pkg_key, {})[PLUGIN_GRANT_KEY] = {PLUGIN_ITEM: value}
                continue
            if isinstance(value, bool):
                types.setdefault(pkg_key, {})[str(exec_type)] = value
            elif isinstance(value, dict) and exec_type in ITEM_EXEC_TYPES:
                pinned = {
                    item: fingerprint
                    for item, fingerprint in value.items()
                    if isinstance(item, str) and is_fingerprint(fingerprint)
                }
                # An empty map is meaningful: nothing of this type is approved.
                items.setdefault(pkg_key, {})[exec_type] = pinned
    return types, items


def _deny_types(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """A pinned deny map denies the whole type: deny stays content-blind."""
    return {
        pkg_key: {
            exec_type: (value is True or isinstance(value, dict))
            for exec_type, value in entry.items()
            if exec_type != PLUGIN_GRANT_KEY
        }
        for pkg_key, entry in raw.items()
        if isinstance(entry, dict)
    }


def sorted_grant_map(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return *raw* with packages, types and items sorted for a stable diff."""
    result: dict[str, dict[str, Any]] = {}
    for pkg_key in sorted(raw):
        entry = raw[pkg_key]
        if not isinstance(entry, dict):
            continue
        result[pkg_key] = {
            exec_type: (dict(sorted(value.items())) if isinstance(value, dict) else value)
            for exec_type, value in sorted(entry.items())
        }
    return result


def parse_project_executables(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]], bool]:
    """Parse the project ``executables`` block from raw apm.yml data.

    Returns ``(allow, deny, used_deprecated_alias)``. The deprecated
    ``allowExecutables`` block is folded into ``allow`` (the new
    ``executables.allow`` wins on a per-key conflict); when it is present the
    boolean flag is ``True`` so callers can emit one deprecation warning.
    """
    used_alias = False
    allow: dict[str, dict[str, bool]] = {}

    alias_raw = data.get("allowExecutables")
    if alias_raw is not None:
        used_alias = True
        allow.update(_parse_grant_block(alias_raw, where="allowExecutables"))

    deny: dict[str, dict[str, bool]] = {}
    block = data.get("executables")
    if block is not None:
        if not isinstance(block, dict):
            raise ValueError("executables must be a mapping with 'allow' and/or 'deny' keys")
        allow.update(
            _parse_grant_block(block.get("allow"), where="executables.allow", allow_items=True)
        )
        deny = _parse_grant_block(block.get("deny"), where="executables.deny", allow_items=True)

    return allow, deny, used_alias


_ALIAS_DEPRECATION_WARNED = False


def warn_allow_executables_alias_once(logger: Any | None = None) -> None:
    """Emit the ``allowExecutables`` deprecation warning at most once per run.

    The deprecated ``allowExecutables`` block in ``apm.yml`` still works (it
    folds into ``executables.allow``), but writers should migrate. This warns
    a single time on ``apm install`` / ``apm approve`` so the message is
    actionable without spamming once per package (#1873).
    """
    global _ALIAS_DEPRECATION_WARNED
    if _ALIAS_DEPRECATION_WARNED:
        return
    _ALIAS_DEPRECATION_WARNED = True
    from ..core.deprecations import deprecation_message

    msg = deprecation_message("allow-executables")
    if logger is not None:
        logger.warning(msg, symbol="warning")
        return
    from ..utils.console import _rich_warning

    _rich_warning(msg)


def project_executables_gate_enabled(data: dict[str, Any]) -> bool:
    """Return True when the project opts into the gate (any block present)."""
    return data.get("executables") is not None or data.get("allowExecutables") is not None


def _user_config_file() -> Path:
    """Return the path to the user-local JSON config (override seam in tests)."""
    from .. import config

    return Path(config.CONFIG_FILE)


def _legacy_approvals_path() -> Path:
    """Return the path to the deprecated user ``approvals.yml`` store.

    Read-only: the file is migrated into the adjacent ``config.json`` on first
    read and deleted. There is no writer for this path anymore (#1873).
    """
    from ..core.scope import get_apm_home

    return get_apm_home() / "approvals.yml"


# A speculative decision (the install fast path, #5428) reads personal consent and
# never migrates it: it runs before its journal, so a later decline could not undo
# the write. The full install (and apm approve) keep migrating.
_SPECULATIVE_CONSENT: list[bool] = []


@contextlib.contextmanager
def speculative_consent() -> Iterator[None]:
    """Read legacy ``approvals.yml`` without migrating it, for the enclosed decision."""
    _SPECULATIVE_CONSENT.append(True)
    try:
        yield
    finally:
        _SPECULATIVE_CONSENT.pop()


def _migrate_legacy_approvals(
    allow: dict[str, dict[str, bool]],
    *,
    persist: bool = True,
) -> dict[str, dict[str, bool]]:
    """Fold a legacy ``approvals.yml`` into *allow* and delete the file.

    The legacy file stored a bare ``{package_key: {exec_type: bool}}`` map of
    grants. Existing config entries win over legacy entries on conflict.
    """
    persist = persist and not _SPECULATIVE_CONSENT
    legacy = _legacy_approvals_path()
    if not legacy.is_file():
        return allow
    from ..utils.yaml_io import load_yaml

    legacy_data = load_yaml(legacy)
    if isinstance(legacy_data, dict):
        for pkg_key, entry in legacy_data.items():
            if isinstance(entry, dict):
                merged = {**{k: bool(v) for k, v in entry.items()}, **allow.get(pkg_key, {})}
                allow[pkg_key] = merged
    if persist:
        with contextlib.suppress(OSError):
            legacy.unlink()
    return allow


def load_user_executables(
    *,
    migrate_legacy: bool = True,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """Load personal executable consent from ``~/.apm/config.json``.

    Returns ``(allow, deny)``. On first read, any legacy
    ``~/.apm/approvals.yml`` is folded into ``allow`` and deleted, and the
    migrated state is persisted back to the config so the fold happens once.
    """
    import json

    cfg_path = _user_config_file()
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            cfg = {}
    section = cfg.get("executables") if isinstance(cfg.get("executables"), dict) else {}
    allow = dict(section.get("allow") or {})
    deny = dict(section.get("deny") or {})

    migrate_legacy = migrate_legacy and not _SPECULATIVE_CONSENT
    original_allow = {key: dict(value) for key, value in allow.items()}
    migrated = _migrate_legacy_approvals(allow, persist=migrate_legacy)
    if migrate_legacy and (migrated != original_allow or (allow and "executables" not in cfg)):
        allow = migrated
        save_user_executables(allow, deny)
    else:
        allow = migrated
    return allow, deny


def save_user_executables(
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Persist personal executable consent into ``~/.apm/config.json``.

    The config file is written owner-only (``0o600``) to keep the consent
    list private on shared systems. The write is atomic (``atomic_write_text``
    with ``new_file_mode=0o600``) so a crash mid-write cannot corrupt the
    shared config and a freshly-created file is never world-readable.
    """
    import json

    from ..utils.atomic_io import atomic_write_text

    cfg_path = _user_config_file()
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            cfg = {}
    cfg["executables"] = {"allow": allow, "deny": deny}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(cfg, indent=2), new_file_mode=0o600)


def build_exec_trust_context(
    *,
    policy: Any | None,
    project_data: dict[str, Any] | None,
    migrate_user_legacy: bool = True,
    include_user: bool = True,
    lockfile: Any | None = None,
) -> ExecTrustContext:
    """Assemble an :class:`ExecTrustContext` from org / project / user inputs.

    Args:
        policy: The merged org :class:`~apm_cli.policy.schema.ApmPolicy`
            (or ``None`` when no policy applies).
        project_data: Raw project ``apm.yml`` data (or ``None``).

    The gate is enabled when ANY layer opts in: the project declares an
    ``executables``/``allowExecutables`` block (even empty), the user has a
    persisted decision, the org policy carries a non-empty ``executables``
    block, or a legacy ``bin_deploy`` deny.
    """
    data = project_data or {}
    project_raw_allow, project_raw_deny, _alias = parse_project_executables(data)
    # ``apm audit`` never reads personal consent: CI and laptops must agree.
    user_raw_allow, user_raw_deny = (
        load_user_executables(migrate_legacy=migrate_user_legacy) if include_user else ({}, {})
    )
    if lockfile is not None:
        project_raw_allow, project_raw_deny, user_raw_allow, user_raw_deny = (
            canonicalize_store_keys(grants, lockfile, store=store)
            for grants, store in (
                (project_raw_allow, "executables.allow"),
                (project_raw_deny, "executables.deny"),
                (user_raw_allow, "user executables.allow"),
                (user_raw_deny, "user executables.deny"),
            )
        )
    project_allow, project_items = split_item_grants(project_raw_allow)
    project_deny = _deny_types(project_raw_deny)
    user_allow, user_items = split_item_grants(user_raw_allow)
    user_deny = _deny_types(user_raw_deny)

    org_deny_all = False
    org_deny: frozenset[str] = frozenset()
    org_require: frozenset[str] = frozenset()
    org_recommend: frozenset[str] = frozenset()
    org_enforce: frozenset[str] = frozenset()
    org_bin_deny_all = False
    org_bin_deny: frozenset[str] = frozenset()
    org_signal = False

    if policy is not None:
        execs = getattr(policy, "executables", None)
        if execs is not None:
            org_deny_all = bool(getattr(execs, "deny_all", False))
            raw_deny = tuple(getattr(execs, "deny", ()) or ())
            org_deny = frozenset(raw_deny) | frozenset(
                canonical_exec_identity(entry) for entry in raw_deny
            )
            org_require = frozenset(getattr(execs, "require", ()) or ())
            org_recommend = frozenset(getattr(execs, "recommend", ()) or ())
            org_enforce = frozenset(getattr(execs, "enforce", ()) or ())
            org_signal = bool(
                org_deny_all or org_deny or org_require or org_recommend or org_enforce
            )
        bin_deploy = getattr(policy, "bin_deploy", None)
        if bin_deploy is not None:
            org_bin_deny_all = bool(getattr(bin_deploy, "deny_all", False))
            org_bin_deny = frozenset(
                normalize_bin_deploy_deny_key(entry)
                for entry in (getattr(bin_deploy, "deny", ()) or ())
            )
            org_signal = org_signal or org_bin_deny_all or bool(org_bin_deny)

    gate_enabled = project_executables_gate_enabled(data) or bool(user_deny) or org_signal

    return ExecTrustContext(
        gate_enabled=gate_enabled,
        org_deny_all=org_deny_all,
        org_deny=org_deny,
        org_require=org_require,
        org_recommend=org_recommend,
        org_enforce=org_enforce,
        org_bin_deny_all=org_bin_deny_all,
        org_bin_deny=org_bin_deny,
        project_allow=project_allow,
        project_deny=project_deny,
        user_allow=user_allow,
        user_deny=user_deny,
        project_items=project_items,
        user_items=user_items,
    )


def load_project_executables(
    manifest_path: Path,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]], bool]:
    """Read the project ``executables`` block (and alias) from ``apm.yml``."""
    from ..models.manifest_io import load_manifest

    if not manifest_path.is_file():
        return {}, {}, False
    data = load_manifest(manifest_path)
    if not isinstance(data, dict):
        return {}, {}, False
    return parse_project_executables(data)


def write_project_executables(
    manifest_path: Path,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Persist project ``executables: {allow, deny}`` back to ``apm.yml``.

    Migrates a legacy ``allowExecutables`` block into ``executables.allow`` on
    write so a project converges on the unified noun. Empty ``allow``/``deny``
    sub-blocks are omitted; an empty ``executables: {}`` is still written when
    the gate was already opted-in so the signal is not lost.
    """
    from ..models.manifest_io import load_manifest_roundtrip
    from ..utils.yaml_io import dump_yaml_roundtrip

    # Round-trip keeps the user's comments and layout (#2000, #2012).
    data = load_manifest_roundtrip(manifest_path)
    if not isinstance(data, dict):
        return

    had_alias = data.pop("allowExecutables", None) is not None
    block: dict[str, Any] = {}
    if allow:
        block["allow"] = sorted_grant_map(allow)
    if deny:
        block["deny"] = sorted_grant_map(deny)

    if block or had_alias or "executables" in data:
        data["executables"] = block
    dump_yaml_roundtrip(data, manifest_path)
