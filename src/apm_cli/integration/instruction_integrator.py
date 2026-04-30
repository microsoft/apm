"""Instruction integration functionality for APM packages.

Deploys .instructions.md files from APM packages to the appropriate
target directory (e.g. ``.github/instructions/`` for Copilot,
``.cursor/rules/`` for Cursor, ``.claude/rules/`` for Claude Code).
Content transforms are selected by the ``format_id`` field in
``PrimitiveMapping``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set  # noqa: F401, UP035

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


class InstructionIntegrator(BaseIntegrator):
    """Handles integration of APM package instructions.

    Deploys .instructions.md files to target-specific directories:

    * Copilot: ``.github/instructions/`` (verbatim, preserving applyTo:)
    * Cursor: ``.cursor/rules/`` (``.mdc`` format, applyTo: -> globs:)
    * Claude Code: ``.claude/rules/`` (``.md`` format, applyTo: -> paths:)
    * Gemini CLI: compile-only (GEMINI.md) -- no per-file rule deployment
    """

    def find_instruction_files(self, package_path: Path) -> list[Path]:
        """Find all .instructions.md files in a package.

        Searches in .apm/instructions/ subdirectory.
        """
        return self.find_files_by_glob(
            package_path,
            "*.instructions.md",
            subdirs=[".apm/instructions"],
        )

    def copy_instruction(self, source: Path, target: Path) -> int:
        """Copy instruction file with link resolution.

        Preserves applyTo: frontmatter and all content as-is.
        """
        content = source.read_text(encoding="utf-8")
        content, links_resolved = self.resolve_links(content, source, target)
        target.write_text(content, encoding="utf-8")
        return links_resolved

    # ------------------------------------------------------------------
    # Target-driven API (data-driven dispatch)
    # ------------------------------------------------------------------

    def integrate_instructions_for_target(
        self,
        target: TargetProfile,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate instructions for a single *target*.

        Selects the content transform via ``format_id``:

        * ``cursor_rules``  -- convert ``applyTo:`` to ``globs:`` frontmatter
        * ``claude_rules``  -- convert ``applyTo:`` to ``paths:`` frontmatter
        * anything else     -- copy verbatim (identity transform)
        """
        mapping = target.primitives.get("instructions")
        if not mapping:
            return IntegrationResult(0, 0, 0, [])

        effective_root = mapping.deploy_root or target.root_dir
        target_root = project_root / effective_root
        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            return IntegrationResult(0, 0, 0, [])

        self.init_link_resolver(package_info, project_root)
        instruction_files = self.find_instruction_files(package_info.install_path)
        if not instruction_files:
            return IntegrationResult(0, 0, 0, [])

        deploy_dir = target_root / mapping.subdir
        deploy_dir.mkdir(parents=True, exist_ok=True)

        fmt = mapping.format_id
        needs_rename = fmt in ("cursor_rules", "claude_rules")

        files_integrated = 0
        files_skipped = 0
        target_paths: list[Path] = []
        total_links_resolved = 0

        for source_file in instruction_files:
            if needs_rename:
                stem = source_file.name
                if stem.endswith(".instructions.md"):
                    stem = stem[: -len(".instructions.md")]
                target_name = f"{stem}{mapping.extension}"
            else:
                target_name = source_file.name

            target_path = deploy_dir / target_name
            rel_path = portable_relpath(target_path, project_root)

            if self.check_collision(
                target_path,
                rel_path,
                managed_files,
                force,
                diagnostics=diagnostics,
            ):
                files_skipped += 1
                continue

            if fmt == "cursor_rules":
                links_resolved = self.copy_instruction_cursor(source_file, target_path)
            elif fmt == "claude_rules":
                links_resolved = self.copy_instruction_claude(source_file, target_path)
            else:
                links_resolved = self.copy_instruction(source_file, target_path)

            total_links_resolved += links_resolved
            files_integrated += 1
            target_paths.append(target_path)

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
        )

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed instruction files for a single *target*."""
        mapping = target.primitives.get("instructions")
        if not mapping:
            return {"files_removed": 0, "errors": 0}
        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        legacy_dir = project_root / effective_root / mapping.subdir
        if mapping.format_id == "cursor_rules":
            legacy_pattern = "*.mdc"
        elif mapping.format_id == "claude_rules":
            # Do not use a broad legacy glob for Claude rules to avoid
            # deleting user-authored .md files under .claude/rules/.
            legacy_pattern = None
        else:
            legacy_pattern = "*.instructions.md"
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

    # DEPRECATED: use integrate_instructions_for_target(KNOWN_TARGETS["copilot"], ...) instead.
    def integrate_package_instructions(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
        logger=None,
    ) -> IntegrationResult:
        """Integrate instructions into .github/instructions/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_instructions_for_target(
            KNOWN_TARGETS["copilot"],
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
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed instruction files from .github/instructions/."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["copilot"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # ------------------------------------------------------------------
    # Cursor Rules (.mdc) support
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_to_cursor_rules(content: str) -> str:
        """Convert APM instruction content to Cursor Rules ``.mdc`` format.

        Parses existing YAML frontmatter, maps ``applyTo`` → ``globs``,
        extracts or generates a ``description``, and rewrites the
        frontmatter in Cursor's expected format.
        """
        body = content
        apply_to = ""
        description = ""

        # Parse existing frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            fm_block = fm_match.group(1)
            body = content[fm_match.end() :]

            for line in fm_block.splitlines():
                line_stripped = line.strip()
                if line_stripped.startswith("applyTo:"):
                    apply_to = line_stripped[len("applyTo:") :].strip().strip("'\"")
                elif line_stripped.startswith("description:"):
                    description = line_stripped[len("description:") :].strip().strip("'\"")

        # Generate description from first content sentence if missing
        if not description:
            for line in body.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    description = stripped.split(".")[0].strip()
                    break

        # Build Cursor Rules frontmatter
        parts = ["---"]
        if description:
            parts.append(f"description: {description}")
        if apply_to:
            parts.append(f'globs: "{apply_to}"')
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    def copy_instruction_cursor(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Cursor Rules format.

        Converts ``applyTo:`` → ``globs:`` frontmatter and resolves links.
        """
        content = source.read_text(encoding="utf-8")
        content = self._convert_to_cursor_rules(content)
        content, links_resolved = self.resolve_links(content, source, target)
        target.write_text(content, encoding="utf-8")
        return links_resolved

    # DEPRECATED: use integrate_instructions_for_target(KNOWN_TARGETS["cursor"], ...) instead.
    def integrate_package_instructions_cursor(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
        logger=None,
    ) -> IntegrationResult:
        """Integrate instructions as Cursor Rules into ``.cursor/rules/``."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_instructions_for_target(
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
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed Cursor Rules files from ``.cursor/rules/``."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["cursor"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )

    # ------------------------------------------------------------------
    # Claude Code Rules (.md with paths: frontmatter)
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_to_claude_rules(content: str) -> str:
        """Convert APM instruction content to Claude Code rules ``.md`` format.

        Parses existing YAML frontmatter, maps ``applyTo`` to ``paths``
        (YAML list), and rewrites the frontmatter in Claude's expected
        format.  Instructions without ``applyTo`` become unconditional
        rules (no ``paths`` key).

        Ref: https://code.claude.com/docs/en/memory#organize-rules-with-claude%2Frules%2F
        """
        body = content
        apply_to = ""

        # Parse existing frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            fm_block = fm_match.group(1)
            body = content[fm_match.end() :]

            for line in fm_block.splitlines():
                line_stripped = line.strip()
                if line_stripped.startswith("applyTo:"):
                    apply_to = line_stripped[len("applyTo:") :].strip().strip("'\"")

        # Build Claude rules frontmatter (only when path-scoped)
        if apply_to:
            parts = ["---"]
            parts.append("paths:")
            parts.append(f'  - "{apply_to}"')
            parts.append("---")
            return "\n".join(parts) + "\n\n" + body.lstrip("\n")

        # No applyTo -> unconditional rule, return body without frontmatter
        return body.lstrip("\n")

    def copy_instruction_claude(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Claude Code rules format.

        Converts ``applyTo:`` to ``paths:`` frontmatter and resolves links.
        """
        content = source.read_text(encoding="utf-8")
        content = self._convert_to_claude_rules(content)
        content, links_resolved = self.resolve_links(content, source, target)
        target.write_text(content, encoding="utf-8")
        return links_resolved

    # DEPRECATED: use integrate_instructions_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def integrate_package_instructions_claude(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
        logger=None,
    ) -> IntegrationResult:
        """Integrate instructions as Claude Code rules into ``.claude/rules/``."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.integrate_instructions_for_target(
            KNOWN_TARGETS["claude"],
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    # DEPRECATED: use sync_for_target(KNOWN_TARGETS["claude"], ...) instead.
    def sync_integration_claude(
        self,
        apm_package,
        project_root: Path,
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed Claude Code rules files from ``.claude/rules/``."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        return self.sync_for_target(
            KNOWN_TARGETS["claude"],
            apm_package,
            project_root,
            managed_files=managed_files,
        )
