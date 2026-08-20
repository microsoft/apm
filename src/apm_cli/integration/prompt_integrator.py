"""Prompt integration functionality for APM packages."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.atomic_io import write_text_lf
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_frontmatter

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile


class PromptIntegrator(BaseIntegrator):
    """Handles integration of APM package prompts into target prompt directories."""

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
        write_text_lf(target, content)
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
        scope=None,
    ) -> IntegrationResult:
        """Integrate prompts for a single *target*."""
        mapping = target.primitives.get("prompts")
        if not mapping:
            return IntegrationResult(0, 0, 0, [])

        # GitHub Copilot desktop App: deploy to SQLite (or WS-IPC when
        # the App is running) instead of files. The branch fully owns
        # lifecycle for this target -- it does not share the file-based
        # collision / link-resolution machinery.
        if target.name == "copilot-app":
            # Delegate to the workflow integrator -- file-based and
            # SQLite-row deploy share NOTHING except the source
            # artefact. See copilot_app_workflow_integrator.py.
            from apm_cli.integration.copilot_app_workflow_integrator import (
                CopilotAppWorkflowIntegrator,
            )

            # Detect --global / user-scope by name; we accept either the
            # InstallScope enum or a string. Avoids a hard import cycle.
            user_scope = False
            if scope is not None:
                user_scope = getattr(scope, "name", str(scope)).upper() == "USER"
            return CopilotAppWorkflowIntegrator().integrate(
                target,
                package_info,
                project_root=project_root,
                user_scope=user_scope,
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
            target=target,
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
            from apm_cli.integration.copilot_app_workflow_integrator import (
                CopilotAppWorkflowIntegrator,
            )

            return CopilotAppWorkflowIntegrator().sync(managed_files or set())

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
    # copilot-app SQLite path -- MOVED to copilot_app_workflow_integrator.py.
    # The methods below are kept as thin shims for back-compat with any
    # external caller / test that imported them directly. They will be
    # removed in a future major.
    # ------------------------------------------------------------------

    def _integrate_prompts_for_copilot_app(
        self,
        target: TargetProfile,
        package_info,
        *,
        project_root: Path,
        user_scope: bool,
        force: bool,
        diagnostics,
    ) -> IntegrationResult:
        """Back-compat shim -- forwards to CopilotAppWorkflowIntegrator.integrate.

        The real implementation moved to
        apm_cli.integration.copilot_app_workflow_integrator. Kept here so
        external callers / tests that still reference the method-on-PromptIntegrator
        attribute continue to work. New code should construct
        CopilotAppWorkflowIntegrator directly.
        """
        from apm_cli.integration.copilot_app_workflow_integrator import (
            CopilotAppWorkflowIntegrator,
        )

        return CopilotAppWorkflowIntegrator().integrate(
            target,
            package_info,
            project_root=project_root,
            user_scope=user_scope,
            force=force,
            diagnostics=diagnostics,
        )

    def _sync_copilot_app(self, managed_files: set[str]) -> dict[str, int]:
        """Back-compat shim -- forwards to CopilotAppWorkflowIntegrator.sync."""
        from apm_cli.integration.copilot_app_workflow_integrator import (
            CopilotAppWorkflowIntegrator,
        )

        return CopilotAppWorkflowIntegrator().sync(managed_files)

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
        target: TargetProfile | None = None,
    ) -> IntegrationResult:
        """Integrate all prompts from a package into the target prompts directory.

        Deploys with clean filenames. Skips files that exist locally and
        are not tracked in any package's deployed_files (user-authored),
        unless force=True.

        Args:
            package_info: PackageInfo object with package metadata
            project_root: Root directory of the project
            force: If True, overwrite user-authored files on collision
            managed_files: Set of relative paths known to be APM-managed
            target: Target profile that determines the prompt deploy directory.

        Returns:
            IntegrationResult: Results of the integration operation
        """
        self.init_link_resolver(package_info, project_root)

        if target is None:
            from apm_cli.integration.targets import KNOWN_TARGETS

            target = KNOWN_TARGETS["copilot"]
        mapping = target.primitives.get("prompts")
        if mapping is None:
            return IntegrationResult(0, 0, 0, [])

        # Find all prompt files in the package
        prompt_files = self.find_prompt_files(package_info.install_path)

        if not prompt_files:
            return IntegrationResult(
                files_integrated=0,
                files_updated=0,
                files_skipped=0,
                target_paths=[],
            )

        effective_root = mapping.deploy_root or target.root_dir
        prompts_dir = project_root / effective_root / mapping.subdir
        prompts_dir.mkdir(parents=True, exist_ok=True)

        # Process each prompt file
        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths = []
        total_links_resolved = 0

        for source_file in prompt_files:
            # Skip workflow-shape prompts at file-based targets: an
            # author who added execution metadata (interval, mode, ...)
            # meant the Copilot App workflows table, NOT a slash command
            # in a file-based prompt directory.  Without this guard, the same source
            # file ships to both surfaces and the App-only metadata
            # leaks into a slash-command users would not expect.
            try:
                _meta = load_frontmatter(str(source_file)).metadata
            except Exception:
                _meta = {}
            if _is_workflow_shape(_meta):
                files_skipped += 1
                continue

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

            if self.try_adopt_identical(
                target_path, source_file, target_paths, lf_normalized_deploy=True
            ):
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
# Schedule frontmatter helpers -- MOVED to copilot_app_workflow_integrator.
#
# Re-exported here for back-compat with tests / external consumers that still
# import them from this module. New code should import from
# apm_cli.integration.copilot_app_workflow_integrator directly.
# ---------------------------------------------------------------------------

from apm_cli.integration.copilot_app_workflow_integrator import (  # noqa: E402
    Schedule,
    _derive_package_owner,
    _is_workflow_shape,
    _parse_schedule,
    _parse_workflow_frontmatter,
)

__all__ = [
    "PromptIntegrator",
    "Schedule",
    "_derive_package_owner",
    "_is_workflow_shape",
    "_parse_schedule",
    "_parse_workflow_frontmatter",
]
