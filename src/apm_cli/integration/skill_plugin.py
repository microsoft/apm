"""Plugin bin deployment helpers for skill integration."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any


def _bin_deploy_denied(package_info: Any, policy: Any, logger: Any) -> bool:
    """Return True when policy opts the package out of bin deployment."""
    if policy is None:
        return False
    bd_policy = policy.bin_deploy
    if bd_policy is None:
        return False
    canonical = package_info.get_canonical_dependency_string()
    if bd_policy.deny_all:
        if logger:
            logger.progress(
                f"bin_deploy.deny_all: skipping bin deploy for {canonical}",
                symbol="info",
            )
        return True
    if canonical in bd_policy.deny:
        if logger:
            logger.progress(
                f"bin_deploy.deny: skipping bin deploy for {canonical}",
                symbol="info",
            )
        return True
    return False


def _deploy_plugin_bin(
    link_rewriter: Any,
    package_info: Any,
    project_root: Path,
    targets: Any,
    *,
    scope: Any = None,
    policy: Any = None,
    force: bool = False,
    logger: Any = None,
) -> tuple[list[Path], str | None]:
    """Deploy bin executables and plugin manifest for a marketplace plugin."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.utils.path_security import validate_path_segments

    bin_dir = package_info.install_path / "bin"
    if not bin_dir.is_dir():
        return [], None

    if scope is not InstallScope.USER:
        if logger and scope is InstallScope.PROJECT:
            logger.progress(
                "bin/ deploy is user-scope only; skipping for project-scope install",
                symbol="info",
            )
        return [], "project_scope"

    if link_rewriter._bin_deploy_denied(package_info, policy, logger):
        return [], None

    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(project_root)

    claude_targets = [
        target for target in targets if target.name == "claude" and target.supports("skills")
    ]
    if not claude_targets:
        if logger:
            logger.progress(
                "bin/ present but no active Claude skills target; skipping bin deploy for "
                f"{package_info.get_canonical_dependency_string()}",
                symbol="warning",
            )
        return [], "no_claude_target"

    skill_name = package_info.install_path.name
    validate_path_segments(skill_name, context="plugin skill name")
    deployed: list[Path] = []
    for target in claude_targets:
        effective_root = target.primitives["skills"].deploy_root or target.root_dir
        target_root_dir = project_root / target.root_dir
        if not target.auto_create and not target_root_dir.is_dir():
            continue

        skill_base = project_root / effective_root / "skills" / skill_name
        rel_prefix = f"{effective_root}/skills/{skill_name}"
        deployed.extend(
            link_rewriter._deploy_bin_files(bin_dir, skill_base, rel_prefix, force, logger)
        )
        manifest = link_rewriter._deploy_plugin_manifest(
            package_info.install_path, skill_base, rel_prefix, force, logger
        )
        if manifest is not None:
            deployed.append(manifest)

    return deployed, None


def _deploy_bin_files(
    bin_dir: Path,
    skill_base: Path,
    rel_prefix: str,
    force: bool,
    logger: Any,
) -> list[Path]:
    """Copy bin executables into a deployed skill directory."""
    from apm_cli.utils.path_security import ensure_path_within

    dest_bin = skill_base / "bin"
    dest_bin.mkdir(parents=True, exist_ok=True)
    deployed: list[Path] = []
    for src_file in bin_dir.iterdir():
        if src_file.is_symlink() or not src_file.is_file():
            continue
        dest_file = dest_bin / src_file.name
        ensure_path_within(dest_file, dest_bin)
        _copy_plugin_file(
            src_file,
            dest_file,
            force=force,
            make_executable=True,
            logger=logger,
            rel_label=f"{rel_prefix}/bin/{src_file.name}",
        )
        deployed.append(dest_file)
    return deployed


def _deploy_plugin_manifest(
    package_path: Path,
    skill_base: Path,
    rel_prefix: str,
    force: bool,
    logger: Any,
) -> Path | None:
    """Copy .claude-plugin/plugin.json next to the deployed bin directory."""
    plugin_manifest = package_path / ".claude-plugin" / "plugin.json"
    if plugin_manifest.is_symlink() or not plugin_manifest.is_file():
        return None
    dest_manifest = skill_base / ".claude-plugin" / "plugin.json"
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    _copy_plugin_file(
        plugin_manifest,
        dest_manifest,
        force=force,
        make_executable=False,
        logger=logger,
        rel_label=f"{rel_prefix}/.claude-plugin/plugin.json",
    )
    return dest_manifest


def _copy_plugin_file(
    src_file: Path,
    dest_file: Path,
    *,
    force: bool,
    make_executable: bool,
    logger: Any,
    rel_label: str,
) -> None:
    """Hash-gated copy of one plugin file, optionally marking it executable."""
    skip_copy = False
    if dest_file.exists() and not force:
        src_hash = hashlib.sha256(src_file.read_bytes()).hexdigest()
        dst_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        skip_copy = src_hash == dst_hash

    if not skip_copy:
        shutil.copy2(src_file, dest_file)

    if make_executable and os.name == "posix":
        current = dest_file.stat().st_mode
        dest_file.chmod((current & ~(stat.S_IXGRP | stat.S_IXOTH)) | stat.S_IXUSR)

    if not skip_copy and logger:
        logger.progress(f"deployed {src_file.name} -> {rel_label}", symbol="check")
