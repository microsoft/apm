"""Agent integration functionality for APM packages.

Note: SKILL.md files are NOT transformed to .agent.md files. Skills are handled
separately by SkillIntegrator and installed to .github/skills/ as native skills.
See skill-strategy.md for the full architectural rationale (T5).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List  # noqa: F401, UP035

import yaml

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


class AgentIntegrator(BaseIntegrator):
    """Handles integration of APM package agents into .github/agents/, .claude/agents/, and .cursor/agents/."""

    def find_agent_files(self, package_path: Path) -> list[Path]:
        """Find all .agent.md and .chatmode.md files in a package.

        Searches in:
        - Package root directory (.agent.md and .chatmode.md)
        - .apm/agents/ subdirectory (new standard, recursive)
        - .apm/chatmodes/ subdirectory (legacy)

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to agent files
        """
        files: list[Path] = []
        # Flat search in package root
        files += self.find_files_by_glob(package_path, "*.agent.md")
        files += self.find_files_by_glob(package_path, "*.chatmode.md")
        # Recursive search in .apm/agents/ (use ** glob for subdirectories)
        apm_agents = package_path / ".apm" / "agents"
        if apm_agents.exists():
            files += self.find_files_by_glob(apm_agents, "**/*.agent.md")
            # Also pick up plain .md files; the directory name implies type
            for f in self.find_files_by_glob(apm_agents, "**/*.md"):
                if not f.name.endswith(".agent.md") and f not in files:
                    files.append(f)
        # Flat search in .apm/chatmodes/ (legacy)
        apm_chatmodes = package_path / ".apm" / "chatmodes"
        if apm_chatmodes.exists():
            files += self.find_files_by_glob(apm_chatmodes, "*.chatmode.md")
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
        if source_file.name.endswith(".agent.md"):
            stem = source_file.name[:-9]
        elif source_file.name.endswith(".chatmode.md"):
            stem = source_file.name[:-12]
        else:
            stem = source_file.stem
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
        agents_dir.mkdir(parents=True, exist_ok=True)

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0

        for source_file in agent_files:
            target_filename = self.get_target_filename_for_target(
                source_file,
                package_info.package.name,
                target,
            )
            target_path = agents_dir / target_filename
            # Defense-in-depth: target_filename comes from
            # get_target_filename_for_target which strips path separators,
            # but assert containment under agents_dir so a future
            # regression cannot smuggle a traversal sequence past the
            # adopt branch (which fires *before* check_collision and
            # would otherwise blindly trust the computed path). Mirrors
            # the guard already in command_integrator and
            # instruction_integrator.
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

            if self.is_content_identical_to_source(target_path, source_file):
                # Pre-existing file is byte-identical to source -- silently
                # adopt so deployed_files reflects reality. See
                # BaseIntegrator.is_content_identical_to_source for the
                # full rationale (catch-22 fix).
                target_paths.append(target_path)
                files_adopted += 1
                continue

            if self.check_collision(
                target_path,
                rel_path,
                managed_files,
                force,
                diagnostics=diagnostics,
            ):
                files_skipped += 1
                continue

            if mapping.format_id == "codex_agent":
                self._write_codex_agent(source_file, target_path)
                links_resolved = 0
            elif mapping.format_id == "windsurf_agent_skill":
                links_resolved = self._write_windsurf_agent_skill(
                    source_file, target_path, diagnostics=diagnostics
                )
            else:
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
        target.write_text(content, encoding="utf-8")
        return links_resolved

    # ------------------------------------------------------------------
    # Codex agent transformer (MD -> TOML)
    # ------------------------------------------------------------------

    _FRONTMATTER_RE = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n?",
        re.DOTALL,
    )

    @staticmethod
    def _write_codex_agent(source: Path, target: Path) -> None:
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
                fm = yaml.safe_load(fm_match.group(1)) or {}
                name = fm.get("name", name)
                description = fm.get("description", description)
            except Exception:
                pass

        doc = {
            "name": name,
            "description": description,
            "developer_instructions": body.strip(),
        }
        target.write_text(_toml.dumps(doc), encoding="utf-8")

    # ------------------------------------------------------------------
    # Windsurf agent-skill transformer (agent.md -> skills/<name>/SKILL.md)
    # ------------------------------------------------------------------

    def _write_windsurf_agent_skill(
        self, source: Path, target: Path, diagnostics=None
    ) -> int:  # not @staticmethod: needs self.resolve_links()
        """Transform an ``.agent.md`` file to a Windsurf Skill (``SKILL.md``).

        Windsurf Skills are the closest equivalent to a specialist persona:
        - Invocable with ``@skill-name`` (like ``@agent-name`` in Copilot)
        - Auto-invoked by Cascade when the description matches the task
        - Support a directory with supplementary resource files

        The conversion:
        - Keeps ``name`` (or derives from filename) and ``description``.
        - Strips agent-specific keys (``model``, ``tools``) and emits a
          diagnostic warning when those fields are dropped.
        - Preserves the markdown body verbatim.
        """
        if source.is_symlink():
            raise ValueError(f"Refusing to read symlink source: {source}")
        content = source.read_text(encoding="utf-8")

        stem = source.name
        if stem.endswith(".agent.md"):
            stem = stem[:-9]
        elif stem.endswith(".chatmode.md"):
            stem = stem[:-12]
        else:
            stem = Path(stem).stem

        fm_match = AgentIntegrator._FRONTMATTER_RE.match(content)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                fm = {}
        else:
            body = content
            fm = {}

        dropped = [k for k in ("tools", "model") if fm.get(k)]
        if dropped and diagnostics is not None:
            diagnostics.warn(
                f"Windsurf skill conversion dropped frontmatter field(s) "
                f"{', '.join(dropped)} from {source.name}",
                detail="Windsurf Skills do not support agent-only fields; "
                "only name, description, and body are preserved.",
            )

        name = fm.get("name", stem)
        description = fm.get("description", "")

        # Use yaml.safe_dump to safely serialize values -- prevents YAML key
        # injection via multi-line name/description strings.

        fm_data: dict = {"name": name}
        if description:
            fm_data["description"] = description
        fm_yaml = yaml.safe_dump(  # yaml-io-exempt: serializes to string, not file handle
            fm_data, default_flow_style=False, allow_unicode=True
        ).rstrip("\n")

        result = f"---\n{fm_yaml}\n---\n" + body
        result, links_resolved = self.resolve_links(result, source, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result, encoding="utf-8")
        return links_resolved

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

            if self.is_content_identical_to_source(target_path, source_file):
                target_paths.append(target_path)
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
                if self.is_content_identical_to_source(claude_path, source_file):
                    target_paths.append(claude_path)
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
                if self.is_content_identical_to_source(cursor_path, source_file):
                    target_paths.append(cursor_path)
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
    def sync_integration_cursor(
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
    def sync_integration_opencode(
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
