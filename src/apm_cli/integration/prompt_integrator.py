"""Prompt integration functionality for APM packages."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set  # noqa: F401, UP035

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


class PromptIntegrator(BaseIntegrator):
    """Handles integration of APM package prompts into .github/prompts/."""

    def find_prompt_files(self, package_path: Path) -> list[Path]:
        """Find all .prompt.md files in a package.

        Searches in:
        - Package root directory
        - .apm/prompts/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to .prompt.md files
        """
        return self.find_files_by_glob(package_path, "*.prompt.md", subdirs=[".apm/prompts"])

    def copy_prompt(self, source: Path, target: Path) -> int:
        """Copy prompt file verbatim with link resolution.

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

    def get_target_filename(self, source_file: Path, package_name: str) -> str:
        """Generate target filename (clean, no suffix).

        Args:
            source_file: Source file path
            package_name: Name of the package (not used in simple naming)

        Returns:
            str: Target filename (e.g., accessibility-audit.prompt.md)
        """
        # Use original filename  -- no -apm suffix
        return source_file.name

    # ------------------------------------------------------------------
    # Target-driven API (data-driven dispatch)
    # ------------------------------------------------------------------

    def integrate_prompts_for_target(
        self,
        target: TargetProfile,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
    ) -> IntegrationResult:
        """Integrate prompts for a single *target*."""
        mapping = target.primitives.get("prompts")
        if not mapping:
            return IntegrationResult(0, 0, 0, [])

        # GitHub Copilot desktop App: deploy to SQLite instead of files.
        # The branch fully owns lifecycle for this target -- it does not
        # share the file-based collision / link-resolution machinery.
        if target.name == "copilot-app":
            return self._integrate_prompts_for_copilot_app(
                target,
                package_info,
                force=force,
                diagnostics=diagnostics,
            )

        if not target.auto_create and not (project_root / target.root_dir).is_dir():
            return IntegrationResult(0, 0, 0, [])

        return self.integrate_package_prompts(
            package_info,
            project_root,
            force=force,
            managed_files=managed_files,
            diagnostics=diagnostics,
        )

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed prompt files for a single *target*."""
        mapping = target.primitives.get("prompts")
        if not mapping:
            return {"files_removed": 0, "errors": 0}

        if target.name == "copilot-app":
            return self._sync_copilot_app(managed_files or set())

        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        legacy_dir = project_root / effective_root / mapping.subdir
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix=prefix,
            legacy_glob_dir=legacy_dir,
            legacy_glob_pattern="*-apm.prompt.md",
            targets=[target],
        )

    # ------------------------------------------------------------------
    # copilot-app SQLite path
    # ------------------------------------------------------------------

    def _integrate_prompts_for_copilot_app(
        self,
        target: TargetProfile,
        package_info,
        *,
        force: bool,
        diagnostics,
    ) -> IntegrationResult:
        """Deploy ``schedule:``-bearing prompts as Copilot App workflow rows.

        Prompts WITHOUT a ``schedule:`` block are silently skipped at
        the copilot-app target -- the target is opt-in via frontmatter
        so unscheduled prompts continue to deploy to file-based targets
        (``copilot``, ``vscode``, ...) without surprising side effects
        here.

        The DB module enforces ``enabled = 0`` on insert; the source
        ``schedule.enabled`` field, if present, is ignored.  This is a
        hard contract: third-party packages cannot auto-run anything on
        the user's machine.
        """
        import frontmatter

        from apm_cli.integration.copilot_app_db import (
            CopilotAppDbError,
            WorkflowRow,
            deploy_workflow,
            namespaced_id,
            resolve_copilot_app_db_path,
        )

        db_path = resolve_copilot_app_db_path()
        if db_path is None:
            # Surfaced as an actionable error by install/phases/targets.py
            # when --target copilot-app was explicit; here we are
            # defensive in case the resolver returns None mid-run.
            return IntegrationResult(0, 0, 0, [])

        owner = _derive_package_owner(package_info)
        pkg_name = package_info.package.name

        files_integrated = 0
        files_skipped = 0
        target_paths: list[Path] = []
        synthetic_root = db_path.parent / "workflows"

        for source_file in self.find_prompt_files(package_info.install_path):
            if source_file.is_symlink():
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Refusing to read symlink prompt: {source_file}",
                        package=pkg_name,
                    )
                files_skipped += 1
                continue
            post = frontmatter.load(str(source_file))
            schedule_block = post.metadata.get("schedule")
            if schedule_block is None:
                files_skipped += 1
                continue
            try:
                schedule = _parse_schedule(schedule_block)
            except ValueError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Invalid schedule in {source_file.name}: {exc}",
                        package=pkg_name,
                    )
                files_skipped += 1
                continue
            prompt_stem = source_file.name.removesuffix(".prompt.md")
            wf_id = namespaced_id(owner, pkg_name, prompt_stem)
            display_name = post.metadata.get("name") or prompt_stem
            row = WorkflowRow(
                id=wf_id,
                name=str(display_name),
                prompt=post.content,
                interval=schedule.interval,
                schedule_hour=schedule.schedule_hour,
                schedule_day=schedule.schedule_day,
                enabled=0,  # ALWAYS disabled on install -- contract.
                model=schedule.model,
                reasoning_effort=schedule.reasoning_effort,
                mode=schedule.mode,
            )
            try:
                deploy_workflow(db_path, row)
            except CopilotAppDbError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Could not deploy {prompt_stem!r} to Copilot App: {exc}",
                        package=pkg_name,
                    )
                files_skipped += 1
                continue
            files_integrated += 1
            # Synthetic path used purely for lockfile encoding -- the
            # services._deployed_path_entry copilot-app branch will
            # convert this to a ``copilot-app-db://`` URI.
            target_paths.append(synthetic_root / wf_id)

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=0,
            files_adopted=0,
        )

    def _sync_copilot_app(self, managed_files: set[str]) -> dict[str, int]:
        """Remove Copilot App workflow rows referenced by *managed_files*.

        Filters the input set to ``copilot-app-db://workflows/`` URIs,
        decodes the workflow ids, and deletes them in a single
        transaction.  Non-APM-namespaced ids are rejected by
        ``copilot_app_db.delete_workflows`` for defence in depth.
        """
        from apm_cli.integration.copilot_app_db import (
            COPILOT_APP_LOCKFILE_PREFIX,
            CopilotAppDbError,
            delete_workflows,
            from_lockfile_uri,
            is_copilot_app_uri,
            resolve_copilot_app_db_path,
        )

        ids: list[str] = []
        for entry in managed_files:
            if not is_copilot_app_uri(entry):
                continue
            if not entry.startswith(COPILOT_APP_LOCKFILE_PREFIX):
                continue
            try:
                ids.append(from_lockfile_uri(entry))
            except ValueError:
                # Malformed entry -- skip rather than fail uninstall.
                continue
        if not ids:
            return {"files_removed": 0, "errors": 0}

        db_path = resolve_copilot_app_db_path()
        if db_path is None:
            # DB gone -- nothing to remove; treat as success (idempotent).
            return {"files_removed": 0, "errors": 0}

        try:
            removed = delete_workflows(db_path, ids)
        except CopilotAppDbError:
            return {"files_removed": 0, "errors": 1}
        return {"files_removed": removed, "errors": 0}

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

    # DEPRECATED: use integrate_prompts_for_target(...) instead.
    def integrate_package_prompts(
        self,
        package_info,
        project_root: Path,
        force: bool = False,
        managed_files: set = None,  # noqa: RUF013
        diagnostics=None,
        logger=None,
    ) -> IntegrationResult:
        """Integrate all prompts from a package into .github/prompts/.

        Deploys with clean filenames. Skips files that exist locally and
        are not tracked in any package's deployed_files (user-authored),
        unless force=True.

        Args:
            package_info: PackageInfo object with package metadata
            project_root: Root directory of the project
            force: If True, overwrite user-authored files on collision
            managed_files: Set of relative paths known to be APM-managed

        Returns:
            IntegrationResult: Results of the integration operation
        """
        self.init_link_resolver(package_info, project_root)

        # Find all prompt files in the package
        prompt_files = self.find_prompt_files(package_info.install_path)

        if not prompt_files:
            return IntegrationResult(
                files_integrated=0,
                files_updated=0,
                files_skipped=0,
                target_paths=[],
            )

        # Create .github/prompts/ if it doesn't exist
        prompts_dir = project_root / ".github" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)

        # Process each prompt file
        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths = []
        total_links_resolved = 0

        for source_file in prompt_files:
            target_filename = self.get_target_filename(source_file, package_info.package.name)
            target_path = prompts_dir / target_filename
            # Defense-in-depth: target_filename is derived from source
            # file name; assert containment under prompts_dir to mirror
            # the guard already present in command/instruction
            # integrators.
            try:
                ensure_path_within(target_path, prompts_dir)
            except PathTraversalError as exc:
                if diagnostics is not None:
                    diagnostics.warn(
                        message=f"Rejected prompt target path: {exc}",
                        package=package_info.package.name,
                    )
                files_skipped += 1
                continue
            rel_path = portable_relpath(target_path, project_root)

            if self.is_content_identical_to_source(target_path, source_file):
                # Pre-existing file is byte-identical to source -- silently
                # adopt. See BaseIntegrator.is_content_identical_to_source.
                target_paths.append(target_path)
                files_adopted += 1
                continue

            if self.check_collision(
                target_path, rel_path, managed_files, force, diagnostics=diagnostics
            ):
                files_skipped += 1
                continue

            links_resolved = self.copy_prompt(source_file, target_path)
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

    # DEPRECATED: use sync_for_target(...) instead.
    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
    ) -> dict[str, int]:
        """Remove APM-managed prompt files.

        Only removes files listed in *managed_files* (from apm.lock
        deployed_files).  Falls back to legacy ``*-apm.prompt.md`` glob
        when *managed_files* is ``None`` (old lockfile).
        """
        prompts_dir = project_root / ".github" / "prompts"
        return self.sync_remove_files(
            project_root,
            managed_files,
            prefix=".github/prompts/",
            legacy_glob_dir=prompts_dir,
            legacy_glob_pattern="*-apm.prompt.md",
        )


# ---------------------------------------------------------------------------
# Schedule frontmatter helpers (copilot-app target)
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402

_VALID_SCHEDULE_INTERVALS: frozenset[str] = frozenset({"manual", "hourly", "daily", "weekly"})
_VALID_SCHEDULE_MODES: frozenset[str] = frozenset({"interactive", "plan", "autopilot"})


@dataclass(frozen=True)
class Schedule:
    """Validated representation of a prompt's ``schedule:`` frontmatter.

    All fields are pre-validated against the same constraints the
    Copilot App's ``workflows`` schema enforces, so deploy time never
    surfaces a raw SQLite ``CHECK`` violation to the user.
    """

    interval: str = "manual"
    schedule_hour: int = 9
    schedule_day: int = 1
    mode: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


def _parse_schedule(block) -> Schedule:
    """Validate a frontmatter ``schedule:`` mapping and return a ``Schedule``.

    Raises ``ValueError`` (with a human-readable message) on any
    out-of-range or wrong-type field.  The caller turns the message
    into a diagnostic warning and skips the prompt.
    """
    if not isinstance(block, dict):
        raise ValueError("'schedule' must be a mapping")

    interval = str(block.get("interval", "manual"))
    if interval not in _VALID_SCHEDULE_INTERVALS:
        raise ValueError(
            f"interval must be one of {sorted(_VALID_SCHEDULE_INTERVALS)}, got {interval!r}"
        )

    hour = block.get("schedule_hour", 9)
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        raise ValueError(f"schedule_hour must be int 0..23, got {hour!r}")

    day = block.get("schedule_day", 1)
    if not isinstance(day, int) or not (0 <= day <= 6):
        raise ValueError(f"schedule_day must be int 0..6, got {day!r}")

    mode = block.get("mode")
    if mode is not None:
        mode = str(mode)
        if mode not in _VALID_SCHEDULE_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_SCHEDULE_MODES)}, got {mode!r}")

    model = block.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"model must be a string, got {model!r}")

    reasoning_effort = block.get("reasoning_effort")
    if reasoning_effort is not None and not isinstance(reasoning_effort, str):
        raise ValueError(f"reasoning_effort must be a string, got {reasoning_effort!r}")

    return Schedule(
        interval=interval,
        schedule_hour=hour,
        schedule_day=day,
        mode=mode,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def _derive_package_owner(package_info) -> str:
    """Best-effort owner-segment extraction for namespacing workflow ids.

    Looks at the package's ``source`` (GitHub-style ``owner/repo`` or
    URL) first, then ``author``, then falls back to ``"local"`` for
    locally-sourced packages.  The returned string is slugified by the
    DB-side ``namespaced_id`` helper, so any input is safe.
    """
    pkg = package_info.package
    source = getattr(pkg, "source", None)
    if isinstance(source, str) and source:
        # github:foo/bar, https://github.com/foo/bar, foo/bar
        s = source.split("://", 1)[-1]
        s = s.split(":", 1)[-1]
        parts = [p for p in s.split("/") if p and p != "github.com"]
        if parts:
            return parts[0]
    author = getattr(pkg, "author", None)
    if isinstance(author, str) and author.strip():
        return author.strip()
    return "local"
