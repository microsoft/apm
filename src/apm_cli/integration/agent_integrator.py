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

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.opencode_frontmatter import validate_opencode_frontmatter
from apm_cli.utils.atomic_io import normalize_crlf_to_lf, write_text_lf
from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_yaml_str, yaml_to_str

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector

# Kiro capability tags approved for agent 'tools' frontmatter.
# Source: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
# Fail closed: any value not in this set blocks deployment of that agent.
KIRO_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "write",
        "shell",
        "web",
        "subagent",
        "knowledge",
        "context",
        "todo_list",
        "@mcp",
        "@builtin",
        "*",
    }
)


class AgentIntegrator(BaseIntegrator):
    """Handles integration of APM package agents into .github/agents/, .claude/agents/, .cursor/agents/, and .kiro/agents/."""

    # Deploys via write_text_lf -> compare adopt candidates in LF mode.
    _LF_NORMALIZED_DEPLOY = True

    def find_agent_files(self, package_path: Path) -> list[Path]:
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
        # Flat search in package root
        files += self.find_files_by_glob(package_path, "*.agent.md")
        # Recursive search in .apm/agents/ (use ** glob for subdirectories)
        apm_agents = package_path / ".apm" / "agents"
        if apm_agents.exists():
            files += self.find_files_by_glob(apm_agents, "**/*.agent.md")
            # Also pick up plain .md files; the directory name implies type
            for f in self.find_files_by_glob(apm_agents, "**/*.md"):
                if not f.name.endswith(".agent.md") and f not in files:
                    files.append(f)
        return files

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
        agent_files = self.find_agent_files(package_info.install_path)
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

                # Compare rendered artifact (not raw source) against existing
                # target so a pre-placed file with invalid tools cannot be
                # adopted by matching source bytes.
                rel_path = portable_relpath(target_path, project_root)
                if target_path.exists() and not target_path.is_symlink():
                    try:
                        existing = target_path.read_bytes()
                        rendered_bytes = normalize_crlf_to_lf(rendered).encode("utf-8")
                        if existing == rendered_bytes:
                            target_paths.append(target_path)
                            files_adopted += 1
                            continue
                    except OSError:
                        pass
                if self.check_collision(
                    target_path, rel_path, managed_files, force, diagnostics=diagnostics
                ):
                    files_skipped += 1
                    continue

                # Safe to materialize: ensure parent dirs exist then write.
                if not agents_dir_created:
                    agents_dir.mkdir(parents=True, exist_ok=True)
                    agents_dir_created = True
                target_path.parent.mkdir(parents=True, exist_ok=True)
                write_text_lf(target_path, rendered)
                files_integrated += 1
                target_paths.append(target_path)
                continue

            # Non-kiro path: eager mkdir (existing behavior preserved).
            if not agents_dir_created:
                agents_dir.mkdir(parents=True, exist_ok=True)
                agents_dir_created = True

            rel_path = portable_relpath(target_path, project_root)

            skip, adopted = self._check_adopt_or_skip(
                target_path, source_file, rel_path, managed_files, force, diagnostics, target_paths
            )
            if skip:
                if adopted:
                    files_adopted += 1
                else:
                    files_skipped += 1
                continue

            if mapping.format_id == "codex_agent":
                self._write_codex_agent(
                    source_file,
                    target_path,
                    diagnostics=diagnostics,
                    package_name=package_info.package.name,
                )
                links_resolved = 0
            else:
                if mapping.format_id == "opencode_agent":
                    self._warn_opencode_frontmatter(
                        source_file, diagnostics, package_info.package.name
                    )
                links_resolved = self.copy_agent(source_file, target_path)
            total_links_resolved += links_resolved
            files_integrated += 1
            target_paths.append(target_path)

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

    def copy_agent(self, source: Path, target: Path) -> int:
        """Copy agent file verbatim, resolving context links.

        Args:
            source: Source file path
            target: Target file path

        Returns:
            int: Number of links resolved
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")
        content = source.read_text(encoding="utf-8")
        content, links_resolved = self.resolve_links(content, source, target)
        write_text_lf(target, content)
        return links_resolved

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
    def _write_codex_agent(
        source: Path,
        target: Path,
        *,
        diagnostics: DiagnosticCollector | None = None,
        package_name: str = "",
    ) -> None:
        """Transform an ``.agent.md`` file to Codex ``.toml`` format.

        Parses YAML frontmatter for ``name`` and ``description``, uses
        the markdown body as ``developer_instructions``.
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")
        import toml as _toml

        content = source.read_text(encoding="utf-8")

        name = source.stem
        if name.endswith(".agent"):
            name = name[: -len(".agent")]
        description = ""
        body = content

        fm_match = AgentIntegrator._FRONTMATTER_RE.match(content)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
                if isinstance(fm, dict):
                    name = fm.get("name", name)
                    description = fm.get("description", description)
                else:
                    AgentIntegrator._warn_codex_unverified_scope(
                        diagnostics,
                        source,
                        package_name,
                        "YAML frontmatter must be a mapping and was ignored",
                        "fix the source agent frontmatter to a YAML mapping, "
                        "then rerun 'apm install'",
                    )
                if isinstance(fm, dict) and "tools" in fm:
                    AgentIntegrator._warn_codex_tools_dropped(
                        diagnostics,
                        source,
                        package_name,
                    )
            except yaml.YAMLError:
                AgentIntegrator._warn_codex_unverified_scope(
                    diagnostics,
                    source,
                    package_name,
                    "invalid YAML frontmatter was ignored",
                    "repair the frontmatter, then rerun 'apm install'",
                )

        doc = {
            "name": name,
            "description": description,
            "developer_instructions": body.strip(),
        }
        write_text_lf(target, _toml.dumps(doc))

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
        source declares tools outside KIRO_AGENT_ALLOWED_TOOLS or has an
        unparseable frontmatter. On failure an actionable diagnostic is
        emitted via diagnostics.error -- the caller MUST skip all target
        mutations without writing any bytes.

        Ref: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
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
                        incompatible = set(tools_strs) - KIRO_AGENT_ALLOWED_TOOLS
                        if incompatible:
                            if diagnostics is not None:
                                names = ", ".join(sorted(incompatible))
                                diagnostics.error(
                                    message=(
                                        f"Kiro agent {printable_ascii_text(source.name)}: "
                                        f"unsupported tool(s) {names!a} -- "
                                        "agent will not be deployed. "
                                        "Remove or replace with Kiro-approved capability "
                                        "tags (read, write, shell, web, subagent, knowledge, "
                                        "context, todo_list, @mcp, @builtin, *). "
                                        "Ref: https://kiro.dev/docs/custom-agents/"
                                    ),
                                    package=printable_ascii_text(package_name),
                                )
                            return None, False
                        tools_out = tools_strs
                    elif isinstance(tools_raw, str):
                        tool = tools_raw.strip()
                        if tool not in KIRO_AGENT_ALLOWED_TOOLS:
                            if diagnostics is not None:
                                diagnostics.error(
                                    message=(
                                        f"Kiro agent {printable_ascii_text(source.name)}: "
                                        f"unsupported tool {tool!a} -- "
                                        "agent will not be deployed. "
                                        "Use a Kiro-approved capability tag. "
                                        "Ref: https://kiro.dev/docs/custom-agents/"
                                    ),
                                    package=printable_ascii_text(package_name),
                                )
                            return None, False
                        tools_out = [tool]
                    else:
                        if diagnostics is not None:
                            diagnostics.error(
                                message=(
                                    f"Kiro agent {printable_ascii_text(source.name)}: "
                                    "'tools' must be a list of capability tags -- "
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

        if out_fm:
            fm_text = yaml_to_str(out_fm)
            rendered = f"---\n{fm_text}---\n{body}"
        else:
            rendered = body

        return rendered, True

    @staticmethod
    def _write_kiro_agent(
        source: Path,
        target: Path,
        *,
        diagnostics=None,
        package_name: str = "",
    ) -> bool:
        """Write a pre-rendered Kiro agent to disk; fail closed on invalid tools.

        Delegates render+validate to _preflight_render_kiro_agent (the single
        owner). Writes only when validation passes. Callers in the integration
        loop should prefer calling _preflight_render_kiro_agent directly so
        the rendered content can be reused for the adopt comparison.

        Ref: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
        """
        rendered, ok = AgentIntegrator._preflight_render_kiro_agent(
            source, diagnostics=diagnostics, package_name=package_name
        )
        if not ok:
            return False
        write_text_lf(target, rendered)  # type: ignore[arg-type]
        return True

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
        agent_files = self.find_agent_files(package_info.install_path)
        if not agent_files:
            return IntegrationResult(0, 0, 0, [])

        agents_dir = project_root / ".github" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        claude_agents_dir = None
        claude_dir = project_root / ".claude"
        if claude_dir.exists() and claude_dir.is_dir():
            claude_agents_dir = claude_dir / "agents"
            claude_agents_dir.mkdir(parents=True, exist_ok=True)

        cursor_agents_dir = None
        cursor_dir = project_root / ".cursor"
        if cursor_dir.exists() and cursor_dir.is_dir():
            cursor_agents_dir = cursor_dir / "agents"
            cursor_agents_dir.mkdir(parents=True, exist_ok=True)

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
            rel_path = portable_relpath(target_path, project_root)

            if self.try_adopt_identical(
                target_path, source_file, target_paths, lf_normalized_deploy=True
            ):
                files_adopted += 1
            else:
                if self.check_collision(
                    target_path, rel_path, managed_files, force, diagnostics=diagnostics
                ):
                    files_skipped += 1
                    continue
                links_resolved = self.copy_agent(source_file, target_path)
                total_links_resolved += links_resolved
                files_integrated += 1
                target_paths.append(target_path)

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
                claude_rel = portable_relpath(claude_path, project_root)
                if self.try_adopt_identical(
                    claude_path, source_file, target_paths, lf_normalized_deploy=True
                ):
                    files_adopted += 1
                elif not self.check_collision(
                    claude_path, claude_rel, managed_files, force, diagnostics=diagnostics
                ):
                    self.copy_agent(source_file, claude_path)
                    target_paths.append(claude_path)

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
                cursor_rel = portable_relpath(cursor_path, project_root)
                if self.try_adopt_identical(
                    cursor_path, source_file, target_paths, lf_normalized_deploy=True
                ):
                    files_adopted += 1
                elif not self.check_collision(
                    cursor_path, cursor_rel, managed_files, force, diagnostics=diagnostics
                ):
                    self.copy_agent(source_file, cursor_path)
                    target_paths.append(cursor_path)

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

        (project_root / ".claude").mkdir(parents=True, exist_ok=True)
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
