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
        target.write_text(content, encoding="utf-8")
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

        if fmt == "copilot_user_instructions":
            return self._integrate_copilot_user_instructions(
                instruction_files,
                deploy_dir,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                pkg_source=getattr(getattr(package_info, "package", None), "source", None),
            )

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
                if (
                    not force
                    and target_path.exists()
                    and target_path.read_text(encoding="utf-8") == new_content
                ):
                    files_adopted += 1
                    target_paths.append(target_path)
                    if diagnostics is not None and getattr(diagnostics, "verbose", False):
                        _rich_echo(f"  [=] adopted-unchanged: {rel_path}", color="dim")
                    continue
                target_path.write_text(new_content, encoding="utf-8")
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
        if mapping.format_id == "copilot_user_instructions":
            prefix = f"{effective_root}/copilot-instructions.md"
            legacy_pattern = None
            legacy_dir = None
        else:
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
    # Copilot user-scope concat support
    # ------------------------------------------------------------------

    # Sentinel line written as the first line of every APM-managed
    # copilot-instructions.md.  Its presence distinguishes the file from
    # a user-authored one, enabling multi-package accumulation without
    # collision false-positives.
    _APM_COPILOT_HEADER: str = "<!-- apm-managed: copilot-instructions.md -->"

    # Matches a single package's provenance-marked section.
    _APM_SOURCE_RE: re.Pattern[str] = re.compile(
        r"<!-- apm:source:(?P<source>[^>]*?) -->\n(?P<body>.*?)<!-- /apm:source -->",
        re.DOTALL,
    )

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Strip YAML frontmatter from instruction content.

        Returns only the body text following the closing ``---`` delimiter.
        If no frontmatter is present, returns the content unchanged.
        Handles both LF and CRLF line endings.
        """
        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            return content[fm_match.end() :]
        return content

    @classmethod
    def _is_apm_managed_copilot(cls, content: str) -> bool:
        """Return True if *content* starts with the APM managed-file sentinel."""
        return content.startswith(cls._APM_COPILOT_HEADER)

    @classmethod
    def _build_copilot_section(cls, pkg_source: str, body: str) -> str:
        """Wrap *body* in APM provenance markers for *pkg_source*."""
        # Sanitize source so it cannot accidentally close the HTML comment.
        safe_source = pkg_source.replace("-->", "__")
        return f"<!-- apm:source:{safe_source} -->\n{body}\n<!-- /apm:source -->"

    @classmethod
    def _update_copilot_managed(cls, existing: str, pkg_source: str, section: str) -> str:
        """Replace or append *section* in the APM-managed file content.

        If a section for *pkg_source* already exists, it is replaced in-place
        so that the file stays ordered and does not grow on re-install.
        Otherwise the section is appended.
        """
        for m in cls._APM_SOURCE_RE.finditer(existing):
            if m.group("source") == pkg_source.replace("-->", "__"):
                return existing[: m.start()] + section + existing[m.end() :]
        # Not yet present: append after stripping trailing newlines.
        return existing.rstrip("\n") + "\n\n" + section + "\n"

    def _integrate_copilot_user_instructions(
        self,
        instruction_files: list[Path],
        deploy_dir: Path,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
        pkg_source: str | None = None,
    ) -> IntegrationResult:
        """Concatenate all instruction files into ~/.copilot/copilot-instructions.md.

        Copilot CLI at user scope reads a single file rather than individual
        ``*.instructions.md`` files.  This method strips YAML frontmatter from
        each source file, wraps the combined body in a per-package provenance
        marker, and accumulates sections across packages in the same file so
        that multi-package installs are fully represented.

        File ownership logic:
        - APM-managed (starts with ``_APM_COPILOT_HEADER``): always update --
          either replace this package's existing section or append a new one.
          This path is taken even when the file is not in *managed_files*,
          allowing a second package in the same install session to contribute
          without a false collision.
        - In *managed_files* but no header (pre-provenance format): upgrade to
          the sectioned format in-place.
        - User-authored (no header, not in *managed_files*, not *force*):
          collision -- skip and warn.
        - *force* is True: overwrite any existing content.
        """
        target_path = deploy_dir / "copilot-instructions.md"
        ensure_path_within(target_path, deploy_dir)
        rel_path = portable_relpath(target_path, project_root)

        bodies: list[str] = []
        for source_file in instruction_files:
            raw = source_file.read_text(encoding="utf-8")
            body = self._strip_frontmatter(raw).strip()
            if body:
                bodies.append(body)

        if not bodies:
            return IntegrationResult(0, 0, 0, [])

        deploy_dir.mkdir(parents=True, exist_ok=True)
        combined_body = "\n\n".join(bodies)
        section = self._build_copilot_section(pkg_source or "unknown", combined_body)

        if target_path.exists():
            existing = target_path.read_text(encoding="utf-8")
            if self._is_apm_managed_copilot(existing):
                # APM-managed: update or append this package's provenance section.
                updated = self._update_copilot_managed(existing, pkg_source or "unknown", section)
                target_path.write_text(updated, encoding="utf-8")
                return IntegrationResult(1, 0, 0, [target_path])
            norm_rel = rel_path.replace("\\", "/")
            if norm_rel in (managed_files or set()) or force:
                # Either was managed on a previous run (pre-provenance format)
                # or caller explicitly requested overwrite.
                new_content = self._APM_COPILOT_HEADER + "\n" + section + "\n"
                target_path.write_text(new_content, encoding="utf-8")
                return IntegrationResult(1, 0, 0, [target_path])
            # User-authored file: emit collision warning and skip.
            self.check_collision(
                target_path, rel_path, managed_files, force, diagnostics=diagnostics
            )
            return IntegrationResult(0, 0, 1, [])

        new_content = self._APM_COPILOT_HEADER + "\n" + section + "\n"
        target_path.write_text(new_content, encoding="utf-8")
        return IntegrationResult(1, 0, 0, [target_path])

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
        import yaml

        body = content
        apply_to = ""

        # Parse existing frontmatter with yaml.safe_load for consistency with the other frontmatter parsers across integrators.
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
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
        target.write_text(content, encoding="utf-8")
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
        import yaml

        body = content
        apply_to = ""

        fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", content, re.DOTALL)
        if fm_match:
            body = content[fm_match.end() :]
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                fm = {}
            raw_apply_to = fm.get("applyTo", "")
            if isinstance(raw_apply_to, list):
                apply_to = ",".join(str(item) for item in raw_apply_to)
            else:
                apply_to = str(raw_apply_to).strip()

        safe_apply_to = apply_to.replace("\n", " ").replace("\r", " ").strip()
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

        Strips YAML frontmatter (Antigravity rules are plain markdown with
        no frontmatter) and returns the body as-is.
        """
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
        if fm_match:
            return fm_match.string[fm_match.end() :].lstrip("\n")
        return content

    def copy_instruction_claude(self, source: Path, target: Path) -> int:
        """Copy instruction file converted to Claude Code rules format.

        Converts ``applyTo:`` to ``paths:`` frontmatter and resolves links.
        """
        content, links_resolved = self._render_instruction(source, target, "claude_rules")
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
