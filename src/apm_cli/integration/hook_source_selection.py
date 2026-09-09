"""Target-aware source selection for deployable hook descriptors and bundles."""

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from apm_cli.hook_contract import walk_hook_commands
from apm_cli.integration.hook_bundle import _hook_source_root
from apm_cli.integration.hook_command_paths import (
    iter_plugin_root_paths,
    iter_relative_script_paths,
    normalize_quoted_plugin_root,
    plugin_root_relative_path,
)
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within


@dataclass(frozen=True)
class HookSourceSelection:
    """Authorized hook descriptors and bundle assets grouped by target."""

    descriptor_files: dict[str, frozenset[Path]]
    bundle_files: dict[str, frozenset[Path]]

    def descriptors_for(self, target_name: str) -> list[Path]:
        """Return target-routed descriptors in stable path order."""
        return sorted(self.descriptor_files.get(target_name, frozenset()))

    def bundle_for(self, target_name: str) -> frozenset[Path]:
        """Return target-materialized bundle assets."""
        return self.bundle_files.get(target_name, frozenset())

    @property
    def files(self) -> frozenset[Path]:
        """Return the multi-target union of every selected source file."""
        return frozenset(
            path
            for selected in (*self.descriptor_files.values(), *self.bundle_files.values())
            for path in selected
        )


def _relative_hook_script_bases(
    package_path: Path,
    hook_file_dir: Path | None,
) -> list[Path]:
    """Return candidate bases for resolving a relative hook script path."""
    bases: list[Path] = []
    if hook_file_dir is not None:
        bases.append(hook_file_dir)
    if package_path not in bases:
        bases.append(package_path)
    return bases


def _resolve_relative_hook_script(
    package_path: Path,
    hook_file_dir: Path | None,
    rel_path: str,
) -> Path | None:
    """Resolve a relative hook script path without escaping the package."""
    last_candidate: Path | None = None
    for base in _relative_hook_script_bases(package_path, hook_file_dir):
        try:
            candidate = ensure_path_within(base / rel_path, package_path)
        except PathTraversalError:
            continue
        last_candidate = candidate
        if candidate.exists() and candidate.is_file():
            return candidate
    return last_candidate


def _referenced_hook_source_files(
    data: dict,
    package_path: Path,
    hook_file_dir: Path,
) -> set[Path]:
    """Resolve existing package files referenced by a parsed hook document."""
    source_files: set[Path] = set()
    for declaration in walk_hook_commands(data):
        command = normalize_quoted_plugin_root(declaration.command)
        for match in iter_plugin_root_paths(command):
            try:
                source_file = ensure_path_within(
                    package_path / plugin_root_relative_path(match.group(1)),
                    package_path,
                )
            except PathTraversalError:
                continue
            if source_file.is_file():
                source_files.add(source_file)
        for match in iter_relative_script_paths(command):
            source_file = _resolve_relative_hook_script(
                package_path,
                hook_file_dir,
                match.group(1)[2:].replace("\\", "/"),
            )
            if source_file is not None and source_file.is_file():
                source_files.add(source_file)
    return source_files


def _parse_hook_json(hook_file: Path, *, logger: logging.Logger) -> dict | None:
    """Parse a hook document, normalizing supported naked hook slices."""
    try:
        with open(hook_file, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return None
        if "hooks" not in data and data and all(isinstance(value, list) for value in data.values()):
            logger.debug(
                "Promoted naked-format hook file %s (top-level event keys: %s) to wrapped shape",
                hook_file,
                sorted(data.keys()),
            )
            data = {"hooks": data}
        if "hooks" in data and not isinstance(data["hooks"], dict):
            logger.warning(
                "Skipping malformed hook file %s: 'hooks' must be a dict, got %s",
                hook_file,
                type(data["hooks"]).__name__,
            )
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def select_hook_sources(
    package_path: Path,
    target_names: Iterable[str],
    *,
    package_name: str,
    package_identity: str,
    warned_packages: set[str] | None,
    hook_files: list[Path],
    parse_hook_json: Callable[[Path], dict | None],
    filter_hook_files: Callable[..., list[Path]],
    iter_bundle_files: Callable[..., list[Path]],
    merge_target_names: Iterable[str],
) -> HookSourceSelection:
    """Select descriptors and bundle assets for every supported target."""
    all_descriptors = set(hook_files)
    descriptors_by_target: dict[str, frozenset[Path]] = {}
    bundles_by_target: dict[str, frozenset[Path]] = {}
    parsed_hooks: dict[Path, dict | None] = {}
    supported_targets = {"copilot", "kiro", *merge_target_names}

    for target_name in dict.fromkeys(target_names):
        if target_name not in supported_targets:
            continue
        descriptors = filter_hook_files(
            hook_files,
            target_name,
            package_name=package_name,
            package_identity=package_identity,
            warned_packages=warned_packages,
        )
        descriptors_by_target[target_name] = frozenset(descriptors)
        source_roots: set[Path] = set()
        for hook_file in descriptors:
            if hook_file not in parsed_hooks:
                parsed_hooks[hook_file] = parse_hook_json(hook_file)
            data = parsed_hooks[hook_file]
            if data is None:
                continue
            for source_file in _referenced_hook_source_files(
                data,
                package_path,
                hook_file.parent,
            ):
                source_roots.add(_hook_source_root(package_path, hook_file.parent, source_file))

        bundle_files: set[Path] = set()
        for source_root in sorted(source_roots):
            bundle_files.update(
                iter_bundle_files(
                    source_root,
                    descriptor_files=all_descriptors,
                    exclude_json_files=target_name == "copilot",
                )
            )
        bundles_by_target[target_name] = frozenset(bundle_files)

    return HookSourceSelection(descriptors_by_target, bundles_by_target)
