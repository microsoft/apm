"""Instruction integration functionality for APM packages.

Deploys .instructions.md files from APM packages to the appropriate
target directory (e.g. ``.github/instructions/`` for Copilot,
``.cursor/rules/`` for Cursor, ``.claude/rules/`` for Claude Code).
Content transforms are selected by the ``format_id`` field in
``PrimitiveMapping``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from apm_cli.core.remediation import INSTRUCTION_FRONTMATTER_REJECTED
from apm_cli.integration import write_seam
from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.targets import RULE_FORMATS
from apm_cli.integration.write_seam import write_text_lf
from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.console import _rich_echo
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.patterns import (
    escape_apply_to_segment,
    normalize_apply_to,
    parse_apply_to,
    yaml_double_quote,
    yaml_globs_scalar,
    yaml_plain_scalar,
)
from apm_cli.utils.yaml_io import loads_frontmatter

if TYPE_CHECKING:
    from collections.abc import Collection

    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.cleanup import DeploymentEvidence
    from apm_cli.integration.targets import TargetProfile


def _is_universal_glob(globs: list[str]) -> bool:
    """Return True when parsed ``applyTo`` is exactly one universal ``**``."""
    return globs == ["**"]


@dataclass(frozen=True)
class _PreparedInstruction:
    """Validated instruction source reused across target plans."""

    content: str
    body: str


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

    def __init__(self):
        super().__init__()
        self._prepared_rule_plans: dict[
            tuple[str, str, str, str, str],
            dict[Path, tuple[Path, str, int]],
        ] = {}
        self._prepared_instructions: dict[str, dict[Path, _PreparedInstruction]] = {}
        # (collector, root) pairs: a planning pass reports into its own
        # throwaway collector and must not silence the real run's warning.
        self._claude_blocker_warned: set[tuple[object, Path]] = set()
        # Instruction sources this install routed to compiled AGENTS.md (#3939).
        self.agents_md_routed: set[str] = set()
        # Routed sources no compiled AGENTS.md carries yet (new or changed).
        self.agents_md_stale: set[str] = set()
        # One compiled-coverage snapshot per project root for this install.
        self._compiled_coverage: dict[Path, Any] = {}
        # Routing verdicts per root and directory for this install (#4787).
        self._routing_cache: dict = {}

    # Map format_id -> converter method.  Built once at class load time;
    # avoids rebuilding the dict on every ``_render_instruction`` call.
    _FORMAT_CONVERTERS: ClassVar[dict[str, str]] = {
        "cursor_rules": "_convert_to_cursor_rules",
        "claude_rules": "_convert_to_claude_rules",
        "windsurf_rules": "_convert_to_windsurf_rules",
        "kiro_steering": "_convert_to_kiro_steering",
        "antigravity_rules": "_convert_to_antigravity_rules",
    }

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        """Return bounded frontmatter metadata and body for instruction text."""
        post = loads_frontmatter(content, preserve_body=True)
        metadata = post.metadata if isinstance(post.metadata, dict) else {}
        return metadata, post.content

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Strip YAML frontmatter from instruction content.

        Returns only the body text following the closing ``---`` delimiter.
        If no frontmatter is present, returns the content unchanged.
        Handles both LF and CRLF line endings.
        """
        _, body = InstructionIntegrator._parse_frontmatter(content)
        return body

    def find_instruction_files(self, package_path: Path, source_plan=None) -> list[Path]:
        """Find all .instructions.md files in a package.

        Searches in .apm/instructions/ subdirectory.
        """
        return self.filter_authorized_files(
            self.find_files_by_glob(
                package_path,
                "*.instructions.md",
                subdirs=[".apm/instructions"],
            ),
            source_plan,
        )

    def copy_instruction(self, source: Path, target: Path, content: str | None = None) -> int:
        """Copy instruction file with link resolution.

        Preserves applyTo: frontmatter and all content as-is.
        """
        content = content if content is not None else source.read_text(encoding="utf-8")
        content, links_resolved = self.resolve_links(content, source, target)
        write_text_lf(target, content)
        return links_resolved

    @staticmethod
    def _package_key(package_path: Path) -> str:
        """Return the stable cache key for one package source tree."""
        return str(package_path.resolve())

    @staticmethod
    def _raise_frontmatter_error(source: Path, exc: yaml.YAMLError) -> None:
        """Raise the package-facing malformed-frontmatter error."""
        raise yaml.YAMLError(
            f"Rejected frontmatter in {source.name}: {exc}\n"
            f"{INSTRUCTION_FRONTMATTER_REJECTED.sentence()}"
        ) from exc

    def _prepare_instruction(
        self,
        source: Path,
        *,
        force: bool = False,
        diagnostics=None,
        package_name: str = "",
    ) -> _PreparedInstruction:
        """Read and validate one instruction without writing target files."""
        from apm_cli.security.gate import BLOCK_POLICY, SecurityGate

        content = source.read_text(encoding="utf-8")
        try:
            metadata, body = self._parse_frontmatter(content)
        except yaml.YAMLError as exc:
            self._raise_frontmatter_error(source, exc)
        decoded_metadata = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
        verdict = SecurityGate.scan_text(
            decoded_metadata,
            f"{source}#decoded-frontmatter",
            policy=BLOCK_POLICY,
            force=force,
        )
        if verdict.has_findings and diagnostics is not None:
            SecurityGate.report(
                verdict,
                diagnostics,
                package=package_name,
                force=force,
                force_action="Allowed by preflight",
                force_detail=(
                    "Decoded frontmatter contains critical hidden characters; "
                    f"edit the escaped value in {source.name}"
                ),
            )
        if verdict.should_block:
            raise yaml.YAMLError(
                f"Rejected decoded frontmatter in {source.name}: critical hidden Unicode characters"
            )
        return _PreparedInstruction(content=content, body=body)

    def _render_instruction(
        self,
        source: Path,
        target: Path,
        fmt: str,
        *,
        prepared: _PreparedInstruction | None = None,
    ) -> tuple[str, int]:
        """Render *source* to the content it would deploy for *fmt*, WITHOUT
        writing. Returns ``(content, links_resolved)``.

        Used both by the ``copy_instruction_*`` writers and by the rule-dir
        deploy loop to decide adopt-vs-rewrite against the *transformed* output
        (a format-transformed rule is never byte-identical to its source).

        The transforming formats are exactly :data:`targets.RULE_FORMATS` --
        the single home for "which formats transform".  Any ``fmt`` outside it
        is copied verbatim (identity transform).
        """
        content = prepared.content if prepared is not None else source.read_text(encoding="utf-8")
        if fmt in RULE_FORMATS:
            converter = getattr(self, self._FORMAT_CONVERTERS[fmt])
            try:
                content = converter(content)
            except yaml.YAMLError as exc:
                self._raise_frontmatter_error(source, exc)
        content, links_resolved = self.resolve_links(content, source, target)
        return content, links_resolved

    @staticmethod
    def _plan_key(
        package_path: Path,
        project_root: Path,
        deploy_dir: Path,
        extension: str,
        fmt: str,
    ) -> tuple[str, str, str, str, str]:
        """Return the per-package, per-target identity for a prepared rule plan."""
        return (
            str(package_path.resolve()),
            str(project_root.resolve()),
            str(deploy_dir.resolve()),
            extension,
            fmt,
        )

    def _prepare_rule_plan(
        self,
        instruction_files: list[Path],
        deploy_dir: Path,
        extension: str,
        fmt: str,
        prepared_instructions: dict[Path, _PreparedInstruction] | None = None,
    ) -> dict[Path, tuple[Path, str, int]]:
        """Render every converted rule without writing target files."""
        plan: dict[Path, tuple[Path, str, int]] = {}
        for source_file in instruction_files:
            stem = source_file.name
            if stem.endswith(".instructions.md"):
                stem = stem[: -len(".instructions.md")]
            target_path = deploy_dir / f"{stem}{extension}"
            ensure_path_within(target_path, deploy_dir)
            content, links_resolved = self._render_instruction(
                source_file,
                target_path,
                fmt,
                prepared=(prepared_instructions or {}).get(source_file),
            )
            plan[source_file] = (target_path, content, links_resolved)
        return plan

    def deployed_rule_matches(self, source: Path, target: TargetProfile, deploy_root: Path) -> bool:
        """Prove native delivery using the same filename and renderer as install.

        Without a package link resolver, rewritten links conservatively retain
        the compiled fallback rather than guessing semantic equivalence.
        Filesystem and parse errors are reported by the compilation caller.
        """
        mapping = target.primitives.get("instructions")
        if mapping is None or not mapping.output_compare:
            return False
        rules_dir = deploy_root / mapping.subdir
        ensure_path_within(rules_dir, deploy_root)
        if not rules_dir.is_dir():
            return False
        plan = self._prepare_rule_plan([source], rules_dir, mapping.extension, mapping.format_id)
        rule_path, expected, _ = plan[source]
        ensure_path_within(rule_path, deploy_root)
        if has_symlink_component(deploy_root, rule_path):
            return False
        if not rule_path.is_file():
            return False
        return rule_path.read_text(encoding="utf-8") == normalize_crlf_to_lf(expected)

    def preflight_instructions_for_targets(
        self,
        targets: list[TargetProfile],
        package_info,
        project_root: Path,
        source_plan: DeployableSourcePlan,
        *,
        force: bool = False,
        diagnostics=None,
    ) -> None:
        """Validate authorized instructions and prepare conversions before writes."""
        self._prepared_rule_plans.clear()
        self._prepared_instructions.clear()
        eligible_targets = []
        for target in targets:
            mapping = target.primitives.get("instructions")
            if not mapping:
                continue
            if not target.auto_create and not (project_root / target.root_dir).is_dir():
                continue
            eligible_targets.append((target, mapping))
        if not eligible_targets:
            return

        package_path = Path(package_info.install_path)
        instruction_files = self.find_instruction_files(package_path, source_plan)
        if not instruction_files:
            return

        self.init_link_resolver(package_info, project_root)
        package_name = getattr(getattr(package_info, "package", None), "name", "")
        prepared_instructions = {
            source_file: self._prepare_instruction(
                source_file,
                force=force,
                diagnostics=diagnostics,
                package_name=package_name,
            )
            for source_file in instruction_files
        }
        self._prepared_instructions[self._package_key(package_path)] = prepared_instructions
        for target, mapping in eligible_targets:
            if not mapping.output_compare:
                continue
            effective_root = mapping.deploy_root or target.root_dir
            deploy_dir = project_root / effective_root / mapping.subdir
            key = self._plan_key(
                package_path,
                project_root,
                deploy_dir,
                mapping.extension,
                mapping.format_id,
            )
            self._prepared_rule_plans[key] = self._prepare_rule_plan(
                instruction_files,
                deploy_dir,
                mapping.extension,
                mapping.format_id,
                prepared_instructions,
            )

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
        source_plan=None,
    ) -> IntegrationResult:
        """Integrate instructions for a single *target*.

        Selects the content transform via ``format_id``:

        * ``cursor_rules``    -- convert ``applyTo:`` to ``globs:`` frontmatter
        * ``claude_rules``    -- convert ``applyTo:`` to ``paths:`` frontmatter
        * ``windsurf_rules``  -- convert ``applyTo:`` to ``trigger: glob`` frontmatter
        * anything else       -- copy verbatim (identity transform)

        Every format shares one collision decision: an existing file that
        matches the rendered output is adopted, a ``managed_files`` path is
        rewritten, and any other existing file is user-authored and skipped
        unless ``force``. Rule-dir targets (``mapping.output_compare``) compare
        against their transformed output rather than the source (apm#1662).
        """
        mapping = target.primitives.get("instructions")
        if not mapping:
            return IntegrationResult(0, 0, 0, [])

        effective_root = mapping.deploy_root or target.root_dir
        target_root = project_root / effective_root
        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            return IntegrationResult(0, 0, 0, [])

        self.init_link_resolver(package_info, project_root)
        package_path = Path(package_info.install_path)
        prepared_instructions = self._prepared_instructions.get(self._package_key(package_path))
        instruction_files = (
            list(prepared_instructions)
            if prepared_instructions is not None
            else self.find_instruction_files(package_path, source_plan)
        )
        if not instruction_files:
            return IntegrationResult(0, 0, 0, [])
        if prepared_instructions is None:
            package_name = getattr(getattr(package_info, "package", None), "name", "")
            prepared_instructions = {
                source_file: self._prepare_instruction(
                    source_file,
                    force=force,
                    diagnostics=diagnostics,
                    package_name=package_name,
                )
                for source_file in instruction_files
            }
        instruction_files = self._claude_rule_sources(
            target,
            instruction_files,
            prepared_instructions,
            package_info,
            project_root,
            scope,
            diagnostics,
        )
        if not instruction_files:
            return IntegrationResult(0, 0, 0, [])

        deploy_dir = target_root / mapping.subdir
        write_seam.mkdir(deploy_dir, parents=True, exist_ok=True)

        fmt = mapping.format_id

        # Rule dirs (.claude/rules, .cursor/rules, .windsurf/rules): the
        # deployed file is a format-transform of its source with a derived
        # filename. Gates the rename and the pre-rendered output comparison.
        # Single source of truth: mapping.output_compare (targets.py).
        rule_dir_format = mapping.output_compare
        breadcrumbs = rule_dir_format and bool(getattr(diagnostics, "verbose", False))

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []
        total_links_resolved = 0
        plan_key = self._plan_key(
            package_path,
            project_root,
            deploy_dir,
            mapping.extension,
            fmt,
        )
        rendered_rules = self._prepared_rule_plans.pop(plan_key, None)
        if rule_dir_format:
            # Direct integrator callers may bypass install-level preflight.
            if rendered_rules is None:
                rendered_rules = self._prepare_rule_plan(
                    instruction_files,
                    deploy_dir,
                    mapping.extension,
                    fmt,
                    prepared_instructions,
                )
        else:
            rendered_rules = {}

        for source_file in instruction_files:
            if rule_dir_format:
                target_path, new_content, links_resolved = rendered_rules[source_file]
            else:
                target_path = deploy_dir / source_file.name
                # source_file.name has no separators, so traversal via
                # deploy_dir is impossible. Validate against deploy_dir (not
                # project_root) so user-scope targets outside the workspace work.
                ensure_path_within(target_path, deploy_dir)
                new_content, links_resolved = self._render_instruction(
                    source_file,
                    target_path,
                    fmt,
                    prepared=prepared_instructions[source_file],
                )

            rel_path = portable_relpath(target_path, project_root)

            # Compare against the rendered output, not the source: rule-dir
            # formats transform content, so a source-based adopt never matches
            # and up-to-date rules would churn (apm#1662). Managed rules are
            # rewritten; an unmanaged differing file is user-authored and is
            # skipped like every other primitive unless --force.
            skip, adopted = self._check_adopt_or_skip(
                target_path,
                source_file,
                rel_path,
                managed_files,
                force,
                diagnostics,
                target_paths,
                expected_content=new_content,
            )
            if skip:
                if adopted:
                    files_adopted += 1
                    if breadcrumbs:
                        _rich_echo(f"  [=] adopted-unchanged: {rel_path}", color="dim")
                else:
                    files_skipped += 1
                continue

            write_text_lf(target_path, new_content)
            self.note_rendered_write(target_path)
            total_links_resolved += links_resolved
            files_integrated += 1
            target_paths.append(target_path)
            if breadcrumbs:
                _rich_echo(f"  [*] rewritten: {rel_path}", color="dim")

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=total_links_resolved,
            files_adopted=files_adopted,
        )

    def _claude_rule_sources(
        self,
        target: TargetProfile,
        instruction_files: list[Path],
        prepared_instructions: dict[Path, _PreparedInstruction],
        package_info,
        project_root: Path,
        scope,
        diagnostics,
    ) -> list[Path]:
        """Drop project instructions Claude loads from AGENTS.md (#3939).

        Always-on and single-directory instructions reach Claude Code through
        the compiled ``AGENTS.md`` that ``CLAUDE.md`` imports, so only the
        remaining path-scoped instructions become ``.claude/rules/`` files.
        Routed sources are recorded so install can ask for ``apm compile``.
        User scope and projects with a blocking hand-authored root file keep
        every instruction in ``.claude/rules/``. Drift replay writes to a
        scratch root, so scope and the blocker come from the live root it replays.
        """
        from apm_cli.compilation.claude_agents_md import (
            CompiledCoverage,
            claude_agents_md_blocker,
            claude_agents_md_blocker_warning,
            routes_to_agents_md,
        )
        from apm_cli.core.scope import InstallScope, get_deploy_root, is_user_scope

        if target.compile_family != "claude" or is_user_scope(scope):
            return instruction_files
        frame = getattr(package_info, "deployment_frame", None)
        live_root = Path(
            frame[1]
            if frame is not None
            else getattr(package_info, "root_local_project_root", None) or project_root
        )
        if live_root.resolve() == get_deploy_root(InstallScope.USER).resolve():
            return instruction_files
        routed = {
            source
            for source in instruction_files
            if routes_to_agents_md(
                self._parse_frontmatter(prepared_instructions[source].content)[0].get("applyTo"),
                live_root,
                cache=self._routing_cache,
            )
        }
        if not routed:
            return instruction_files
        blocker = claude_agents_md_blocker(live_root, cache=self._routing_cache)
        if blocker is not None:
            warned = (diagnostics, live_root)
            if diagnostics is not None and warned not in self._claude_blocker_warned:
                self._claude_blocker_warned.add(warned)
                diagnostics.warn(claude_agents_md_blocker_warning(blocker))
            return instruction_files
        self.agents_md_routed.update(source.name for source in routed)
        coverage = self._compiled_coverage.setdefault(live_root, CompiledCoverage(live_root))
        for source in routed:
            prepared = prepared_instructions[source]
            apply_to = self._parse_frontmatter(prepared.content)[0].get("applyTo")
            if not coverage.has(apply_to, prepared.body):
                self.agents_md_stale.add(source.name)
        return [source for source in instruction_files if source not in routed]

    def render_agents_md_routed_rules(
        self, package_info, project_root: Path, source_plan
    ) -> dict[str, str]:
        """Render the ``.claude/rules/`` files Claude routing left undeployed.

        ``apm pack --format apm`` ships deployed files, and bundle consumers do
        not compile, so a pack still carries each instruction that #3939 routed
        to ``AGENTS.md`` as the rule a pre-#3939 install deployed. Routing and
        rendering reuse this integrator's install decision and projection.
        Returns ``{project-relative rule path: content}``.
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        target = KNOWN_TARGETS["claude"]
        mapping = target.primitives["instructions"]
        package_path = Path(package_info.install_path)
        # Only the sources install admits (the canonical source plan, #4787).
        sources = self.find_instruction_files(package_path, source_plan)
        if not sources:
            return {}
        self.init_link_resolver(package_info, project_root)
        prepared = {source: self._prepare_instruction(source) for source in sources}
        kept = set(
            self._claude_rule_sources(
                target, sources, prepared, package_info, project_root, None, None
            )
        )
        routed = [source for source in sources if source not in kept]
        if not routed:
            return {}
        deploy_dir = project_root / target.root_dir / mapping.subdir
        plan = self._prepare_rule_plan(
            routed, deploy_dir, mapping.extension, mapping.format_id, prepared
        )
        return {
            portable_relpath(rule_path, project_root): normalize_crlf_to_lf(content)
            for rule_path, content, _links in plan.values()
        }

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set[str] | None = None,
        evidence: DeploymentEvidence | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed instruction files for a single *target*."""
        mapping = target.primitives.get("instructions")
        if not mapping:
            return {"files_removed": 0, "errors": 0}
        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        # No legacy glob fallback: without a lockfile inventory every native
        # instruction directory may hold user-authored files, and uninstall
        # deletes only lockfile-tracked paths.
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix=prefix,
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

        Map ``applyTo`` to one comma-joined ``globs`` scalar and preserve
        or derive the description. Ordinary globs stay bare; unsafe
        characters are escaped. Re-escape each segment before joining
        to preserve APM's literal-comma and backslash boundaries.
        Exactly one universal ``**`` pattern becomes Cursor's native
        ``alwaysApply: true`` with no ``globs`` key (issue #1744).
        """
        metadata, body = InstructionIntegrator._parse_frontmatter(content)
        apply_to = normalize_apply_to(metadata.get("applyTo"), default="")
        raw_description = metadata.get("description")
        description = "" if raw_description is None else str(raw_description).strip()

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
            parts.append(f"description: {yaml_plain_scalar(description)}")
        globs = parse_apply_to(apply_to)
        if _is_universal_glob(globs):
            parts.append("alwaysApply: true")
        elif globs:
            joined = ", ".join(escape_apply_to_segment(g) for g in globs)
            parts.append(f"globs: {yaml_globs_scalar(joined)}")
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

        Parses existing YAML frontmatter through the canonical bounded loader, maps
        ``applyTo`` to Windsurf's ``trigger: glob`` + ``globs`` frontmatter.
        Instructions without ``applyTo`` become ``trigger: always_on`` rules.

        Ref: https://docs.windsurf.com/windsurf/cascade/memories
        """
        metadata, body = InstructionIntegrator._parse_frontmatter(content)
        apply_to = normalize_apply_to(metadata.get("applyTo"), default="")

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
        metadata, body = InstructionIntegrator._parse_frontmatter(content)
        apply_to = normalize_apply_to(metadata.get("applyTo"), default="")
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
        metadata, body = InstructionIntegrator._parse_frontmatter(content)
        apply_to = normalize_apply_to(metadata.get("applyTo"), default="")

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

        Parses existing YAML frontmatter and maps ``applyTo`` to
        ``trigger: glob`` plus ONE double-quoted, comma-separated ``globs``
        string (Antigravity rejects a YAML sequence). Each segment is
        re-escaped before joining to preserve APM's literal-comma and
        backslash boundaries. Instructions without ``applyTo`` become
        ``trigger: always_on`` rules, since Antigravity discards rule files
        lacking a valid ``trigger``.

        Ref: https://antigravity.google/docs/rules
        """
        metadata, body = InstructionIntegrator._parse_frontmatter(content)
        apply_to = normalize_apply_to(metadata.get("applyTo"), default="")
        safe_apply_to = apply_to.replace("\n", " ").replace("\r", " ").strip()
        globs = parse_apply_to(safe_apply_to)

        parts = ["---"]
        if globs:
            joined = ", ".join(escape_apply_to_segment(g) for g in globs)
            parts.append("trigger: glob")
            parts.append(f"globs: {yaml_double_quote(joined)}")
        else:
            parts.append("trigger: always_on")
        parts.append("---")
        return "\n".join(parts) + "\n\n" + body.lstrip("\r\n")

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


def routed_claude_rule_renders(
    lockfile,
    project_root: Path,
    modules_root: Path,
    *,
    include_local: bool,
    verified: bool = False,
    dependency_keys: Collection[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return ``{dependency key: {rule path: content}}`` #3939 routed to AGENTS.md.

    Each locked dependency installed under *modules_root* is rendered through
    :meth:`InstructionIntegrator.render_agents_md_routed_rules`: the
    ``.claude/rules/`` files a pre-#3939 install deployed for it. Pack ships
    them; cleanup proves a legacy rule file against them. A dependency whose
    edge or own ``targets:`` exclude Claude renders nothing, as at install.
    *dependency_keys*, when given, limits rendering to the dependencies the
    caller ships (the plugin pack skips dev dependencies). With *verified*
    (pack), only rules the lock does not already record as deployed are
    returned. ``apm_modules`` is not attested: a cached copy whose lock row
    records a ``content_hash`` must match it before its manifest or its
    instructions (or their absence) decide routing, so a deleted routed
    instruction fails too. An older row without one renders nothing it
    does not already record as deployed. A missing cached copy of an
    attested dependency fails too unless the lock proves it gives Claude
    nothing (:func:`_lock_proves_claude_excluded`).
    """
    from apm_cli.core.scope import manifest_file
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.install.package_resolution import effective_dependency_selection
    from apm_cli.install.target_filter import resolve_effective_package_targets
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    rules: dict[str, dict[str, str]] = {}
    for dep in lockfile.get_all_dependencies():
        if dep.source == "local" and not include_local:
            continue
        if dependency_keys is not None and dep.get_unique_key() not in dependency_keys:
            continue
        dep_ref = dep.to_dependency_ref()
        install_path = dep_ref.get_install_path(modules_root)
        attested = bool(getattr(dep, "content_hash", None))
        if not install_path.is_dir():
            if verified and attested and not _lock_proves_claude_excluded(dep, dep_ref):
                _refuse_missing_source(dep, install_path, project_root)
            continue
        if verified and attested:
            _require_locked_source(lockfile, dep, install_path, project_root)
        manifest = manifest_file(install_path)
        package = (
            APMPackage.from_apm_yml(manifest)
            if manifest.is_file()
            else APMPackage(name=dep.name or install_path.name, version=dep.version or "0.0.0")
        )
        dependency_selection = effective_dependency_selection(dep_ref)
        selection = resolve_effective_package_targets(
            [KNOWN_TARGETS["claude"]],
            dependency_selection.target_subset,
            package,
            None,
            dep.get_unique_key(),
        )
        if not selection.targets:
            continue
        package_info = PackageInfo(
            package=package, install_path=install_path, dependency_ref=dep_ref
        )
        source_plan = DeployableSourcePlan.create(
            package_info,
            [KNOWN_TARGETS["claude"]],
            skill_subset=dependency_selection.skill_subset,
            hooks_approved=False,
            canvas_approved=False,
            skip_bin=True,
        )
        rendered = InstructionIntegrator().render_agents_md_routed_rules(
            package_info, project_root, source_plan
        )
        if verified:
            deployed = set(dep.deployed_files)
            rendered = {path: text for path, text in rendered.items() if path not in deployed}
            if rendered and not attested:
                _require_locked_source(lockfile, dep, install_path, project_root)
        if rendered:
            rules[dep.get_unique_key()] = rendered
    return rules


def _lock_proves_claude_excluded(dep, dep_ref) -> bool:
    """Return True when the lock records that *dep* excludes Claude.

    Without its cached copy, pack cannot read the dependency's ``targets:``
    or instructions, so only a recorded exclusion is authoritative: the
    edge's target subset, or the package's own ``targets:`` install recorded
    (#4850), judged by the same rule install applies. A missing Claude
    deployment proves nothing: a collision can block one target while another
    deploys, and Claude lacks some primitives.
    """
    from apm_cli.install.package_resolution import effective_dependency_selection
    from apm_cli.install.target_filter import recorded_targets_exclude
    from apm_cli.integration.targets import KNOWN_TARGETS

    subset = effective_dependency_selection(dep_ref).target_subset
    if subset and "claude" not in subset:
        return True
    recorded = getattr(dep, "package_targets", None) or ()
    return recorded_targets_exclude(KNOWN_TARGETS["claude"], recorded)


def _refuse_missing_source(dep, install_path: Path, project_root: Path) -> None:
    """Refuse to pack a Claude dependency whose cached copy is gone (#4755)."""
    from apm_cli.core.remediation import PACK_INSTALLED_FILES_MISSING

    cached = portable_relpath(install_path, project_root)
    raise ValueError(
        f"Cannot pack dependency {dep.repo_url}: its installed copy {cached} is missing, so "
        "its Claude instructions routed to AGENTS.md cannot be rendered into the bundle. "
        f"{PACK_INSTALLED_FILES_MISSING.sentence()}"
    )


def _require_locked_source(lockfile, dep, install_path: Path, project_root: Path) -> None:
    """Refuse to render from a cached copy its locked content hash does not attest."""
    from apm_cli.core.remediation import PACK_DEPENDENCY_UNATTESTED, PACK_INSTALLED_FILE_TAMPERED
    from apm_cli.install.source_helpers import locked_content_mismatch

    if not getattr(dep, "content_hash", None):
        raise ValueError(
            f"Cannot pack dependency {dep.repo_url}: apm.lock.yaml records no content hash "
            "for it, so its Claude rules cannot be rendered from the apm_modules cache. "
            f"{PACK_DEPENDENCY_UNATTESTED.sentence()}"
        )
    key = dep.get_unique_key()
    if locked_content_mismatch(key, dep, install_path, lockfile=lockfile):
        cached = portable_relpath(install_path, project_root)
        raise ValueError(
            f"Cannot pack dependency {dep.repo_url}: {cached} does not match the content "
            "hash recorded in apm.lock.yaml, so its Claude rules cannot be rendered. "
            f"{PACK_INSTALLED_FILE_TAMPERED.bind(path=cached).sentence()}"
        )
