"""Executable primitive approval gate (npm v12-inspired opt-in model).

APM packages can declare four kinds of executable primitives -- hooks,
MCP servers, bin/ executables, and canvas extensions -- that run arbitrary
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

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Executable type constants used as keys in the allowExecutables block.
EXEC_TYPE_HOOKS = "hooks"
EXEC_TYPE_MCP = "mcp"
EXEC_TYPE_BIN = "bin"
EXEC_TYPE_CANVAS = "canvas"

# Types with active enforcement in the install gate.
ENFORCED_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_BIN, EXEC_TYPE_MCP, EXEC_TYPE_CANVAS)

# All recognised exec-type keys (for manifest validation).
ALL_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_MCP, EXEC_TYPE_BIN, EXEC_TYPE_CANVAS)


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
        hook_count: Number of hook files discovered.
        mcp_count: Number of MCP server entries discovered.
        bin_count: Number of bin/ executables discovered.
        canvas_count: Number of canvas extensions discovered.
        hook_details: Per-hook summaries for ``inspect`` display.
        mcp_details: Per-MCP-server summaries.
        bin_details: Per-binary summaries.
        canvas_details: Per-canvas summaries.
    """

    package_key: str
    package_name: str
    is_transitive: bool = False
    parent_name: str | None = None
    hook_count: int = 0
    mcp_count: int = 0
    bin_count: int = 0
    canvas_count: int = 0
    hook_details: list[str] = field(default_factory=list)
    mcp_details: list[str] = field(default_factory=list)
    bin_details: list[str] = field(default_factory=list)
    canvas_details: list[str] = field(default_factory=list)

    @property
    def has_executables(self) -> bool:
        """Return True if this package declares enforced executable primitives."""
        return (
            self.hook_count > 0 or self.bin_count > 0 or self.mcp_count > 0 or self.canvas_count > 0
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
        return ", ".join(parts)


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
        exec_type: One of ``hooks``, ``mcp``, ``bin``.

    Returns:
        ``True`` only when the block contains a matching entry with
        ``{exec_type}: true``.
    """
    if not allow_executables:
        return False
    entry = allow_executables.get(package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return bool(entry.get(exec_type, False))


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
    return any(entry.get(t, False) for t in ALL_EXEC_TYPES)


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


def _strip_version(package_key: str) -> str:
    """Return the version-blind canonical name from an approval key."""
    return package_key.split("#", 1)[0]


def _map_grants(
    grant_map: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Return True if *grant_map* grants *exec_type* for *package_key*.

    Matches the exact key, the version-blind name, or any stored key that
    shares the same version-blind name -- so approving ``owner/repo``
    covers ``owner/repo#v1`` and vice-versa.
    """
    if not grant_map:
        return False
    name = _strip_version(package_key)
    for stored_key, entry in grant_map.items():
        if not isinstance(entry, dict):
            continue
        if (stored_key in (package_key, name) or _strip_version(stored_key) == name) and bool(
            entry.get(exec_type, False)
        ):
            return True
    return False


def _deny_glob_match(name: str, patterns: Any) -> bool:
    """Return True if *name* matches any DENY *pattern* (exact or glob).

    Deny is the org ceiling, so in v1 it supports ``fnmatch`` globs such as
    ``evil/*`` to block a whole publisher fleet-wide. Allow / recommend /
    require remain exact-match only -- widening the GRANT side with a glob is
    a larger blast radius (a typo over-trusts), whereas broad denial is
    safety-positive (#1873).
    """
    if name in patterns:
        return True
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _org_denies(ctx: ExecTrustContext, name: str, exec_type: str) -> tuple[bool, str | None]:
    """Return ``(denied, layer)`` for the org DENY ceiling (rule 1)."""
    if ctx.org_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if exec_type == EXEC_TYPE_BIN and ctx.org_bin_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if _deny_glob_match(name, ctx.org_deny):
        return True, LAYER_ORG_DENY
    if exec_type == EXEC_TYPE_BIN and _deny_glob_match(name, ctx.org_bin_deny):
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
    if _map_grants(ctx.user_deny, package_key, exec_type):
        return ExecDecision(
            False, LAYER_USER_DENY, TRUST_DENIED, _shadowed_grants(ctx, name, exec_type)
        )
    if _map_grants(ctx.project_deny, package_key, exec_type):
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


# -------------------------------------------------------------------
# Package scanning
# -------------------------------------------------------------------


def scan_package_executables(
    install_path: Path,
    package_name: str,
    package_version: str,
    *,
    is_transitive: bool = False,
    parent_name: str | None = None,
) -> ExecutableDeclaration:
    """Scan a materialised package directory for executable primitives.

    Checks for:
    - ``.apm/hooks/*.json`` and ``hooks/*.json`` -- hook definitions
      (mirrors :meth:`HookIntegrator.find_hook_files`)
    - ``bin/`` directory -- bin executables
    - MCP is declared in the package's ``apm.yml`` under
      ``dependencies.mcp``, not as files -- so we parse that instead.
    - ``.apm/extensions/<name>/extension.mjs`` -- canvas extension bundles
      (mirrors :meth:`CanvasIntegrator.find_canvas_bundles`)

    Returns an :class:`ExecutableDeclaration` (may have zero counts if
    the package declares no executables).
    """
    key = build_approval_key(package_name, package_version)

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
    apm_yml = install_path / "apm.yml"
    if apm_yml.is_file():
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml)
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
        except Exception:
            pass  # Non-fatal: if we cannot parse, treat as zero MCP

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
        hook_count=len(hook_files),
        mcp_count=mcp_count,
        bin_count=len(bin_files),
        canvas_count=len(canvas_dirs),
        hook_details=hook_details,
        mcp_details=mcp_details,
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


def prompt_executable_approval(
    declarations: list[ExecutableDeclaration],
    *,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    trust_all: bool = False,
    no_executables: bool = False,
) -> dict[str, dict[str, bool]]:
    """Run the interactive approval flow for packages with executables.

    Args:
        declarations: Executable declarations for packages that need
            approval (already filtered to only those with executables).
        allow_executables: Existing ``allowExecutables`` block from
            ``apm.yml`` (merged into result for packages already approved).
        trust_all: When True, auto-approve everything without prompting.
        no_executables: When True, deny everything without prompting.

    Returns:
        Updated ``allowExecutables`` dict ready to write back to
        ``apm.yml``.

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
            result[decl.package_key] = {t: True for t in decl.exec_types}
        return result

    # Non-interactive (CI): hard error
    if not _is_interactive():
        _rich_warning(f"{len(pending)} package(s) ship executables that are not trusted yet:")
        for decl in pending:
            provenance = "(transitive)" if decl.is_transitive else "(direct)"
            _rich_echo(f"  {decl.package_key} {provenance}: {decl.summary_line()}")
        _rich_echo("")
        _rich_info(
            "Trust the org-vetted set: apm approve --recommended  |  "
            "Trust one: apm approve <package>  |  "
            "Inspect: apm policy explain <package>",
            symbol="info",
        )
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
            result[decl.package_key] = {t: True for t in decl.exec_types}
        _rich_echo("")

    return result


def _is_fully_approved(
    allow_executables: dict[str, dict[str, bool]],
    decl: ExecutableDeclaration,
) -> bool:
    """Return True if all exec types in *decl* are approved."""
    entry = allow_executables.get(decl.package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return all(entry.get(t, False) for t in decl.exec_types)


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
            "package keys to {hooks: bool, mcp: bool, bin: bool, canvas: bool}"
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

    Reads the existing YAML, updates the ``allowExecutables`` key, and
    writes it back using the standard ``dump_yaml`` helper.

    Note: this writes only the project gate opt-in signal
    (``allowExecutables: {}``) and the CI/automated-context approvals that are
    intentionally committed.  Personal, machine-local consent lives in
    ``~/.apm/config.json`` under ``executables: {allow, deny}`` (see
    :func:`save_user_executables`); there is no standalone approvals file.
    """
    from ..utils.yaml_io import dump_yaml, load_yaml

    data = load_yaml(manifest_path)
    if not isinstance(data, dict):
        return

    if allow_executables:
        data["allowExecutables"] = allow_executables
    elif "allowExecutables" in data:
        del data["allowExecutables"]

    dump_yaml(data, manifest_path)


def materialize_exec_map(ctx: ExecTrustContext) -> dict[str, dict[str, bool]] | None:
    """Materialise the deny-wins effective allow-map from a trust context.

    Returns ``None`` when the gate is disabled; otherwise every candidate
    package key is run through :func:`resolve_exec_decision` and only ALLOWED
    ``(key, exec_type)`` pairs are emitted, each also under its version-blind
    name so the gate's exact-membership lookup matches any installed version.
    """
    if not ctx.gate_enabled:
        return None

    candidate_keys: set[str] = set()
    candidate_keys |= set(ctx.project_allow) | set(ctx.project_deny)
    candidate_keys |= set(ctx.user_allow) | set(ctx.user_deny)
    candidate_keys |= set(ctx.org_recommend) | set(ctx.org_deny) | set(ctx.org_enforce)
    candidate_keys |= set(ctx.org_bin_deny)

    result: dict[str, dict[str, bool]] = {}
    for key in candidate_keys:
        for exec_type in ALL_EXEC_TYPES:
            if not resolve_exec_decision(ctx, key, exec_type).allowed:
                continue
            result.setdefault(key, {})[exec_type] = True
            name = _strip_version(key)
            if name != key:
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


def build_effective_exec_map(
    *,
    policy: Any | None,
    project_data: dict[str, Any] | None,
) -> dict[str, dict[str, bool]] | None:
    """Materialise the deny-wins effective allow-map consumed by the install gate.

    This is the #1873 replacement for the legacy ``{**project, **user}``
    user-wins merge. See :func:`materialize_exec_map` for the emission rules.

    Returns ``None`` when the gate is disabled (backward-compatible: every
    executable deploys), mirroring :attr:`ExecTrustContext.gate_enabled`.
    """
    ctx = build_exec_trust_context(policy=policy, project_data=project_data)
    return materialize_exec_map(ctx)


def effective_allow_executables(
    project_allow_executables: dict[str, dict[str, bool]] | None,
) -> dict[str, dict[str, bool]] | None:
    """Return the effective allow-map for an install run (deny-wins).

    Back-compat shim around :func:`build_effective_exec_map`: the historical
    ``{**project, **user}`` user-wins merge is replaced by the #1873 deny-wins
    precedence. Callers that only have the legacy ``allowExecutables`` block
    (no org policy) reach the resolver through here; the install template uses
    :func:`build_effective_exec_map` directly so the org-deny ceiling applies.

    Returns ``None`` when the gate is disabled (no block), preserving the
    backward-compatible "deploy everything" behaviour.
    """
    if isinstance(project_allow_executables, dict):
        data: dict[str, Any] = {"allowExecutables": project_allow_executables}
    else:
        # ``allow_executables`` is ``dict | None`` by contract; any other shape
        # (an absent/unparsed in-memory signal) means "no project layer".
        data = {}
    return build_effective_exec_map(policy=None, project_data=data)


def filter_mcp_by_allow_executables(
    mcp_deps: list,
    project_allow_execs: dict | None,
    logger: Any,
) -> list:
    """Filter MCP deps not approved in allowExecutables. Returns filtered list."""
    if project_allow_execs is None or not mcp_deps:
        return mcp_deps
    _allow_execs = effective_allow_executables(project_allow_execs)
    if _allow_execs is None:
        return mcp_deps
    _filtered = []
    for _dep in mcp_deps:
        _slug = _dep.name
        if _slug and not is_package_approved(_allow_execs, _slug, EXEC_TYPE_MCP):
            logger.verbose_detail(
                f"Skipping MCP server from '{_slug}': executables not trusted yet. "
                f"Run 'apm approve {_slug}' to trust it."
            )
        else:
            _filtered.append(_dep)
    if len(_filtered) < len(mcp_deps):
        logger.warning(
            f"Filtered {len(mcp_deps) - len(_filtered)} MCP server(s) whose "
            "executables are not trusted yet."
        )
    return _filtered


def read_bundle_allow_executables(apm_yml_path: Path, logger: Any) -> dict | None:
    """Read allowExecutables from apm.yml for bundle install. Fail-closed on error."""
    try:
        from ..utils.yaml_io import load_yaml  # local import avoids circular at module init

        if not apm_yml_path.is_file():
            return None
        data = load_yaml(apm_yml_path)
        if isinstance(data, dict):
            return parse_allow_executables(data)
        return None
    except Exception as exc:
        logger.warning(
            f"Could not read allowExecutables from apm.yml: {exc}. "
            "Treating as fully enforced with no approvals.",
            symbol="warning",
        )
        return {}


# -------------------------------------------------------------------
# Unified vocabulary layer (issue #1873): one noun ``executables``
# -------------------------------------------------------------------
#
# Project apm.yml: ``executables: {allow, deny}`` (the deprecated
# ``allowExecutables`` block remains a read alias for one minor cycle).
# Personal consent: ``~/.apm/config.json`` under ``executables:{allow,deny}``
# (lowest authority). The standalone ``~/.apm/approvals.yml`` is migrated on
# first read and DELETED -- net-new control-surface files = 0.


def _parse_grant_block(
    raw: Any,
    *,
    where: str,
) -> dict[str, dict[str, bool]]:
    """Validate and normalise a ``{package_key: {exec_type: bool}}`` map."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} must be a mapping of package keys to "
            "{hooks: bool, mcp: bool, bin: bool, canvas: bool}"
        )
    result: dict[str, dict[str, bool]] = {}
    for pkg_key, entry in raw.items():
        if not isinstance(pkg_key, str):
            raise ValueError(f"{where} key must be a string, got {type(pkg_key).__name__}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}[{pkg_key!r}] must be a mapping of exec types to "
                f"booleans, got {type(entry).__name__}"
            )
        parsed: dict[str, bool] = {}
        for exec_type, value in entry.items():
            exec_type_str = str(exec_type)
            if exec_type_str not in ALL_EXEC_TYPES:
                raise ValueError(
                    f"{where}[{pkg_key!r}]: unknown exec type {exec_type_str!r} "
                    f"(valid: {', '.join(ALL_EXEC_TYPES)})"
                )
            if not isinstance(value, bool):
                raise ValueError(
                    f"{where}[{pkg_key!r}][{exec_type_str!r}] must be a boolean, "
                    f"got {type(value).__name__}"
                )
            parsed[exec_type_str] = value
        result[pkg_key] = parsed
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
        allow.update(_parse_grant_block(block.get("allow"), where="executables.allow"))
        deny = _parse_grant_block(block.get("deny"), where="executables.deny")

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
    msg = (
        "'allowExecutables' in apm.yml is deprecated; it now maps to "
        "'executables.allow'. It will migrate automatically on your next "
        "'apm approve'/'apm deny'."
    )
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
    """Return the path to the deprecated ``~/.apm/approvals.yml`` store.

    Read-only: the file is migrated into ``~/.apm/config.json`` on first read
    and deleted. There is no writer for this path anymore (#1873).
    """
    return Path.home() / ".apm" / "approvals.yml"


def _migrate_legacy_approvals(allow: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Fold a legacy ``approvals.yml`` into *allow* and delete the file.

    The legacy file stored a bare ``{package_key: {exec_type: bool}}`` map of
    grants. Existing config entries win over legacy entries on conflict.
    """
    import contextlib

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
    with contextlib.suppress(OSError):
        legacy.unlink()
    return allow


def load_user_executables() -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
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

    migrated = _migrate_legacy_approvals(allow)
    if migrated != allow or (allow and "executables" not in cfg):
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
) -> ExecTrustContext:
    """Assemble an :class:`ExecTrustContext` from org / project / user inputs.

    Args:
        policy: The merged org :class:`~apm_cli.policy.schema.ApmPolicy`
            (or ``None`` when no policy applies).
        project_data: Raw project ``apm.yml`` data (or ``None``).

    The gate is enabled when ANY layer opts in: the project declares an
    ``executables``/``allowExecutables`` block (even empty), or the org policy
    carries a non-empty ``executables`` block, or a legacy ``bin_deploy`` deny.
    """
    data = project_data or {}
    project_allow, project_deny, _alias = parse_project_executables(data)
    user_allow, user_deny = load_user_executables()

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
            org_deny = frozenset(getattr(execs, "deny", ()) or ())
            org_require = frozenset(getattr(execs, "require", ()) or ())
            org_recommend = frozenset(getattr(execs, "recommend", ()) or ())
            org_enforce = frozenset(getattr(execs, "enforce", ()) or ())
            org_signal = bool(
                org_deny_all or org_deny or org_require or org_recommend or org_enforce
            )
        bin_deploy = getattr(policy, "bin_deploy", None)
        if bin_deploy is not None:
            org_bin_deny_all = bool(getattr(bin_deploy, "deny_all", False))
            org_bin_deny = frozenset(getattr(bin_deploy, "deny", ()) or ())
            org_signal = org_signal or org_bin_deny_all or bool(org_bin_deny)

    gate_enabled = project_executables_gate_enabled(data) or org_signal

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
    )


def load_project_executables(
    manifest_path: Path,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]], bool]:
    """Read the project ``executables`` block (and alias) from ``apm.yml``."""
    from ..utils.yaml_io import load_yaml

    if not manifest_path.is_file():
        return {}, {}, False
    data = load_yaml(manifest_path)
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
    from ..utils.yaml_io import dump_yaml, load_yaml

    data = load_yaml(manifest_path)
    if not isinstance(data, dict):
        return

    had_alias = data.pop("allowExecutables", None) is not None
    block: dict[str, Any] = {}
    if allow:
        block["allow"] = allow
    if deny:
        block["deny"] = deny

    if block or had_alias or "executables" in data:
        data["executables"] = block
    dump_yaml(data, manifest_path)
