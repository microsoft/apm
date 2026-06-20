"""Target-profile dataclasses for multi-tool integration.

``PrimitiveMapping`` and ``TargetProfile`` describe where each APM primitive
type is deployed in a target tool. They are extracted from ``targets`` so the
data registry (``KNOWN_TARGETS``) and the resolver functions live in a focused
module; ``targets`` re-exports all three public names, so the original import
path ``apm_cli.integration.targets`` is preserved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

RULE_FORMATS: frozenset[str] = frozenset(
    {"cursor_rules", "claude_rules", "windsurf_rules", "kiro_steering", "antigravity_rules"}
)
"""Canonical set of format-transforming rule ``format_id``s.

Single home for "which instruction formats transform their source on
deploy".  A mapping with one of these ``format_id``s MUST set
``output_compare=True`` (enforced by :meth:`PrimitiveMapping.__post_init__`),
and :meth:`InstructionIntegrator._render_instruction` dispatches on this same
set.  Adding a new rule format means: add it here, set ``output_compare=True``
on the mapping, and add a ``_convert_to_*`` branch in ``_render_instruction``.
"""


@dataclass(frozen=True)
class PrimitiveMapping:
    """Where a single primitive type is deployed in a target tool."""

    subdir: str
    """Subdirectory under the target root (e.g. ``"rules"``, ``"agents"``)."""

    extension: str
    """File extension or suffix for deployed files
    (e.g. ``".mdc"``, ``".agent.md"``)."""

    format_id: str
    """Opaque tag used by integrators to select the right
    content transformer (e.g. ``"cursor_rules"``)."""

    deploy_root: str | None = None
    """Override *root_dir* for this primitive only.

    When set, integrators use ``deploy_root`` instead of
    ``target.root_dir`` to compute the deploy directory.
    For example, Codex skills deploy to ``.agents/`` (cross-tool
    directory) rather than ``.codex/``.  Default ``None`` preserves
    existing behavior for all other targets.
    """

    output_compare: bool = False
    """Whether this primitive's deployed file is a format-transform of its
    source, so the integrator must adopt/collision-check against the
    rendered *output* rather than the source bytes.

    This is the single source of truth for the rule-dir formats
    (``cursor_rules``, ``claude_rules``, ``windsurf_rules``, ``kiro_steering``).  When ``True``:

    * The deployed file is never byte-identical to its source, so a
      source-based adopt always misses (apm#1662).  The integrator instead
      compares against the rendered output and (re)writes when stale.
    * The target is APM-owned per-file (``target_name`` derives 1:1 from a
      source instruction), so ``managed_files`` is NOT consulted -- any
      existing file at the target path is APM's, not user-authored.
    * The deployed filename is renamed from ``<x>.instructions.md`` to
      ``<x>{extension}``.

    Adding a future format-transformed rule type requires two coordinated
    edits: set ``output_compare=True`` here (add the ``format_id`` to
    ``RULE_FORMATS``) *and* add the matching ``_convert_to_*`` branch to
    :meth:`InstructionIntegrator._render_instruction`, which dispatches on the
    ``format_id`` to perform the transform.
    """

    def __post_init__(self) -> None:
        """Keep ``output_compare`` and :data:`RULE_FORMATS` in lockstep.

        A rule ``format_id`` that transforms its source MUST compare against
        the rendered output; otherwise the integrator would fall through to a
        verbatim copy and silently deploy untransformed content (apm#1662).
        The converse is also enforced so the canonical set stays the one home
        for "which formats transform".
        """
        is_rule = self.format_id in RULE_FORMATS
        if is_rule and not self.output_compare:
            raise ValueError(
                f"PrimitiveMapping(format_id={self.format_id!r}) is a rule "
                f"format ({sorted(RULE_FORMATS)}) and must set "
                "output_compare=True; otherwise its source is deployed "
                "untransformed."
            )
        if self.output_compare and not is_rule:
            raise ValueError(
                f"PrimitiveMapping(format_id={self.format_id!r}) sets "
                "output_compare=True but is not a known rule format "
                f"({sorted(RULE_FORMATS)}); add it to RULE_FORMATS and a "
                "_render_instruction branch, or unset output_compare."
            )


@dataclass(frozen=True)
class TargetProfile:
    """Capabilities and layout of a single target tool."""

    name: str
    """Short unique identifier (``"copilot"``, ``"claude"``, ``"cursor"``)."""

    root_dir: str
    """Top-level directory in the workspace (e.g. ``".github"``)."""

    primitives: dict[str, PrimitiveMapping]
    """Mapping from APM primitive name -> deployment spec.

    Only primitives listed here are deployed to this target.
    """

    auto_create: bool = True
    """Create *root_dir* if it does not exist (used during fallback or
    explicit ``--target`` selection)."""

    detect_by_dir: bool = True
    """If ``True``, only deploy when *root_dir* already exists."""

    # -- user-scope metadata --------------------------------------------------

    user_supported: bool | str = False
    """Whether this target supports user-scope (``~/``) deployment.

    * ``True``  -- fully supported (all primitives work at user scope).
    * ``"partial"`` -- some primitives work, others do not.
    * ``False`` -- not supported at user scope.
    """

    user_root_dir: str | None = None
    """Override for *root_dir* at user scope.

    When ``None`` the normal *root_dir* is used at both project and user
    scope.  Set this when the tool reads from a different directory at
    user level (e.g. Copilot CLI uses ``~/.copilot/`` instead of
    ``~/.github/``).
    """

    unsupported_user_primitives: tuple[str, ...] = ()
    """Primitives that are **not** available at user scope even when the
    target itself is partially supported."""

    user_primitive_overrides: dict[str, PrimitiveMapping] | None = None
    """Primitive mapping overrides applied at user scope only.

    When set, these entries replace the corresponding entries in
    ``primitives`` after ``unsupported_user_primitives`` filtering in
    ``for_scope(user_scope=True)``.

    Use this when a primitive must be deployed to a *different* location
    or via a *different* transform at user scope.  The canonical example
    is the Copilot target: at project scope each ``*.instructions.md``
    file deploys individually to ``.github/instructions/``; at user scope
    they are all concatenated into the single file that Copilot CLI reads
    (``~/.copilot/copilot-instructions.md``).
    """

    user_root_resolver: Callable[[], Path | None] | None = None
    """Optional callable that resolves the deploy root at runtime.

    When set, ``for_scope(user_scope=True)`` calls this resolver instead of
    using a static ``user_root_dir``.  If the resolver returns ``None``
    the target is unavailable in the current environment (same semantics
    as ``user_supported=False``).

    The callable must be hashable by reference (plain function or
    staticmethod) so ``frozen=True`` is preserved.
    """

    resolved_deploy_root: Path | None = None
    """Absolute deploy root populated by ``for_scope()`` when
    ``user_root_resolver`` returns a concrete ``Path``.

    Downstream code uses ``deploy_path()`` to route filesystem I/O
    through this root instead of ``project_root / root_dir``.
    """

    requires_flag: str | None = None
    """When set, the target is only returned by ``active_targets`` /
    ``active_targets_user_scope`` / ``resolve_targets`` when the named
    experimental flag is enabled.  The target entry is always visible
    in ``KNOWN_TARGETS`` for tooling introspection.
    """

    scope_invariant_resolver: bool = False
    """When True, ``user_root_resolver`` runs in BOTH project and user
    scope (the resolved deploy root does not depend on install intent).

    Set this for targets whose deploy root is a user-machine resource
    that exists regardless of who triggered the install -- e.g.
    ``copilot-app`` (the GitHub Copilot desktop App's SQLite DB at
    ``~/.copilot/data.db`` is the same path whether a team-shared
    workflow comes in via project ``apm.yml`` or user-scope ``--global``).

    Contrast with cowork, where the OneDrive deploy root only makes
    sense at user scope; project-scope cowork is intentionally rejected.
    """

    generated_files: tuple[str, ...] = ()
    """Additional generated files associated with this target.

    These are compile-time outputs that live at the target root but are not
    deployed via primitive integrators, e.g. Copilot's root
    ``copilot-instructions.md`` file.
    """

    # -- subsystem-specific metadata (single source of truth) -----------------
    #
    # The four fields below centralize per-target knowledge that previously
    # lived in scattered module-local dicts and ``if/elif`` chains
    # (see ``bundle/lockfile_enrichment.py``, ``core/conflict_detector.py``,
    # ``commands/compile/cli.py``, ``install/services.py``).  Adding a new
    # target now requires only a single ``KNOWN_TARGETS`` entry.

    pack_prefixes: tuple[str, ...] = ()
    """Path prefixes that identify this target's deployed files when packing.

    When empty, ``bundle.lockfile_enrichment`` derives ``(f"{root_dir}/",)``
    from :attr:`root_dir`.  Override only when the target deploys to multiple
    top-level directories (e.g. Codex deploys both ``.codex/`` and
    ``.agents/``).
    """

    compile_family: str | None = None
    """Compiler family this target belongs to for ``apm compile`` routing.

    Recognised values:

    * ``"vscode"`` -- emits ``.github/copilot-instructions.md`` *and* AGENTS.md.
    * ``"claude"`` -- emits ``CLAUDE.md`` and ``.claude/rules/`` files.
    * ``"gemini"`` -- emits ``GEMINI.md``.
    * ``"agents"`` -- emits AGENTS.md only (cursor, opencode, codex, windsurf).
    * ``None`` -- target has no compile output (agent-skills, copilot-cowork).

    Used by :func:`apm_cli.commands.compile.cli._resolve_compile_target` to
    derive multi-target routing from the registry instead of hard-coded sets.
    """

    hooks_config_display: str | None = None
    """Human-readable path shown in the install log for hooks integration.

    e.g. ``".claude/settings.json"`` for Claude (hooks merge into a settings
    file rather than landing in their own subdir).  When ``None``, the
    install log falls back to the generic ``"{root}/{subdir}/"`` formula.
    """

    @property
    def prefix(self) -> str:
        """Return the path prefix for this target (e.g. ``".github/"``).

        Used by ``validate_deploy_path`` and ``partition_managed_files``.
        """
        return f"{self.root_dir}/"

    @property
    def effective_pack_prefixes(self) -> tuple[str, ...]:
        """Return the path prefixes used by pack-time file filtering.

        Falls back to ``(self.prefix,)`` when :attr:`pack_prefixes` is empty,
        so most targets need not override the field explicitly.
        """
        return self.pack_prefixes if self.pack_prefixes else (self.prefix,)

    def supports(self, primitive: str) -> bool:
        """Return ``True`` if this target accepts *primitive*."""
        return primitive in self.primitives

    def effective_root(self, user_scope: bool = False) -> str:
        """Return the root directory for the given scope.

        At user scope, returns *user_root_dir* when set, otherwise
        falls back to the standard *root_dir*.
        """
        if user_scope and self.user_root_dir:
            return self.user_root_dir
        return self.root_dir

    def supports_at_user_scope(self, primitive: str) -> bool:
        """Return ``True`` if *primitive* can be deployed at user scope."""
        if not self.user_supported:
            return False
        if primitive in self.unsupported_user_primitives:
            return False
        return primitive in self.primitives

    def deploy_path(self, project_root: Path, *parts: str) -> Path:
        """Return the filesystem path for deployment.

        When ``resolved_deploy_root`` is set (dynamic-root targets like
        cowork), the path is rooted there.  Otherwise falls back to the
        standard ``project_root / root_dir`` pattern.

        Args:
            project_root: Workspace or home directory root.
            *parts: Additional path segments (e.g. ``"skills"``, ``"my-skill"``).
        """
        if self.resolved_deploy_root is not None:
            return (
                self.resolved_deploy_root.joinpath(*parts) if parts else self.resolved_deploy_root
            )
        base = project_root / self.root_dir
        return base.joinpath(*parts) if parts else base

    def for_scope(self, user_scope: bool = False) -> TargetProfile | None:
        """Return a scope-resolved copy of this profile.

        When *user_scope* is ``False``, returns ``self`` unchanged.

        When *user_scope* is ``True``:
        - If ``user_root_resolver`` is set, calls it.  Returns ``None``
          when the resolver returns ``None`` (target unavailable).
          Otherwise returns a copy with ``resolved_deploy_root`` set and
          primitives filtered for user scope.
        - Returns ``None`` if this target does not support user scope.
        - Otherwise returns a frozen copy with ``root_dir`` set to
          ``user_root_dir`` (or left unchanged when ``user_root_dir``
          is ``None``) and ``primitives`` filtered to exclude entries
          listed in ``unsupported_user_primitives``.

        This is the **single place** where scope resolution happens.
        All downstream code reads ``target.root_dir`` directly.
        """
        if not user_scope:
            # Most targets have no project-scope resolver work to do.
            # The scope_invariant_resolver opt-in lets a target whose
            # deploy root is a user-machine resource (e.g. copilot-app's
            # ~/.copilot/data.db) populate resolved_deploy_root even when
            # the install intent is project-scope. Downstream lockfile
            # enrichment then routes via the dynamic-root URI path.
            if self.scope_invariant_resolver and self.user_root_resolver is not None:
                resolved_root = self.user_root_resolver()
                if resolved_root is None:
                    return None
                from dataclasses import replace

                return replace(self, resolved_deploy_root=resolved_root)
            return self

        from dataclasses import replace

        # --- dynamic-root resolver path (cowork) ---
        if self.user_root_resolver is not None:
            resolved_root = self.user_root_resolver()
            if resolved_root is None:
                return None
            if self.unsupported_user_primitives:
                filtered = {
                    k: v
                    for k, v in self.primitives.items()
                    if k not in self.unsupported_user_primitives
                }
            else:
                filtered = self.primitives
            if self.user_primitive_overrides:
                merged = dict(filtered)
                merged.update(self.user_primitive_overrides)
                filtered = merged
            return replace(
                self,
                primitives=filtered,
                resolved_deploy_root=resolved_root,
            )

        if not self.user_supported:
            return None

        new_root = self.user_root_dir or self.root_dir

        # Claude Code honors CLAUDE_CONFIG_DIR (default ~/.claude) and Hermes
        # honors HERMES_HOME (default ~/.hermes); mirror that at user scope so
        # `apm install -g` lands where the tool reads.
        if self.name in ("claude", "hermes"):
            import os
            from pathlib import Path

            env_var = "CLAUDE_CONFIG_DIR" if self.name == "claude" else "HERMES_HOME"
            env = os.environ.get(env_var, "").strip()
            if env:
                # ``resolve`` collapses ``..`` so traversal segments cannot
                # leak into ``root_dir`` and escape ``project_root / root_dir``.
                abs_path = Path(env).expanduser().resolve(strict=False)
                home = Path.home().resolve(strict=False)
                try:
                    # Keep ``root_dir`` home-relative so cleanup prefix matching holds.
                    new_root = abs_path.relative_to(home).as_posix()
                except ValueError:
                    # Fallback: when CLAUDE_CONFIG_DIR points outside $HOME we
                    # store an absolute path. ``pathlib.Path / <absolute>`` is
                    # ``<absolute>`` so deploy + cleanup write to the right
                    # place. Caveat: the lockfile path translator
                    # (``install/services._deployed_path_entry``) calls
                    # ``relative_to(project_root)`` and raises ``RuntimeError``
                    # for out-of-tree paths that are not dynamic-root targets.
                    # Today this is unreachable because user-scope CLAUDE
                    # installs do not flow through that translator, but any
                    # future refactor that lockfiles user-scope deploys must
                    # treat absolute ``root_dir`` as a dynamic-root case.
                    new_root = str(abs_path)

        if self.unsupported_user_primitives:
            filtered = {
                k: v
                for k, v in self.primitives.items()
                if k not in self.unsupported_user_primitives
            }
        else:
            filtered = self.primitives

        if self.user_primitive_overrides:
            merged = dict(filtered)
            merged.update(self.user_primitive_overrides)
            filtered = merged

        return replace(self, root_dir=new_root, primitives=filtered)
