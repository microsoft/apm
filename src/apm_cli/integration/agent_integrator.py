"""Agent integration functionality for APM packages.

Note: SKILL.md files are NOT transformed to .agent.md files. Skills are handled
separately by SkillIntegrator and installed to .github/skills/ as native skills.
See skill-strategy.md for the full architectural rationale (T5).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

from . import _agent_legacy, _agent_writers
from ._agent_integrator_deprecated import _AgentDeprecatedMixin
from ._opts import IntegrateOpts, SyncRemoveOpts


def _write_agent_for_mapping(
    integrator, mapping, source_file: Path, target_path: Path, diagnostics
) -> int:
    """Write one agent file using the target mapping format."""
    if mapping.format_id == "codex_agent":
        integrator._write_codex_agent(source_file, target_path)
        return 0
    if mapping.format_id == "windsurf_agent_skill":
        return integrator._write_windsurf_agent_skill(
            source_file,
            target_path,
            diagnostics=diagnostics,
        )
    return integrator.copy_agent(source_file, target_path)


if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


class AgentIntegrator(BaseIntegrator, _AgentDeprecatedMixin):
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
        opts: IntegrateOpts | None = None,
        **legacy_kwargs,
    ) -> IntegrationResult:
        """Integrate agents from a package for a single *target*.

        Each call deploys to exactly one target.  The dispatch loop in
        ``install.py`` calls this once per active target that supports
        the ``agents`` primitive.
        """
        if opts is None and legacy_kwargs:
            opts = IntegrateOpts(
                force=legacy_kwargs.get("force", False),
                managed_files=legacy_kwargs.get("managed_files"),
                diagnostics=legacy_kwargs.get("diagnostics"),
            )
        resolved_opts = opts or IntegrateOpts()
        force = resolved_opts.force
        managed_files = resolved_opts.managed_files
        diagnostics = resolved_opts.diagnostics

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

            skip, adopted = self._check_adopt_or_skip(
                target_path, source_file, rel_path, managed_files, force, diagnostics, target_paths
            )
            if skip:
                if adopted:
                    files_adopted += 1
                else:
                    files_skipped += 1
                continue

            links_resolved = _write_agent_for_mapping(
                self,
                mapping,
                source_file,
                target_path,
                diagnostics,
            )
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
        managed_files: set | None = None,
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
            prefix,
            SyncRemoveOpts(
                legacy_glob_dir=legacy_dir,
                legacy_glob_pattern=legacy_pattern,
                targets=[target],
            ),
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
    # Codex agent transformer (MD -> TOML)  -- implementation in _agent_writers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_codex_agent(source: Path, target: Path) -> None:
        """Transform an ``.agent.md`` file to Codex ``.toml`` format.

        Delegates to :func:`._agent_writers.write_codex_agent`.
        """
        _agent_writers.write_codex_agent(source, target)

    # ------------------------------------------------------------------
    # Windsurf agent-skill transformer -- implementation in _agent_writers
    # ------------------------------------------------------------------

    def _write_windsurf_agent_skill(
        self, source: Path, target: Path, diagnostics=None
    ) -> int:  # not @staticmethod: needs self.resolve_links()
        """Transform an ``.agent.md`` file to a Windsurf Skill (``SKILL.md``).

        Delegates to :func:`._agent_writers.write_windsurf_agent_skill`,
        passing ``self.resolve_links`` as the link-resolution callback.
        """
        return _agent_writers.write_windsurf_agent_skill(
            source, target, self.resolve_links, diagnostics=diagnostics
        )
