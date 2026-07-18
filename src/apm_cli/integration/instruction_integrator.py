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
from typing import TYPE_CHECKING, ClassVar

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.targets import RULE_FORMATS
from apm_cli.utils.atomic_io import normalize_crlf_to_lf, write_text_lf
from apm_cli.utils.console import _rich_echo
from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.patterns import parse_apply_to, yaml_double_quote

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

    # Deploys via write_text_lf -> compare adopt candidates in LF mode.
    _LF_NORMALIZED_DEPLOY = True

    # Map format_id -> converter method.  Built once at class load time;
    # avoids rebuilding the dict on every ``_render_instruction`` call.
    _FORMAT_CONVERTERS: ClassVar[dict[str, str]] = {
        "cursor_rules": "_convert_to_cursor_rules",
        "claude_rules": "_convert_to_claude_rules",
        "windsurf_rules": "_convert_to_windsurf_rules",
        "kiro_steering": "_convert_to_kiro_steering",
        "antigravity_rules": "_convert_to_antigravity_rules",
    }

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
        write_text_lf(target, content)
        return links_resolved

    def _render_instruction(self, source: Path, target: Path, fmt: str) -> tuple[str, int]:
        """Render *source* to the content it would deploy for *fmt*, WITHOUT
        writing. Returns ``(content, links_resolved)``.

        Used both by the ``copy_instruction_*`` writers and by the rule-dir
        deploy loop to decide adopt-vs-rewrite against the *transformed* output
        (a format-transformed rule is never byte-identical to its source).

        The transforming formats are exactly :data:`targets.RULE_FORMATS` --
        the single home for "which formats transform".  Any ``fmt`` outside it
        is copied verbatim (identity transform).
        """
        content = source.read_text(encoding="utf-8")
        if fmt in RULE_FORMATS:
            converter = getattr(self, self._FORMAT_CONVERTERS[fmt])
            content = converter(content)
        content, links_resolved = self.resolve_links(content, source, target)
        return content, links_resolved

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
        scope=None,
    ) -> IntegrationResult:
        """Integrate instructions for a single *target*.

        Selects the content transform via ``format_id``:

        * ``cursor_rules``    -- convert ``applyTo:`` to ``globs:`` frontmatter
        * ``claude_rules``    -- convert ``applyTo:`` to ``paths:`` frontmatter
        * ``windsurf_rules``  -- convert ``applyTo:`` to ``trigger: glob`` frontmatter
        * anything else       -- copy verbatim (identity transform)

        ``managed_files`` is consulted for collision detection on identity
        targets only.  For rule-dir targets (``mapping.output_compare``) it is
        deliberately NOT consulted: those files are APM-owned per-file
        (``target_name`` derives 1:1 from a source instruction), so an existing
        file there is always APM's and is adopted-or-rewritten regardless of
        whether it was recorded as managed (apm#1662).
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

        # APM-owned rule dirs (.claude/rules, .cursor/rules, .windsurf/rules):
        # the deployed file is a format-transform of its source and the target
        # name derives 1:1 from the source, so APM owns it per-file. Gates both
        # the filename rename and the output-comparison / collision-guard
        # bypass. Single source of truth: mapping.output_compare (targets.py).
        apm_owned_rule_dir = mapping.output_compare

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0

        for source_file in instruction_files:
            if apm_owned_rule_dir:
                stem = source_file.name
                if stem.endswith(".instructions.md"):
                    stem = stem[: -len(".instructions.md")]
                target_name = f"{stem}{mapping.extension}"
            else:
                target_name = source_file.name

            target_path = deploy_dir / target_name
            # target_name is Path.name (no separators), so traversal via
            # deploy_dir is impossible.  Validated against deploy_dir (not
            # project_root) so user-scope targets whose root resolves
            # outside the workspace still work correctly.
            ensure_path_within(target_path, deploy_dir)

            rel_path = portable_relpath(target_path, project_root)

            if apm_owned_rule_dir:
                # Path containment already validated by ensure_path_within above.
                # Structural invariant: target_name derives 1:1 from source, so
                # ANY existing file here is APM's -- must hold for every format
                # with output_compare=True. managed_files is not consulted.
                #
                # The deployed file is format-transformed, so it is never
                # byte-identical to the *source* -- the source-based adopt always
                # misses, the file gets treated as a user-authored collision and
                # skipped, and it falls out of ``local_deployed_files`` so later
                # edits never propagate (apm#1662). Instead compare against the
                # transformed *output*: adopt when up-to-date (no churn), else
                # (re)write. Always record the path so it stays managed on the
                # next run.
                new_content, links_resolved = self._render_instruction(
                    source_file, target_path, fmt
                )
                # Compare the on-disk bytes against the exact bytes
                # write_text_lf would emit (LF-normalized). A text-mode
                # read_text() comparison would collapse CRLF->LF and wrongly
                # adopt a stale CRLF file left by a pre-fix install, pinning a
                # platform-dependent hash in the lockfile (apm#1889).
                if (
                    not force
                    and target_path.exists()
                    and target_path.read_bytes()
                    == normalize_crlf_to_lf(new_content).encode("utf-8")
                ):
                    files_adopted += 1
                    target_paths.append(target_path)
                    if diagnostics is not None and getattr(diagnostics, "verbose", False):
                        _rich_echo(f"  [=] adopted-unchanged: {rel_path}", color="dim")
                    continue
                write_text_lf(target_path, new_content)
                total_links_resolved += links_resolved
                files_integrated += 1
                target_paths.append(target_path)
                if diagnostics is not None and getattr(diagnostics, "verbose", False):
                    _rich_echo(f"  [*] rewritten: {rel_path}", color="dim")
                continue

            skip, adopted = self._check_adopt_or_skip(
                target_path, source_file, rel_path, managed_files, force, diagnostics, target_paths
            )
            if skip:
                if adopted:
                    files_adopted += 1
                else:
                    files_skipped += 1
                continue

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
            files_adopted=files_adopted,
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
        elif mapping.format_id == "windsurf_rules":
            # Do not use a broad legacy glob for Windsurf rules to avoid
            # deleting user-authored .md files under .windsurf/rules/.
            legacy_pattern = None
        elif mapping.format_id == "claude_rules":
            # Do not use a broad legacy glob for Claude rules to avoid
            # deleting user-authored .md files under .claude/rules/.
            legacy_pattern = None
        elif mapping.format_id == "kiro_steering":
            # Do not delete user-authored steering markdown under .kiro/steering/.
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
        globs = parse_apply_to(apply_to)
        if len(globs) == 1:
            parts.append(f"globs: {yaml_double_quote(globs[0])}")
        elif globs:
            parts.append("globs:")
            parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    def copy_instruction_cursor(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Cursor Rules format.

        Converts ``applyTo:`` → ``globs:`` frontmatter and resolves links.
        """
        content, links_resolved = self._render_instruction(source, target, "cursor_rules")
        write_text_lf(target, content)
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
    def sync_integration_cursor(  # pylint: disable=duplicate-code  # deprecated shim; structural similarity is intentional
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
    # Windsurf Rules (.md with trigger/globs frontmatter)
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_to_windsurf_rules(content: str) -> str:
        """Convert APM instruction content to Windsurf rules ``.md`` format.

        Parses existing YAML frontmatter via ``yaml.safe_load``, maps
        ``applyTo`` to Windsurf's ``trigger: glob`` + ``globs`` frontmatter.
        Instructions without ``applyTo`` become ``trigger: always_on`` rules.

        Ref: https://docs.windsurf.com/windsurf/cascade/memories
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        apply_to = ""

        # Parse existing frontmatter with the bounded loader so a hostile
        # frontmatter block in an untrusted package cannot hang the parser.
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception:
                fm = {}
            apply_to = str(fm.get("applyTo", "")).strip()

        # Build Windsurf rules frontmatter
        parts = ["---"]
        # Sanitize: strip newlines to prevent frontmatter injection
        # via crafted applyTo values (e.g. "**\ntrigger: always_on").
        safe_apply_to = apply_to.replace("\n", " ").replace("\r", " ").strip()
        globs = parse_apply_to(safe_apply_to)
        if globs:
            parts.append("trigger: glob")
            if len(globs) == 1:
                parts.append(f"globs: {yaml_double_quote(globs[0])}")
            else:
                parts.append("globs:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        else:
            parts.append("trigger: always_on")
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

    def copy_instruction_windsurf(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Windsurf rules format.

        Converts ``applyTo:`` to ``trigger: glob`` + ``globs:`` frontmatter
        and resolves links.
        """
        content, links_resolved = self._render_instruction(source, target, "windsurf_rules")
        write_text_lf(target, content)
        return links_resolved

    # ------------------------------------------------------------------
    # Kiro Steering (.md with inclusion frontmatter)
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_to_kiro_steering(content: str) -> str:
        """Convert APM instructions to Kiro steering format.

        Kiro steering files use ``inclusion: always`` for unconditional
        guidance and ``inclusion: fileMatch`` plus ``fileMatchPattern`` for
        path-scoped guidance. APM's ``applyTo`` frontmatter is the source of
        truth for that scoping.
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        globs = []

        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception:
                fm = {}
            raw_apply_to = fm.get("applyTo", "")
            if isinstance(raw_apply_to, list):
                globs = [
                    s
                    for item in raw_apply_to
                    if (s := str(item).replace("\n", " ").replace("\r", " ").strip())
                ]
            else:
                safe_apply_to = str(raw_apply_to).replace("\n", " ").replace("\r", " ").strip()
                globs = parse_apply_to(safe_apply_to)

        parts = ["---"]
        if globs:
            parts.append("inclusion: fileMatch")
            if len(globs) == 1:
                parts.append(f"fileMatchPattern: {yaml_double_quote(globs[0])}")
            else:
                parts.append("fileMatchPattern:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
        else:
            parts.append("inclusion: always")
        parts.append("---")

        return "\n".join(parts) + "\n\n" + body.lstrip("\n")

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
        globs = parse_apply_to(apply_to)
        if globs:
            parts = ["---", "paths:"]
            parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
            parts.append("---")
            return "\n".join(parts) + "\n\n" + body.lstrip("\n")

        # No applyTo -> unconditional rule, return body without frontmatter
        return body.lstrip("\n")

    @staticmethod
    def _convert_to_antigravity_rules(content: str) -> str:
        """Convert APM instruction content to Antigravity CLI rules format.

        Parses existing YAML frontmatter, maps ``applyTo`` to Antigravity's
        ``trigger: glob`` + ``globs`` frontmatter.
        """
        from ..utils.yaml_io import load_yaml_str

        body = content
        globs = []

        # Parse existing frontmatter
        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = load_yaml_str(fm_match.group(1)) or {}
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to parse instruction frontmatter YAML: %s", e
                )
                fm = {}
            raw_apply_to = fm.get("applyTo", "")
            if isinstance(raw_apply_to, list):
                globs = [
                    s
                    for item in raw_apply_to
                    if (s := str(item).replace("\n", " ").replace("\r", " ").strip())
                ]
            else:
                safe_apply_to = str(raw_apply_to).replace("\n", " ").replace("\r", " ").strip()
                globs = parse_apply_to(safe_apply_to)

        # Build Antigravity rules frontmatter
        parts = ["---"]
        if globs:
            parts.append("trigger: glob")
            if len(globs) == 1:
                parts.append(f"globs: {yaml_double_quote(globs[0])}")
            else:
                parts.append("globs:")
                parts.extend(f"  - {yaml_double_quote(g)}" for g in globs)
            parts.append("---")
            return "\n".join(parts) + "\n\n" + body.lstrip("\r\n")

        # No applyTo -> unconditional rule, return body without frontmatter
        return body.lstrip("\r\n")

    def copy_instruction_claude(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Claude Code rules format.

        Converts ``applyTo:`` to ``paths:`` frontmatter and resolves links.
        """
        content, links_resolved = self._render_instruction(source, target, "claude_rules")
        write_text_lf(target, content)
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
    def sync_integration_claude(  # pylint: disable=duplicate-code  # deprecated shim; structural similarity is intentional
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
