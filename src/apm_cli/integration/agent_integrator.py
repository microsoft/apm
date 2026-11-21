"""Agent integration functionality for APM packages.

Note: SKILL.md files are NOT transformed to .agent.md files. Skills are handled
separately by SkillIntegrator and installed to .github/skills/ as native skills.
See skill-strategy.md for the full architectural rationale (T5).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from apm_cli.core.remediation import (
    CODEX_AGENT_CLAUDE_MODEL,
    CODEX_AGENT_FIELD_NOT_STRING,
    CODEX_AGENT_FRONTMATTER_INVALID,
    CODEX_AGENT_FRONTMATTER_NOT_MAPPING,
)
from apm_cli.install.primitive_classification import (
    AgentSourceClassification,
    PrimitiveKind,
    classify_agent_source_file,
)
from apm_cli.integration import write_seam
from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.opencode_frontmatter import validate_opencode_frontmatter
from apm_cli.integration.write_seam import write_text_lf
from apm_cli.primitives.models import resolve_user_invocable
from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_yaml_str, yaml_to_str

if TYPE_CHECKING:
    from apm_cli.integration.cleanup import DeploymentEvidence
    from apm_cli.integration.skill_adoption import ResolveLinks
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector

# Kiro agent ``tools`` vocabulary (IDE 1.0 / CLI V3).
# Sources (accessed 2026-11-19):
#   https://kiro.dev/docs/custom-agents/configuration-reference/#tools-field
#   https://kiro.dev/docs/tools/#configuring-tool-access
# Fail closed: a value outside this vocabulary blocks deployment of that agent
# only; sibling agents still deploy (req-tg-009).
KIRO_AGENT_TOOL_TAGS: frozenset[str] = frozenset({"read", "write", "shell", "web", "subagent"})
KIRO_AGENT_TOOL_SPECIAL_VALUES: frozenset[str] = frozenset({"@builtin", "@mcp", "*"})
KIRO_AGENT_TOOL_IDS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_directory",
        "file_search",
        "grep_search",
        "code",
        "fs_write",
        "fs_append",
        "str_replace",
        "delete_file",
        "execute_bash",
        "execute_pwsh",
        "control_bash_process",
        "control_pwsh_process",
        "get_process_output",
        "list_processes",
        "remote_web_search",
        "web_fetch",
        "invoke_sub_agent",
        "disclose_context",
        "introspect",
        "knowledge",
        "createHook",
        "kiro_powers",
    }
)
# Accepted by earlier APM releases; kept so agents that deploy today keep deploying.
KIRO_AGENT_TOOL_COMPAT_VALUES: frozenset[str] = frozenset({"context", "todo_list"})
KIRO_AGENT_ALLOWED_TOOLS: frozenset[str] = (
    KIRO_AGENT_TOOL_TAGS
    | KIRO_AGENT_TOOL_SPECIAL_VALUES
    | KIRO_AGENT_TOOL_IDS
    | KIRO_AGENT_TOOL_COMPAT_VALUES
)
# ``@server`` or ``@server/tool`` (``@builtin`` and ``@mcp`` take no tool
# suffix; tool names follow MCP SEP-986), or ``subagent/<agent>`` to limit
# which sub-agents an agent may start.
_KIRO_SELECTOR_RE = re.compile(
    r"@(?!(?:builtin|mcp)/)[A-Za-z0-9][A-Za-z0-9_.-]{0,127}(?:/[A-Za-z0-9_.-]{1,128})?"
    r"|subagent/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
)
_KIRO_TOOLS_HINT = (
    "Approved Kiro tool selectors: category tags ("
    + ", ".join(sorted(KIRO_AGENT_TOOL_TAGS))
    + "); built-in tool IDs ("
    + ", ".join(sorted(KIRO_AGENT_TOOL_IDS))
    + "); compatibility values ("
    + ", ".join(sorted(KIRO_AGENT_TOOL_COMPAT_VALUES))
    + "); @server, @server/tool, or subagent/<agent>; "
    + ", ".join(sorted(KIRO_AGENT_TOOL_SPECIAL_VALUES))
    + "."
)


def is_kiro_agent_tool(value: str) -> bool:
    """Return True when *value* is a valid Kiro agent ``tools`` selector."""
    return value in KIRO_AGENT_ALLOWED_TOOLS or bool(_KIRO_SELECTOR_RE.fullmatch(value))


# Outcomes of :meth:`AgentIntegrator._deploy_rendered_agent`.
_ADOPTED = "adopted"
_SKIPPED = "skipped"
_WRITTEN = "written"

# Codex rejects an agent role whose name, description or developer_instructions
# is blank (codex-rs agent-roles agent_role_config.rs).
_CODEX_REQUIRED_FIELDS = ("name", "description", "developer_instructions")


class CodexAgentFieldError(ValueError):
    """A rendered Codex agent field that Codex's agent-role parser rejects."""


def _derived_codex_description(name: object, body: str) -> object:
    """Return the agent's leading Markdown heading, else *name*, as its description."""
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    heading = first.lstrip("#").strip() if first.startswith("#") else ""
    return heading or name


class AgentIntegrator(BaseIntegrator):
    """Handles integration of APM package agents into .github/agents/, .claude/agents/, .cursor/agents/, and .kiro/agents/."""

    # Deploys via write_text_lf -> compare adopt candidates in LF mode.
    _LF_NORMALIZED_DEPLOY = True

    @staticmethod
    def _warn_agent_classification(
        classification: AgentSourceClassification,
        diagnostics: DiagnosticCollector | None,
        package_name: str,
    ) -> None:
        if classification.warning and diagnostics is not None:
            diagnostics.warn(
                message=classification.warning,
                package=printable_ascii_text(package_name),
            )

    def find_agent_files(
        self,
        package_path: Path,
        source_plan=None,
        diagnostics=None,
        package_name: str = "",
    ) -> list[Path]:
        """Find all agent files in a package.

        Searches in:
        - Package root directory (*.agent.md files)
        - .apm/agents/ subdirectory (recursive): *.agent.md and plain *.md files

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to agent files
        """
        files: list[Path] = []
        # Flat search in package root. The extension is only the final
        # fallback; declarations inside the file still route through the owner.
        for candidate in self.find_files_by_glob(package_path, "*.agent.md"):
            classification = classify_agent_source_file(candidate, package_path)
            self._warn_agent_classification(classification, diagnostics, package_name)
            if classification.kind is PrimitiveKind.AGENT:
                files.append(candidate)
        # Recursive search in .apm/agents/; the classification owner decides
        # whether each file is an agent before target layout derives a name.
        apm_agents = package_path / ".apm" / "agents"
        if apm_agents.exists():
            candidates = [f for f in self.find_files_by_glob(apm_agents, "**/*") if f.is_file()]
            authorized = set(self.filter_authorized_files(candidates, source_plan))
            for candidate in candidates:
                classification = classify_agent_source_file(candidate, apm_agents)
                self._warn_agent_classification(classification, diagnostics, package_name)
                if (
                    classification.kind is PrimitiveKind.AGENT
                    and candidate in authorized
                    and candidate not in files
                ):
                    files.append(candidate)
        return self.filter_authorized_files(files, source_plan)

    # NOTE: find_skill_file(), integrate_skill(), and _generate_skill_agent_content()
    # have been REMOVED as part of T5 (skill-strategy.md).
    #
    # Skills are NOT transformed to .agent.md files. Instead:
    # - Skills go directly to .github/skills/ via SkillIntegrator
    # - This preserves the native skill format and avoids semantic confusion
    # - See skill-strategy.md for the full architectural rationale

    # ------------------------------------------------------------------
    # Target-driven API (data-driven dispatch)
    # ------------------------------------------------------------------

    def get_target_filename_for_target(
        self,
        source_file: Path,
        package_name: str,
        target: TargetProfile,
    ) -> str:
        """Generate target filename using the extension from *target*'s agents mapping."""
        mapping = target.primitives.get("agents")
        ext = mapping.extension if mapping else ".agent.md"
        stem = source_file.name[:-9] if source_file.name.endswith(".agent.md") else source_file.stem
        return f"{stem}{ext}"

    def integrate_agents_for_target(
        self,
        target: TargetProfile,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        scope=None,
        source_plan=None,
    ) -> IntegrationResult:
        """Integrate agents from a package for a single *target*.

        Each call deploys to exactly one target.  The dispatch loop in
        ``install.py`` calls this once per active target that supports
        the ``agents`` primitive.
        """
        mapping = target.primitives.get("agents")
        if not mapping:
            return IntegrationResult(0, 0, 0, [])

        effective_root = mapping.deploy_root or target.root_dir
        target_root = project_root / effective_root
        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            return IntegrationResult(0, 0, 0, [])

        self.init_link_resolver(package_info, project_root)
        agent_files = self.find_agent_files(
            package_info.install_path,
            source_plan,
            diagnostics,
            package_info.package.name,
        )
        if not agent_files:
            return IntegrationResult(0, 0, 0, [])

        agents_dir = target_root / mapping.subdir
        # Lazy mkdir: only create the dir when we actually need to write.
        # This avoids creating .kiro/agents/ when every agent is invalid-tools.
        agents_dir_created = False

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0

        for source_file in agent_files:
            # kiro_agent uses relative path from .apm/agents/ for identity.
            if mapping.format_id == "kiro_agent":
                target_relpath = self._kiro_agent_relpath(source_file, package_info.install_path)
            else:
                target_relpath = self.get_target_filename_for_target(
                    source_file,
                    package_info.package.name,
                    target,
                )
            target_path = agents_dir / target_relpath
            # Defense-in-depth: assert containment under agents_dir so a
            # regression cannot smuggle a traversal sequence past the adopt
            # branch (which fires *before* check_collision and would otherwise
            # blindly trust the computed path). Mirrors the guard already in
            # command_integrator and instruction_integrator.
            try:
                ensure_path_within(target_path, agents_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected agent target path: {exc}",
                        package=package_info.package.name,
                    )
                files_skipped += 1
                continue

            links_resolved = 0
            if mapping.format_id == "kiro_agent":
                # req-tg-009: Preflight render+validate MUST run before any
                # content-identity adoption fast-path or filesystem mutation.
                # If validation fails, skip without writing or creating dirs.
                rendered, ok = self._preflight_render_kiro_agent(
                    source_file,
                    diagnostics=diagnostics,
                    package_name=package_info.package.name,
                )
                if not ok:
                    files_skipped += 1
                    continue
            else:
                # Non-kiro path: eager mkdir (existing behavior preserved).
                if not agents_dir_created:
                    write_seam.mkdir(agents_dir, parents=True, exist_ok=True)
                    agents_dir_created = True
                if mapping.format_id == "codex_agent":
                    rendered, links_resolved = self._render_codex_agent(
                        source_file,
                        target_path,
                        diagnostics=diagnostics,
                        package_name=package_info.package.name,
                        resolve_links=self.resolve_links,
                    )
                else:
                    rendered, links_resolved = self._render_copy_agent(source_file, target_path)

            outcome = self._deploy_rendered_agent(
                target_path,
                source_file,
                portable_relpath(target_path, project_root),
                rendered,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
            )
            if outcome == _ADOPTED:
                files_adopted += 1
                continue
            if outcome == _SKIPPED:
                files_skipped += 1
                continue
            if mapping.format_id == "opencode_agent":
                self._warn_opencode_frontmatter(source_file, diagnostics, package_info.package.name)
            total_links_resolved += links_resolved
            files_integrated += 1

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
            files_adopted=files_adopted,
        )

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
        evidence: DeploymentEvidence | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed agent files for a single *target*."""
        mapping = target.primitives.get("agents")
        if not mapping:
            return {"files_removed": 0, "errors": 0}
        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        legacy_dir = project_root / effective_root / mapping.subdir
        # Copilot uses .agent.md suffix; others use plain .md
        legacy_pattern = "*-apm.agent.md" if mapping.extension == ".agent.md" else "*-apm.md"
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix=prefix,
            legacy_glob_dir=legacy_dir,
            legacy_glob_pattern=legacy_pattern,
            targets=[target],
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Legacy per-target API (DEPRECATED)
    #
    # These methods hardcode a specific target and bypass scope
    # resolution.  Use the target-driven API (*_for_target) with
    # profiles from resolve_targets() instead.
    #
    # Kept for backward compatibility with external consumers.
    # Do NOT add new per-target methods here.
    # ------------------------------------------------------------------

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def get_target_filename(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for copilot (always .agent.md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["copilot"],
        )

    def _render_copy_agent(self, source: Path, target: Path) -> tuple[str, int]:
        """Render a verbatim-copy agent: the source text with context links resolved.

        Returns:
            tuple[str, int]: Rendered text and number of links resolved.
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")
        content = source.read_text(encoding="utf-8")
        return self.resolve_links(content, source, target)

    def copy_agent(self, source: Path, target: Path) -> int:
        """Copy agent file verbatim, resolving context links.

        Args:
            source: Source file path
            target: Target file path

        Returns:
            int: Number of links resolved
        """
        content, links_resolved = self._render_copy_agent(source, target)
        write_text_lf(target, content)
        return links_resolved

    def _deploy_rendered_agent(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        rendered: str,
        *,
        managed_files: set[str] | None,
        force: bool,
        diagnostics,
        target_paths: list,
    ) -> str:
        """Adopt, skip, or write an already-rendered agent (render, then adopt).

        Every agent writer renders first (Codex TOML, Kiro filtered markdown,
        verbatim copy with rewritten links) and hands the exact string here.
        Adoption is decided only by :meth:`_check_adopt_or_skip` against that
        string, and the same string is written, so a target APM wrote earlier
        is adopted even when the lockfile does not list it (#3722).

        Returns:
            ``"adopted"``, ``"skipped"`` or ``"written"``. On adopt and write
            *target_path* is appended to *target_paths*.
        """
        skip, adopted = self._check_adopt_or_skip(
            target_path,
            source_file,
            rel_path,
            managed_files,
            force,
            diagnostics,
            target_paths,
            expected_content=rendered,
        )
        if skip:
            return _ADOPTED if adopted else _SKIPPED
        write_seam.mkdir(target_path.parent, parents=True, exist_ok=True)
        write_text_lf(target_path, rendered)
        self.note_rendered_write(target_path)
        target_paths.append(target_path)
        return _WRITTEN

    # ------------------------------------------------------------------
    # OpenCode validate-and-warn (Phase 1 of #581)
    # ------------------------------------------------------------------

    @staticmethod
    def _warn_opencode_frontmatter(
        source: Path,
        diagnostics: DiagnosticCollector | None,
        package_name: str,
    ) -> None:
        """Emit warnings for OpenCode-incompatible agent frontmatter.

        Phase 1 only: surfaces Zod-fatal shapes (tools as list/string,
        named colors outside the OpenCode theme enum) so users learn
        why OpenCode will refuse to load the agent. The file is still
        copied verbatim; Phase 2 (per-target frontmatter transformer)
        is tracked separately.
        """
        if diagnostics is None:
            return
        if source.is_symlink():
            return
        try:
            content = source.read_text(encoding="utf-8")
        except OSError:
            return
        fm_match = AgentIntegrator._FRONTMATTER_RE.match(content)
        if not fm_match:
            return
        try:
            fm = load_yaml_str(fm_match.group(1)) or {}
        except yaml.YAMLError:
            return
        if not isinstance(fm, dict):
            return
        for message in validate_opencode_frontmatter(fm, source, package_name=package_name):
            diagnostics.warn(
                message=message,
                package=printable_ascii_text(package_name),
            )

    # ------------------------------------------------------------------
    # Codex agent transformer (MD -> TOML)
    # ------------------------------------------------------------------

    _FRONTMATTER_RE = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n?",
        re.DOTALL,
    )

    # Bound the dropped-fields diagnostic to a fixed number of named keys so
    # hostile/oversized frontmatter (many keys) cannot blow up a single CLI
    # diagnostic line; the remainder is summarized by count instead of
    # enumerated.
    _MAX_DROPPED_FIELDS_SHOWN = 5
    # Bound each individually-named key to a fixed display length so a
    # single hostile/oversized key name cannot blow up a single CLI
    # diagnostic line either; combined with _MAX_DROPPED_FIELDS_SHOWN this
    # bounds the whole diagnostic's total length.
    _MAX_DROPPED_FIELD_KEY_LEN = 60

    # Native string fields Codex itself recognizes and whose declared value
    # takes precedence over any APM default. Single source of truth for both
    # the model-settings extraction loop and the dropped-fields exclusion set
    # below, so adding a third recognized field only requires one edit here.
    _CODEX_MODEL_FIELDS = ("model", "model_reasoning_effort")
    # 'user-invocable' and its 'visibility' alias are recognized-but-lossy:
    # they get their own dedicated diagnostic via _warn_user_invocable_dropped
    # (with the actionable 'cannot be guaranteed' hint), so they must be
    # excluded here to avoid a second, generic dropped-fields warning for the
    # same key.
    _CODEX_KNOWN_FIELDS = frozenset(
        {"name", "description", "tools", "user-invocable", "visibility", *_CODEX_MODEL_FIELDS}
    )

    @staticmethod
    def _display_dropped_field_key(field: object) -> str:
        """Render one dropped frontmatter key name, bounded and ASCII-safe.

        ``field`` stays ``object`` (not a narrower union) because the YAML
        frontmatter loader (``load_yaml_str``) accepts the full YAML 1.1 key
        domain, including ``datetime.date`` (bare ``2026-10-05:`` keys) and
        ``bytes`` (``!!binary`` keys) -- neither representable by a small
        str/int/float/bool/None union. ``str(field)`` below covers all of
        them uniformly.

        Sanitizes control/non-ASCII characters first, then truncates the
        *displayed* key (never the full value) so a single oversized key
        name cannot blow up the diagnostic line.
        """
        text = printable_ascii_text(str(field))
        max_len = AgentIntegrator._MAX_DROPPED_FIELD_KEY_LEN
        if len(text) > max_len:
            text = text[:max_len] + "...(truncated)"
        return text

    @staticmethod
    def _warn_codex_unverified_scope(
        diagnostics: DiagnosticCollector | None,
        source: Path,
        package_name: str,
        issue: str,
        fix: str,
    ) -> None:
        """Warn that invalid Codex frontmatter prevents scope verification."""
        if diagnostics is None:
            return
        diagnostics.warn(
            message=(
                f"Codex agent {printable_ascii_text(source.name)}: {issue}. "
                "Tool restrictions could not be verified, so the agent may inherit broader "
                f"tool access. Fix: {fix}."
            ),
            package=printable_ascii_text(package_name),
        )

    @staticmethod
    def _warn_codex_tools_dropped(
        diagnostics: DiagnosticCollector | None,
        source: Path,
        package_name: str,
    ) -> None:
        """Warn that APM cannot preserve Codex agent tool restrictions."""
        if diagnostics is None:
            return
        diagnostics.lossy_agent_compilation(
            message=(
                f"Codex agent {printable_ascii_text(source.name)}: frontmatter field 'tools' "
                "was dropped; the agent may inherit all project/session MCP servers."
            ),
            package=printable_ascii_text(package_name),
            detail=(
                "Fix: remove 'tools' if unrestricted access is intentional; "
                "otherwise do not use the generated agent with Codex."
            ),
        )

    @staticmethod
    def _warn_user_invocable_dropped(
        fm,
        source: Path,
        target_name: str,
        diagnostics: DiagnosticCollector | None,
        package_name: str,
    ) -> None:
        """Warn that a target renderer cannot carry programmatic-only status.

        No-ops when diagnostics are unavailable, the frontmatter is not a
        mapping, or the agent resolves as user-invocable (the default). The
        single interpretation authority is resolve_user_invocable(), shared
        with the parser, so the model and the integrator agree on meaning:
        the source may have set 'user-invocable: false' directly or only the
        'visibility: internal' alias.

        The emitted diagnostic is a warning (lossy compilation), not an error:
        the agent is still deployed, but this renderer cannot encode the
        source's programmatic-only intent, so that semantic cannot be
        guaranteed after the render. The APM source frontmatter remains the
        authority; operators who must keep the agent out of a user-facing
        picker should not generate/deploy it for this target, or should
        prefer a target that preserves 'user-invocable'.
        """
        if diagnostics is None:
            return
        if not isinstance(fm, dict):
            return
        if resolve_user_invocable(fm):
            return
        diagnostics.lossy_agent_compilation(
            message=(
                f"{target_name} agent {printable_ascii_text(source.name)}: user-invocability "
                "(resolved from 'user-invocable' or the 'visibility: internal' alias) cannot be "
                "represented in this target format; programmatic-only semantics cannot be "
                "guaranteed after this lossy render."
            ),
            package=printable_ascii_text(package_name),
            detail=(
                "The APM source agent remains the authority for user-invocability. Mitigation: "
                "do not generate/deploy this agent for this target if it must stay out of a "
                "user-facing agent picker, or prefer a target that preserves 'user-invocable'."
            ),
        )

    # Claude Code model aliases (and claude-* ids) are not Codex models. The
    # value is still preserved verbatim (OpenAPM req-tg-015); a shared
    # Claude/Codex source only gets a warning (issue #3477).
    _CLAUDE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku", "inherit"})

    @staticmethod
    def _is_claude_model(value: str) -> bool:
        """Whether *value* names a Claude Code model alias or id."""
        normalized = value.strip().lower()
        return normalized in AgentIntegrator._CLAUDE_MODEL_ALIASES or normalized.startswith(
            "claude-"
        )

    @staticmethod
    def _warn_codex_claude_model(
        diagnostics: DiagnosticCollector | None,
        source: Path,
        package_name: str,
    ) -> None:
        """Warn that a preserved Codex ``model`` names a Claude model.

        Names-only: the diagnostic names the agent and the field, never the
        model value or any other frontmatter value.
        """
        if diagnostics is None:
            return
        diagnostics.lossy_agent_compilation(
            message=(
                f"Codex agent {printable_ascii_text(source.name)}: frontmatter field 'model' "
                "names a Claude model alias or id; it was kept as written, but Codex does "
                "not recognize Claude models."
            ),
            package=printable_ascii_text(package_name),
            detail=f"Fix: {CODEX_AGENT_CLAUDE_MODEL.clause()}.",
        )

    @staticmethod
    def _render_codex_agent(
        source: Path,
        target: Path,
        *,
        diagnostics: DiagnosticCollector | None = None,
        package_name: str = "",
        resolve_links: ResolveLinks | None = None,
        content: str | None = None,
        strict: bool = False,
    ) -> tuple[str, int]:
        """Render an ``.agent.md`` file to Codex ``.toml`` format.

        Preserves ``name``, ``description``, and native model settings;
        uses the markdown body as ``developer_instructions``.

        *resolve_links* is the shared :meth:`BaseIntegrator.resolve_links`
        owner; it rewrites source-relative Markdown links in the body from
        *source* for the generated *target* before serialization (#2966).

        *content* is source text the caller already read and verified; when
        given, *source* is not read again, so the rendered text is exactly
        the verified snapshot.

        With *strict*, an agent that ships no description gets one derived
        from its leading Markdown heading, else its name, so Codex can load
        it (a plugin agent need not declare one).

        Raises:
            CodexAgentFieldError: with *strict*, a native field Codex requires
                is blank or not a string, so Codex could not load the agent.

        Returns:
            tuple[str, int]: Rendered TOML string and number of links resolved.
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")
        import toml as _toml

        if content is None:
            content = source.read_text(encoding="utf-8")

        name = source.stem
        if name.endswith(".agent"):
            name = name[: -len(".agent")]
        description = ""
        body = content
        model_settings: dict[str, str] = {}

        fm_match = AgentIntegrator._FRONTMATTER_RE.match(content)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
                if isinstance(fm, dict):
                    name = fm.get("name", name)
                    description = fm.get("description", description)
                    for field in AgentIntegrator._CODEX_MODEL_FIELDS:
                        if field not in fm:
                            continue
                        if isinstance(fm[field], str):
                            model_settings[field] = fm[field]
                            if field == "model" and AgentIntegrator._is_claude_model(fm[field]):
                                AgentIntegrator._warn_codex_claude_model(
                                    diagnostics, source, package_name
                                )
                        elif diagnostics is not None:
                            diagnostics.lossy_agent_compilation(
                                message=(
                                    f"Codex agent {printable_ascii_text(source.name)}: frontmatter "
                                    f"field '{field}' must be a string and was dropped."
                                ),
                                package=printable_ascii_text(package_name),
                                detail=(
                                    "Fix: "
                                    f"{CODEX_AGENT_FIELD_NOT_STRING.bind(field=field).clause()}."
                                ),
                            )
                    # Collect only the raw dropped keys here (no formatting)
                    # so hostile frontmatter with many keys does not force
                    # rendering every key just to discard most of them below.
                    dropped_field_names = [
                        field for field in fm if field not in AgentIntegrator._CODEX_KNOWN_FIELDS
                    ]
                    if dropped_field_names and diagnostics is not None:
                        shown_names = dropped_field_names[
                            : AgentIntegrator._MAX_DROPPED_FIELDS_SHOWN
                        ]
                        remaining = len(dropped_field_names) - len(shown_names)
                        shown_fields = [
                            f"'{AgentIntegrator._display_dropped_field_key(field)}'"
                            for field in shown_names
                        ]
                        fields_text = ", ".join(shown_fields)
                        if remaining > 0:
                            fields_text += f" (and {remaining} more)"
                        singular = len(dropped_field_names) == 1
                        field_label = "field" if singular else "fields"
                        dropped_verb = "was" if singular else "were"
                        field_reference = "this field" if singular else "these fields"
                        setting_reference = "its setting" if singular else "their settings"
                        diagnostics.lossy_agent_compilation(
                            message=(
                                f"Codex agent {printable_ascii_text(source.name)}: frontmatter "
                                f"{field_label} {fields_text} {dropped_verb} dropped; "
                                "this metadata is not translated by APM for Codex."
                            ),
                            package=printable_ascii_text(package_name),
                            detail=(
                                f"Fix: remove {field_reference} if unnecessary; otherwise do not "
                                f"rely on {setting_reference} in the generated Codex agent."
                            ),
                        )
                else:
                    AgentIntegrator._warn_codex_unverified_scope(
                        diagnostics,
                        source,
                        package_name,
                        "YAML frontmatter must be a mapping and was ignored",
                        CODEX_AGENT_FRONTMATTER_NOT_MAPPING.clause(),
                    )
                if isinstance(fm, dict) and "tools" in fm:
                    AgentIntegrator._warn_codex_tools_dropped(
                        diagnostics,
                        source,
                        package_name,
                    )
                AgentIntegrator._warn_user_invocable_dropped(
                    fm, source, "Codex", diagnostics, package_name
                )
            except yaml.YAMLError:
                AgentIntegrator._warn_codex_unverified_scope(
                    diagnostics,
                    source,
                    package_name,
                    "invalid YAML frontmatter was ignored",
                    CODEX_AGENT_FRONTMATTER_INVALID.clause(),
                )

        links_resolved = 0
        if resolve_links is not None:
            body, links_resolved = resolve_links(body, source, target)

        # A blank YAML ``description:`` loads as None: treat it as absent.
        if strict and (
            description is None or (isinstance(description, str) and not description.strip())
        ):
            description = _derived_codex_description(name, body)
        doc = {
            "name": name,
            "description": description,
            **model_settings,
            "developer_instructions": body.strip(),
        }
        for field in _CODEX_REQUIRED_FIELDS if strict else ():
            if not isinstance(doc[field], str) or not doc[field].strip():
                raise CodexAgentFieldError(
                    f"Codex agent {printable_ascii_text(source.name)}: '{field}' must be "
                    "a non-empty string, so Codex cannot load it"
                )
        return _toml.dumps(doc), links_resolved

    @staticmethod
    def _write_codex_agent(
        source: Path,
        target: Path,
        *,
        diagnostics: DiagnosticCollector | None = None,
        package_name: str = "",
        resolve_links: ResolveLinks | None = None,
    ) -> int:
        """Transform an ``.agent.md`` file to Codex ``.toml`` format and write it.

        Preserves ``name``, ``description``, and native model settings;
        uses the markdown body as ``developer_instructions``.

        *resolve_links* is the shared :meth:`BaseIntegrator.resolve_links`
        owner; it rewrites source-relative Markdown links in the body from
        *source* for the generated *target* before serialization (#2966).

        Returns:
            int: Number of links resolved in ``developer_instructions``.
        """
        rendered, links_resolved = AgentIntegrator._render_codex_agent(
            source,
            target,
            diagnostics=diagnostics,
            package_name=package_name,
            resolve_links=resolve_links,
        )
        write_text_lf(target, rendered)
        return links_resolved

    # ------------------------------------------------------------------
    # Kiro agent transformer (MD -> filtered MD)
    # ------------------------------------------------------------------

    @staticmethod
    def _kiro_agent_relpath(source_file: Path, package_path: Path) -> str:
        """Compute the relative target path for a Kiro agent file.

        Preserves subdirectory structure from .apm/agents/ so identity
        derives from the deployed path, not from a 'name' frontmatter
        field (Kiro CLI v3 / IDE uses relative path as identity).

        Sources under .apm/agents/ keep their relative subpath; root-level
        sources are flattened to the filename only.

        Ref: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
        """
        apm_agents_root = package_path / ".apm" / "agents"
        try:
            rel = source_file.relative_to(apm_agents_root)
        except ValueError:
            rel = Path(source_file.name)
        parts = rel.parts
        stem = parts[-1]
        if stem.endswith(".agent.md"):
            stem = stem[: -len(".agent.md")] + ".md"
        elif not stem.endswith(".md"):
            stem = stem + ".md"
        if len(parts) > 1:
            return str(Path(*parts[:-1]) / stem)
        return stem

    @staticmethod
    def _preflight_render_kiro_agent(
        source: Path,
        *,
        diagnostics=None,
        package_name: str = "",
    ) -> tuple[str | None, bool]:
        """Validate and render a Kiro agent file; return (rendered, ok).

        This is the SINGLE OWNER of the Kiro render+validate decision.
        It MUST be called before any filesystem mutation (adopt check,
        directory creation, or write) so that req-tg-009 is upheld: the
        fail-closed evaluation runs prior to any content-identity fast-path.

        Returns (rendered_str, True) on success or (None, False) if the
        source declares a tool that :func:`is_kiro_agent_tool` rejects. On
        failure an actionable warning is emitted -- the caller MUST skip all
        target mutations for this agent without writing any bytes. The
        warning is non-fatal so conformant sibling agents still deploy and
        are recorded (req-tg-009).

        Ref: https://kiro.dev/docs/tools/ (accessed 2026-11-19)
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")

        content = source.read_text(encoding="utf-8")
        body = content
        out_fm: dict = {}

        fm_match = AgentIntegrator._FRONTMATTER_RE.match(content)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except yaml.YAMLError:
                fm = {}

            if isinstance(fm, dict):
                # Validate tools; fail closed on any incompatible value.
                if "tools" in fm:
                    tools_raw = fm["tools"]
                    if tools_raw is None:
                        tools_out = None
                    elif isinstance(tools_raw, list):
                        tools_strs = [str(t).strip() for t in tools_raw]
                        incompatible = {t for t in tools_strs if not is_kiro_agent_tool(t)}
                        if incompatible:
                            if diagnostics is not None:
                                names = ", ".join(sorted(incompatible))
                                diagnostics.warn(
                                    message=(
                                        f"Kiro agent {printable_ascii_text(source.name)}: "
                                        f"unsupported tool(s) {names!a} -- "
                                        "agent will not be deployed. "
                                        f"{_KIRO_TOOLS_HINT} "
                                        "Ref: https://kiro.dev/docs/tools/"
                                    ),
                                    package=printable_ascii_text(package_name),
                                )
                            return None, False
                        tools_out = tools_strs
                    elif isinstance(tools_raw, str):
                        tool = tools_raw.strip()
                        if not is_kiro_agent_tool(tool):
                            if diagnostics is not None:
                                diagnostics.warn(
                                    message=(
                                        f"Kiro agent {printable_ascii_text(source.name)}: "
                                        f"unsupported tool {tool!a} -- "
                                        "agent will not be deployed. "
                                        f"{_KIRO_TOOLS_HINT} "
                                        "Ref: https://kiro.dev/docs/tools/"
                                    ),
                                    package=printable_ascii_text(package_name),
                                )
                            return None, False
                        tools_out = [tool]
                    else:
                        if diagnostics is not None:
                            diagnostics.warn(
                                message=(
                                    f"Kiro agent {printable_ascii_text(source.name)}: "
                                    "'tools' must be a list of tool selectors -- "
                                    "agent will not be deployed. "
                                    "Fix: use a YAML list, e.g. 'tools: [read, write]'. "
                                    "Ref: https://kiro.dev/docs/custom-agents/"
                                ),
                                package=printable_ascii_text(package_name),
                            )
                        return None, False

                    if tools_out is not None:
                        out_fm["tools"] = tools_out

                # Emit approved fields in canonical order: description, model, tools.
                out_fm_ordered: dict = {}
                if "description" in fm and fm["description"] is not None:
                    out_fm_ordered["description"] = fm["description"]
                if "model" in fm and fm["model"] is not None:
                    out_fm_ordered["model"] = fm["model"]
                if "tools" in out_fm:
                    out_fm_ordered["tools"] = out_fm["tools"]
                out_fm = out_fm_ordered

                AgentIntegrator._warn_user_invocable_dropped(
                    fm, source, "Kiro", diagnostics, package_name
                )

        if out_fm:
            fm_text = yaml_to_str(out_fm)
            rendered = f"---\n{fm_text}---\n{body}"
        else:
            rendered = body

        return rendered, True

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def integrate_package_agents(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .github/agents/ + auto-copy to claude/cursor.

        Legacy entry point that preserves the multi-target auto-copy
        behaviour. New callers should use ``integrate_agents_for_target``
        directly.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        copilot = KNOWN_TARGETS["copilot"]

        self.init_link_resolver(package_info, project_root)
        agent_files = self.find_agent_files(
            package_info.install_path,
            diagnostics=diagnostics,
            package_name=package_info.package.name,
        )
        if not agent_files:
            return IntegrationResult(0, 0, 0, [])

        agents_dir = project_root / ".github" / "agents"
        write_seam.mkdir(agents_dir, parents=True, exist_ok=True)

        claude_agents_dir = None
        claude_dir = project_root / ".claude"
        if claude_dir.exists() and claude_dir.is_dir():
            claude_agents_dir = claude_dir / "agents"
            write_seam.mkdir(claude_agents_dir, parents=True, exist_ok=True)

        cursor_agents_dir = None
        cursor_dir = project_root / ".cursor"
        if cursor_dir.exists() and cursor_dir.is_dir():
            cursor_agents_dir = cursor_dir / "agents"
            write_seam.mkdir(cursor_agents_dir, parents=True, exist_ok=True)

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0

        for source_file in agent_files:
            target_filename = self.get_target_filename_for_target(
                source_file,
                package_info.package.name,
                copilot,
            )
            target_path = agents_dir / target_filename
            try:
                ensure_path_within(target_path, agents_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected agent target path: {exc}",
                        package=package_info.package.name,
                    )
                files_skipped += 1
                continue
            rendered, links_resolved = self._render_copy_agent(source_file, target_path)
            outcome = self._deploy_rendered_agent(
                target_path,
                source_file,
                portable_relpath(target_path, project_root),
                rendered,
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                target_paths=target_paths,
            )
            if outcome == _SKIPPED:
                files_skipped += 1
                continue
            if outcome == _ADOPTED:
                files_adopted += 1
            else:
                total_links_resolved += links_resolved
                files_integrated += 1

            if claude_agents_dir:
                claude_target = KNOWN_TARGETS["claude"]
                claude_filename = self.get_target_filename_for_target(
                    source_file,
                    package_info.package.name,
                    claude_target,
                )
                claude_path = claude_agents_dir / claude_filename
                try:
                    ensure_path_within(claude_path, claude_agents_dir)
                except PathTraversalError as exc:
                    if diagnostics is not None:
                        diagnostics.warn(
                            message=f"Rejected claude agent target path: {exc}",
                            package=package_info.package.name,
                        )
                    continue
                claude_rendered, _ = self._render_copy_agent(source_file, claude_path)
                if (
                    self._deploy_rendered_agent(
                        claude_path,
                        source_file,
                        portable_relpath(claude_path, project_root),
                        claude_rendered,
                        managed_files=managed_files,
                        force=force,
                        diagnostics=diagnostics,
                        target_paths=target_paths,
                    )
                    == _ADOPTED
                ):
                    files_adopted += 1

            if cursor_agents_dir:
                cursor_target = KNOWN_TARGETS["cursor"]
                cursor_filename = self.get_target_filename_for_target(
                    source_file,
                    package_info.package.name,
                    cursor_target,
                )
                cursor_path = cursor_agents_dir / cursor_filename
                try:
                    ensure_path_within(cursor_path, cursor_agents_dir)
                except PathTraversalError as exc:
                    if diagnostics is not None:
                        diagnostics.warn(
                            message=f"Rejected cursor agent target path: {exc}",
                            package=package_info.package.name,
                        )
                    continue
                cursor_rendered, _ = self._render_copy_agent(source_file, cursor_path)
                if (
                    self._deploy_rendered_agent(
                        cursor_path,
                        source_file,
                        portable_relpath(cursor_path, project_root),
                        cursor_rendered,
                        managed_files=managed_files,
                        force=force,
                        diagnostics=diagnostics,
                        target_paths=target_paths,
                    )
                    == _ADOPTED
                ):
                    files_adopted += 1

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
            files_adopted=files_adopted,
        )

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def get_target_filename_claude(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for Claude agents (plain .md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["claude"],
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_agents_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .claude/agents/.

        Legacy compat: ensures ``.claude/`` exists so the target-driven
        method does not skip (the old method did not guard on root-dir
        existence).
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        write_seam.mkdir(project_root / ".claude", parents=True, exist_ok=True)
        return self.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .github/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["copilot"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration_claude(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .claude/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["claude"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use get_target_filename_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def get_target_filename_cursor(self, source_file: Path, package_name: str) -> str:
        """Generate target filename for Cursor agents (plain .md)."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.get_target_filename_for_target(
            source_file,
            package_name,
            KNOWN_TARGETS["cursor"],
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def integrate_package_agents_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .cursor/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_agents_for_target(
            KNOWN_TARGETS["cursor"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def sync_integration_cursor(  # pylint: disable=duplicate-code  # deprecated shim; structural similarity is intentional
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .cursor/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["cursor"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # DEPRECATED: use integrate_agents_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def integrate_package_agents_opencode(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate agents into .opencode/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["opencode"], ...) instead.
    def sync_integration_opencode(  # pylint: disable=duplicate-code  # deprecated shim; structural similarity is intentional
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict[str, int]:
        """Remove APM-managed agent files from .opencode/agents/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["opencode"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )
