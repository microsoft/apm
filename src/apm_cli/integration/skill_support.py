"""Supporting value objects and cleanup helpers for skill integration."""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apm_cli.core.deployment_state import MaterializationResult


def build_copy_ignore(
    *,
    skip_bin: bool = False,
) -> Callable[[str, list[str]], list[str]]:
    """Build a ``shutil.copytree`` ignore function."""
    from apm_cli.security.gate import ignore_non_content

    if not skip_bin:
        return ignore_non_content
    bin_filter = shutil.ignore_patterns("bin")

    def combined(directory: str, contents: list[str]) -> list[str]:
        return list(
            set(ignore_non_content(directory, contents)) | set(bin_filter(directory, contents))
        )

    return combined


@dataclass
class SkillIntegrationResult:
    """Compatibility result returned by skill-specific integration APIs."""

    skill_created: bool
    skill_updated: bool
    skill_skipped: bool
    skill_path: Path | None
    references_copied: int
    links_resolved: int = 0
    sub_skills_promoted: int = 0
    bin_deployed: int = 0
    bin_skipped_reason: str | None = None
    target_paths: list[Path] = None
    materializations: tuple[MaterializationResult, ...] = ()

    def __post_init__(self) -> None:
        if self.target_paths is None:
            self.target_paths = []


def clean_orphaned_skills(
    skills_dir: Path,
    installed_skill_names: set,
    *,
    project_root: Path | None,
    get_lockfile_owned_agent_skills: Callable[[Path], set[str]],
) -> dict[str, int]:
    """Remove legacy-orphan skill directories without touching foreign agents."""
    protected_names = set(installed_skill_names)
    if project_root is not None:
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        protected_names.update(LSPIntegrator.reserved_project_skill_names(skills_dir, project_root))
    files_removed = 0
    errors = 0
    lockfile_owned_skills: set[str] | None = None
    if skills_dir.parent.name == ".agents" and project_root is not None:
        lockfile_owned_skills = get_lockfile_owned_agent_skills(project_root)

    for skill_subdir in skills_dir.iterdir():
        if not skill_subdir.is_dir() or skill_subdir.name in protected_names:
            continue
        if lockfile_owned_skills is not None and skill_subdir.name not in lockfile_owned_skills:
            continue
        try:
            shutil.rmtree(skill_subdir)
            files_removed += 1
        except Exception:
            errors += 1

    return {"files_removed": files_removed, "errors": errors}


def get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
    """Return APM-owned ``.agents/skills`` names from the lockfile."""
    owned: set[str] = set()
    try:
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lockfile = LockFile.read(get_lockfile_path(project_root))
        if lockfile and lockfile.dependencies:
            for dep in lockfile.dependencies.values():
                for deployed_file in dep.deployed_files:
                    if deployed_file.startswith(".agents/skills/"):
                        name = deployed_file[len(".agents/skills/") :].split("/", 1)[0]
                        if name:
                            owned.add(name)
    except (FileNotFoundError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        import logging

        logging.getLogger(__name__).debug("Could not read lockfile for ownership check: %s", exc)
    return owned
