"""Private helpers for pack_bundle -- extracted to keep packer.py within complexity budget."""

import shutil
from pathlib import Path

from ..core.target_detection import detect_target
from .attest import verify_attested_file
from .lockfile_enrichment import _filter_files_by_target


def _resolve_package_meta(
    project_root: Path,
    target: "str | list[str] | None",
    logger=None,
) -> "tuple[str, str, str | list[str]]":
    """Read apm.yml, validate package, resolve the effective target.

    Returns ``(pkg_name, pkg_version, effective_target)``.

    Raises ``ValueError`` for local-path dependencies.
    """
    apm_yml_path = project_root / "apm.yml"
    skill_md_path = project_root / "SKILL.md"
    is_hybrid_root = apm_yml_path.exists() and skill_md_path.exists()
    config_target = None
    try:
        from apm_cli.models.apm_package import APMPackage, package_target_selection

        package = APMPackage.from_apm_yml(apm_yml_path)
        pkg_name = package.name
        pkg_version = package.version or "0.0.0"
        config_target = package_target_selection(package)

        if is_hybrid_root and not package.description and logger:
            _maybe_warn_hybrid_description(skill_md_path, logger)

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

    effective_target = _compute_effective_target(project_root, target, config_target)
    return pkg_name, pkg_version, effective_target


def _maybe_warn_hybrid_description(skill_md_path: Path, logger) -> None:
    """Emit a warning when apm.yml is missing a description in a hybrid root."""
    try:
        from apm_cli.utils.yaml_io import load_frontmatter

        with open(skill_md_path, encoding="utf-8") as _f:
            _skill_post = load_frontmatter(_f)
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


def _compute_effective_target(
    project_root: Path,
    target: "str | list[str] | None",
    config_target: "str | list[str] | None",
) -> "str | list[str]":
    """Derive the effective pack target from CLI input and apm.yml config."""
    import sys

    # Route detect_target through the packer facade so tests can monkeypatch
    # apm_cli.bundle.packer.detect_target (RULE B seam).
    _packer = sys.modules.get("apm_cli.bundle.packer")
    _detect_target = getattr(_packer, "detect_target", None) or detect_target

    if isinstance(target, list):
        return target
    if isinstance(config_target, list) and target is None:
        return config_target
    effective, _reason = _detect_target(
        project_root,
        explicit_target=target,
        config_target=config_target if isinstance(config_target, str) else None,
    )
    if effective == "minimal":
        return "all"
    return effective


def _collect_and_filter_deployed(
    lockfile,
    effective_target: "str | list[str]",
) -> "tuple[list[str], dict[str, str], dict[str, str], dict[str, str]]":
    """Collect deployed files from lockfile deps, filter by target, and deduplicate.

    Returns ``(unique_files, path_mappings, deployed_hashes, hash_dep_labels)``.
    """
    all_deployed: list[str] = []
    deployed_hashes: dict[str, str] = {}
    hash_dep_labels: dict[str, str] = {}
    for dep in lockfile.get_all_dependencies():
        if dep.source == "local":
            continue
        all_deployed.extend(dep.deployed_files)
        for _dpath, _dhash in dep.deployed_file_hashes.items():
            deployed_hashes[_dpath] = _dhash
            hash_dep_labels[_dpath] = dep.repo_url

    filtered_files, path_mappings = _filter_files_by_target(all_deployed, effective_target)

    seen: set[str] = set()
    unique_files: list[str] = []
    for f in filtered_files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    return unique_files, path_mappings, deployed_hashes, hash_dep_labels


def _verify_deployed_paths(
    unique_files: list[str],
    path_mappings: dict[str, str],
    project_root: Path,
    project_root_resolved: Path,
) -> None:
    """Raise ``ValueError`` if any deployed file path is unsafe or missing on disk."""
    missing: list[str] = []
    for rel_path in unique_files:
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Refusing to pack unsafe path from lockfile: {rel_path!r}")
        disk_path = path_mappings.get(rel_path, rel_path)
        abs_path = project_root / disk_path
        if not abs_path.resolve().is_relative_to(project_root_resolved):
            raise ValueError(f"Refusing to pack path that escapes project root: {disk_path!r}")
        if not abs_path.exists():
            missing.append(disk_path)
    if missing:
        raise ValueError(
            "The following deployed files are missing on disk  -- "
            "run 'apm install' to restore them:\n" + "\n".join(f"  - {m}" for m in missing)
        )


def _scan_and_warn_hidden_chars(
    unique_files: list[str],
    path_mappings: dict[str, str],
    project_root: Path,
    logger=None,
) -> None:
    """Scan bundle files for hidden characters and emit a warning if any are found."""
    from ..security.gate import WARN_POLICY, SecurityGate
    from ..utils.console import _rich_warning

    total_findings = 0
    for rel_path in unique_files:
        disk_path = path_mappings.get(rel_path, rel_path)
        src = project_root / disk_path
        if src.is_symlink():
            continue
        if src.is_dir():
            verdict = SecurityGate.scan_files(src, policy=WARN_POLICY)
            total_findings += len(verdict.all_findings)
        elif src.is_file():
            verdict = SecurityGate.scan_text(
                src.read_text(encoding="utf-8", errors="replace"),
                str(src),
                policy=WARN_POLICY,
            )
            total_findings += len(verdict.all_findings)

    if total_findings:
        _warn_msg = (
            f"Bundle contains {total_findings} hidden character(s) across source files "
            f"-- run 'apm audit' to inspect before publishing"
        )
        if logger:
            logger.warning(_warn_msg)
        else:
            _rich_warning(_warn_msg)


def _copy_files_verified(
    unique_files: list[str],
    path_mappings: dict[str, str],
    deployed_hashes: dict[str, str],
    hash_dep_labels: dict[str, str],
    project_root: Path,
    project_root_resolved: Path,
    bundle_dir: Path,
    bundle_dir_resolved: Path,
) -> None:
    """Copy deployed files into the bundle directory with hash verification."""

    for rel_path in unique_files:
        disk_path = path_mappings.get(rel_path, rel_path)
        src = project_root / disk_path
        if src.is_symlink():
            continue
        dest = bundle_dir / rel_path
        if not dest.resolve().is_relative_to(bundle_dir_resolved):
            raise ValueError(f"Refusing to write outside bundle directory: {rel_path!r}")
        if src.is_dir():
            _copy_dir_verified(
                src,
                dest,
                disk_path,
                deployed_hashes,
                hash_dep_labels,
                project_root_resolved,
            )
        else:
            verify_attested_file(
                src,
                deployed_hashes.get(disk_path),
                hash_dep_labels.get(disk_path, "dependency"),
                disk_path,
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)


def _copy_dir_verified(
    src: Path,
    dest: Path,
    disk_path: str,
    deployed_hashes: dict[str, str],
    hash_dep_labels: dict[str, str],
    project_root_resolved: Path,
) -> None:
    """Recursively verify and copy a directory into the bundle."""
    from ..security.gate import ignore_non_content

    disk_base = disk_path.rstrip("/")
    for child in sorted(src.rglob("*")):
        if not child.is_file() or child.is_symlink():
            continue
        if not child.resolve().is_relative_to(project_root_resolved):
            raise ValueError(
                f"Refusing to pack path that escapes project root: "
                f"{child.relative_to(project_root_resolved.parent)!r}"
            )
        child_key = f"{disk_base}/{child.relative_to(src).as_posix()}"
        verify_attested_file(
            child,
            deployed_hashes.get(child_key),
            hash_dep_labels.get(child_key, "dependency"),
            child_key,
        )
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore_non_content)
