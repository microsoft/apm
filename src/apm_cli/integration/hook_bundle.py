"""Helpers for deploying hook script bundles."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from apm_cli.install.cache_pin import MARKER_FILENAME
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.utils.path_security import ensure_path_within
from apm_cli.utils.paths import portable_relpath

_HOOK_SCRIPT_EXTENSIONS = {".js", ".mjs", ".cjs", ".ts"}


@dataclass
class HookBundleCopyResult:
    """Counters for deployed hook bundle file copies."""

    scripts_copied: int = 0
    files_adopted: int = 0


def _hook_source_root(
    package_path: Path,
    hook_file_dir: Path | None,
    source_file: Path,
) -> Path:
    """Return the hooks directory that should deploy with a script."""
    candidates: list[Path] = []
    if hook_file_dir is not None:
        candidates.append(hook_file_dir)
    candidates.extend((package_path / "hooks", package_path / ".apm" / "hooks"))

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            source_file.relative_to(candidate)
        except ValueError:
            continue
        return candidate
    return source_file.parent


def _target_root_for_hook_source(
    target_script: Path,
    source_file: Path,
    source_root: Path,
) -> Path:
    """Return the deployed directory corresponding to a source hooks root."""
    source_rel = source_file.relative_to(source_root)
    target_root = target_script
    for _part in source_rel.parts:
        target_root = target_root.parent
    return target_root


def _hook_module_type(package_path: Path, hook_source_root: Path) -> str:
    """Return the Node module type that governs a source hooks root."""
    current = hook_source_root
    while True:
        package_json = current / "package.json"
        if package_json.exists():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return "commonjs"
            package_type = data.get("type") if isinstance(data, dict) else None
            return package_type if package_type in {"commonjs", "module"} else "commonjs"
        if current in (package_path, current.parent):
            return "commonjs"
        current = current.parent


def _same_text(path: Path, content: str) -> bool:
    """Return True when path already contains content."""
    try:
        return path.read_text(encoding="utf-8") == content
    except OSError:
        return False


def _is_root_hook_descriptor(
    source_file: Path, source_root: Path, descriptor_files: set[Path]
) -> bool:
    """Return True for root-level hook manifests that should not deploy."""
    if source_file in descriptor_files:
        return True
    if source_file.parent != source_root or source_file.suffix.lower() != ".json":
        return False
    stem = source_file.stem.lower()
    return stem == "hooks" or stem.startswith("hooks-") or stem.endswith("-hooks")


def copy_deployed_hook_bundle(
    integrator: BaseIntegrator,
    *,
    package_path: Path,
    hook_file_dir: Path | None,
    project_root: Path,
    scripts: list[tuple[Path, str]],
    managed_files: set | None,
    force: bool,
    diagnostics=None,
    target_paths: list[Path],
    hook_descriptor_files: set[Path] | None = None,
) -> HookBundleCopyResult:
    """Copy each referenced script's whole hooks root and module type."""
    result = HookBundleCopyResult()
    source_target_roots: dict[tuple[Path, Path], str] = {}
    root_has_js_hook: dict[tuple[Path, Path], bool] = {}
    command_target_rels = {target_rel for _source_file, target_rel in scripts}
    descriptor_files = hook_descriptor_files or set()

    for source_file, target_rel in scripts:
        target_script = project_root / target_rel
        source_root = _hook_source_root(package_path, hook_file_dir, source_file)
        target_root = _target_root_for_hook_source(
            target_script,
            source_file,
            source_root,
        )
        source_target_roots[(source_root, target_root)] = _hook_module_type(
            package_path,
            source_root,
        )
        root_has_js_hook.setdefault((source_root, target_root), False)

    copy_plan: dict[str, Path] = {}
    for source_root, target_root in source_target_roots:
        for source_file in sorted(source_root.rglob("*")):
            if (
                source_file.is_symlink()
                or not source_file.is_file()
                or source_file.name in {"package.json", MARKER_FILENAME}
                or _is_root_hook_descriptor(source_file, source_root, descriptor_files)
            ):
                continue
            source_rel = source_file.relative_to(source_root)
            target_file = target_root / source_rel
            copy_plan[portable_relpath(target_file, project_root)] = source_file
            if source_file.suffix.lower() in _HOOK_SCRIPT_EXTENSIONS:
                root_has_js_hook[(source_root, target_root)] = True

    for target_rel, source_file in copy_plan.items():
        target_file = project_root / target_rel
        ensure_path_within(target_file, project_root)
        if integrator.try_adopt_identical(target_file, source_file, target_paths):
            if target_rel in command_target_rels:
                result.files_adopted += 1
            continue
        if integrator.check_collision(
            target_file,
            target_rel,
            managed_files,
            force,
            diagnostics=diagnostics,
        ):
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        if target_rel in command_target_rels:
            result.scripts_copied += 1
        target_paths.append(target_file)

    for (_source_root, target_root), module_type in source_target_roots.items():
        if not root_has_js_hook.get((_source_root, target_root), False):
            continue
        target_file = target_root / "package.json"
        target_rel = portable_relpath(target_file, project_root)
        ensure_path_within(target_file, project_root)
        content = json.dumps({"type": module_type}, indent=2, sort_keys=True) + "\n"
        if target_file.exists() and _same_text(target_file, content):
            target_paths.append(target_file)
            continue
        if integrator.check_collision(
            target_file,
            target_rel,
            managed_files,
            force,
            diagnostics=diagnostics,
        ):
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        target_paths.append(target_file)

    return result
