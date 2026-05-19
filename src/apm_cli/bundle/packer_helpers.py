"""Helper functions for bundle packing operations."""

from pathlib import Path

from ..deps.lockfile import LockFile
from ..models.apm_package import APMPackage


def validate_package_metadata(
    project_root: Path, apm_yml_path: Path, skill_md_path: Path, logger
) -> tuple[str, str, str | list[str] | None]:
    """Validate package metadata and return name, version, config target.

    Args:
        project_root: Root directory of the project.
        apm_yml_path: Path to apm.yml.
        skill_md_path: Path to SKILL.md (for hybrid check).
        logger: Logger instance for warnings.

    Returns:
        Tuple of (pkg_name, pkg_version, config_target).

    Raises:
        ValueError: If local-path dependencies are found or other validation fails.
    """
    is_hybrid_root = apm_yml_path.exists() and skill_md_path.exists()
    try:
        package = APMPackage.from_apm_yml(apm_yml_path)
        pkg_name = package.name
        pkg_version = package.version or "0.0.0"
        config_target = package.target

        # HYBRID author guard: apm.yml.description and SKILL.md
        # description serve different consumers (human-facing CLI/search
        # vs. agent-runtime invocation matcher) and are NOT merged. If
        # the author shipped a SKILL.md description but left
        # apm.yml.description blank, the human-facing surfaces (apm view,
        # apm search, marketplace listings) will degrade silently while
        # Claude/Copilot still invoke the skill correctly. Warn loudly
        # at pack time -- this is the publish gate for the AUTHOR.
        if is_hybrid_root and not package.description and logger:
            try:
                import frontmatter as _frontmatter

                with open(skill_md_path, encoding="utf-8") as _f:
                    _skill_post = _frontmatter.load(_f)
                _skill_desc = _skill_post.metadata.get("description")
            except Exception:
                _skill_desc = None
            if _skill_desc:
                logger.warning(
                    "apm.yml is missing 'description'. SKILL.md has its own "
                    "description, but that is for agent invocation -- not "
                    "for 'apm view' or search. Add a short tagline to "
                    'apm.yml:  description: "One-line human summary"'
                )

        # Guard: reject local-path dependencies (non-portable)
        for dep_ref in package.get_apm_dependencies():
            if dep_ref.is_local:
                raise ValueError(
                    f"Cannot pack -- apm.yml contains local path dependency: "
                    f"{dep_ref.local_path}\n"
                    f"Local dependencies are for development only. Replace them with "
                    f"remote references (e.g., 'owner/repo') before packing."
                )
    except ValueError:
        raise
    except FileNotFoundError:
        pkg_name = project_root.resolve().name
        pkg_version = "0.0.0"
        config_target = None

    return pkg_name, pkg_version, config_target


def collect_deployed_files(lockfile: LockFile) -> list[str]:
    """Collect deployed files from all non-local dependencies.

    Args:
        lockfile: The lockfile to extract deployed files from.

    Returns:
        List of deployed file paths from all dependencies.
    """
    all_deployed: list[str] = []
    for dep in lockfile.get_all_dependencies():
        if dep.source == "local":
            continue
        all_deployed.extend(dep.deployed_files)
    return all_deployed


def verify_file_safety_and_existence(
    unique_files: list[str],
    path_mappings: dict[str, str],
    project_root: Path,
) -> None:
    """Verify each path is safe (no traversal) and exists on disk.

    Args:
        unique_files: List of file paths to verify.
        path_mappings: Mapping of output paths to disk paths.
        project_root: Root directory of the project.

    Raises:
        ValueError: If unsafe paths or missing files are detected.
    """
    project_root_resolved = project_root.resolve()
    missing: list[str] = []
    for rel_path in unique_files:
        # Guard against absolute paths or path-traversal entries in deployed_files
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Refusing to pack unsafe path from lockfile: {rel_path!r}")
        # For cross-target mapped files, verify the original (on-disk) path
        disk_path = path_mappings.get(rel_path, rel_path)
        abs_path = project_root / disk_path
        if not abs_path.resolve().is_relative_to(project_root_resolved):
            raise ValueError(f"Refusing to pack path that escapes project root: {disk_path!r}")
        # deployed_files may reference directories (ending with /)
        if not abs_path.exists():
            missing.append(disk_path)
    if missing:
        raise ValueError(
            "The following deployed files are missing on disk  -- "
            "run 'apm install' to restore them:\n" + "\n".join(f"  - {m}" for m in missing)
        )


def scan_bundle_security(
    unique_files: list[str],
    path_mappings: dict[str, str],
    project_root: Path,
    logger,
) -> int:
    """Scan files for hidden characters before bundling.

    Args:
        unique_files: List of file paths to scan.
        path_mappings: Mapping of output paths to disk paths.
        project_root: Root directory of the project.
        logger: Logger instance for warnings.

    Returns:
        Total number of security findings.
    """
    from ..security.gate import WARN_POLICY, SecurityGate
    from ..utils.console import _rich_warning

    scan_findings_total = 0
    for rel_path in unique_files:
        disk_path = path_mappings.get(rel_path, rel_path)
        src = project_root / disk_path
        if src.is_symlink():
            continue
        if src.is_dir():
            verdict = SecurityGate.scan_files(src, policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
        elif src.is_file():
            verdict = SecurityGate.scan_text(
                src.read_text(encoding="utf-8", errors="replace"),
                str(src),
                policy=WARN_POLICY,
            )
            scan_findings_total += len(verdict.all_findings)
    if scan_findings_total:
        warn_msg = (
            f"Bundle contains {scan_findings_total} hidden character(s) across source files "
            f"-- run 'apm audit' to inspect before publishing"
        )
        if logger:
            logger.warning(warn_msg)
        else:
            _rich_warning(warn_msg)
    return scan_findings_total


def copy_bundle_files(
    unique_files: list[str],
    path_mappings: dict[str, str],
    project_root: Path,
    bundle_dir: Path,
) -> None:
    """Copy files to bundle directory preserving structure.

    Args:
        unique_files: List of file paths to copy.
        path_mappings: Mapping of output paths to disk paths.
        project_root: Root directory of the project.
        bundle_dir: Target bundle directory.

    Raises:
        ValueError: If a file would be written outside bundle directory.
    """
    import shutil

    bundle_dir_resolved = bundle_dir.resolve()
    for rel_path in unique_files:
        # For cross-target mapped files, read from the original disk path
        disk_path = path_mappings.get(rel_path, rel_path)
        src = project_root / disk_path
        if src.is_symlink():
            continue  # Never bundle symlinks
        dest = bundle_dir / rel_path
        # Defence-in-depth: verify mapped destination stays inside the bundle
        if not dest.resolve().is_relative_to(bundle_dir_resolved):
            raise ValueError(f"Refusing to write outside bundle directory: {rel_path!r}")
        if src.is_dir():
            from ..security.gate import ignore_non_content

            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore_non_content)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)
