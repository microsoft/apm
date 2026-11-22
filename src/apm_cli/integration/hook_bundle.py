"""Helpers for deploying hook script bundles."""

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from apm_cli.install.cache_pin import MARKER_FILENAME
from apm_cli.integration import write_seam
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

_HOOK_SCRIPT_EXTENSIONS = {".js", ".mjs", ".cjs", ".ts"}
_log = logging.getLogger(__name__)


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


def _walk_dereferencing(source_root: Path, package_root: Path) -> list[Path]:
    """List files under *source_root* as local materialization lays them out.

    In-package symlinks (files and directories) appear under their link
    names, as the local package copy dereferences them; a link that escapes
    *package_root*, dangles or cycles raises ``PathTraversalError``, as that
    copy aborts.
    """
    package_root = package_root.resolve()
    found: list[Path] = []
    pending = [(source_root, frozenset({source_root.resolve()}))]
    while pending:
        directory, visited = pending.pop()
        for entry in directory.iterdir():
            target = entry
            if entry.is_symlink():
                try:
                    target = ensure_path_within(entry.resolve(strict=True), package_root)
                except OSError as exc:
                    raise PathTraversalError(f"Unresolvable hook bundle link {entry}") from exc
            if target.is_dir():
                if target.resolve() in visited:
                    raise PathTraversalError(f"Circular hook bundle link {entry}")
                pending.append((entry, visited | {target.resolve()}))
            elif target.is_file():
                found.append(entry)
    return sorted(found)


def iter_deployable_hook_bundle_files(
    source_root: Path,
    *,
    descriptor_files: set[Path] | None = None,
    exclude_json_files: bool = False,
    include_package_metadata: bool = False,
    dereference_within: Path | None = None,
) -> Iterator[Path]:
    """Yield source files that hook bundle materialization may copy.

    With *dereference_within*, in-package symlinks are followed as the local
    package copy follows them (see :func:`_walk_dereferencing`).
    """
    descriptors = descriptor_files or set()
    candidates = (
        sorted(source_root.rglob("*"))
        if dereference_within is None
        else _walk_dereferencing(source_root, dereference_within)
    )
    for source_file in candidates:
        if exclude_json_files and source_file.suffix.lower() == ".json":
            _log.debug(
                "Skipping JSON hook bundle asset %s for recursive-scanner target",
                source_file,
            )
            continue
        if (
            (source_file.is_symlink() and dereference_within is None)
            or not source_file.is_file()
            or source_file.name == MARKER_FILENAME
            or (source_file.name == "package.json" and not include_package_metadata)
            or _is_root_hook_descriptor(source_file, source_root, descriptors)
        ):
            continue
        yield source_file


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
    exclude_json_files: bool = False,
    source_plan=None,
    selected_bundle_files: frozenset[Path],
) -> HookBundleCopyResult:
    """Copy each referenced script's whole hooks root and module type.

    When exclude_json_files is True, source JSON assets and the generated
    package.json sidecar are not written. Use this for targets whose hook
    loader recursively scans the deployment directory for JSON hook
    descriptors (e.g. Copilot/VS Code), where package metadata or a nested
    configuration file would be mistaken for a hook descriptor. Hook packages
    that need ES module support on such targets should use the .mjs file
    extension instead; without the sidecar, Node.js treats bare .js files as
    CommonJS.
    """
    result = HookBundleCopyResult()
    package_path = package_path.resolve()
    if hook_file_dir is not None:
        hook_file_dir = hook_file_dir.resolve()
    selected_bundle_files = frozenset(path.resolve() for path in selected_bundle_files)
    source_target_roots: dict[tuple[Path, Path], str] = {}
    root_has_js_hook: dict[tuple[Path, Path], bool] = {}
    command_target_rels = {target_rel for _source_file, target_rel in scripts}
    descriptor_files = {path.resolve() for path in hook_descriptor_files or set()}

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
    files_by_ancestor: dict[Path, list[Path]] = defaultdict(list)
    for source_file in selected_bundle_files:
        current = source_file.parent
        while current.is_relative_to(package_path):
            files_by_ancestor[current].append(source_file)
            if current == package_path:
                break
            current = current.parent
    for source_root, target_root in source_target_roots:
        for source_file in files_by_ancestor.get(source_root, ()):
            if source_file in descriptor_files:
                continue
            if source_plan is not None and not source_plan.includes(
                portable_relpath(source_file, source_plan.source_root)
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
            if integrator.receipted_unchanged(target_file):
                continue  # unchanged since APM wrote it: not an adoption (RA-007)
            # A managed script already holding the source bytes is APM's: record
            # the receipt the skipped copy would have (#4184, #3701).
            if managed_files and target_rel in managed_files:
                integrator.note_rendered_write(target_file)
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
        # An edited copied script is kept like any managed file (#4626).
        if integrator.keeps_edited_output(
            target_file, source_file, target_rel, force, diagnostics, target_paths
        ):
            continue
        write_seam.mkdir(target_file.parent, parents=True, exist_ok=True)
        write_seam.copy2(source_file, target_file)
        integrator.note_rendered_write(target_file)
        if target_rel in command_target_rels:
            result.scripts_copied += 1
        target_paths.append(target_file)

    if exclude_json_files:
        return result

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
        if integrator.keeps_edited_output(
            target_file,
            target_file,
            target_rel,
            force,
            diagnostics,
            target_paths,
            expected_content=content,
        ):
            continue
        write_seam.mkdir(target_file.parent, parents=True, exist_ok=True)
        write_seam.write_text(target_file, content)
        integrator.note_rendered_write(target_file)
        target_paths.append(target_file)

    return result
