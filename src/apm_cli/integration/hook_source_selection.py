"""Target-aware source selection for deployable hook descriptors and bundles."""

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.hook_contract import walk_hook_commands
from apm_cli.integration.hook_bundle import _hook_source_root
from apm_cli.integration.hook_command_paths import (
    iter_plugin_root_paths,
    iter_relative_script_paths,
    normalize_quoted_plugin_root,
    plugin_root_relative_path,
)
from apm_cli.integration.hook_file_routing import (
    configured_hook_file_selection,
    hook_package_label,
    legacy_routing_widening,
    raise_for_legacy_routing,
)
from apm_cli.integration.hook_native_formats import require_claude_native_events
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookSourceSelection:
    """Authorized hook descriptors and bundle assets grouped by target."""

    descriptor_files: dict[str, frozenset[Path]]
    bundle_files: dict[str, frozenset[Path]]
    # Target -> package-relative files the retired filename convention hid.
    legacy_routing_conflicts: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def require_configured_routing(self, package_label: str) -> None:
        """Refuse deployment when removing filename routing would widen reach."""
        raise_for_legacy_routing(self.legacy_routing_conflicts, package_label)

    def require_deployable(
        self,
        package_info: Any,
        event_maps: dict[str, dict[str, str]],
    ) -> None:
        """Refuse retired-routing widening and Claude events without a native pair."""
        self.require_configured_routing(hook_package_label(package_info))
        require_claude_native_events(
            self.descriptors_for("claude"),
            lambda path: _parse_hook_json(path, logger=_LOG),
            event_maps,
        )

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


def resolve_plugin_root_hook_script(
    package_path: Path,
    rel_path: str,
    *,
    hook_file_dir: Path | None = None,
) -> Path:
    """Resolve a plugin-root resource, optionally accepting a producer hooks root.

    Installation keeps package-root semantics. Producers may supply the source
    descriptor directory for resources authored relative to that hooks root.
    Return the lexical path so callers can reject symlinked source components.
    """
    candidate = package_path / rel_path
    resolved = ensure_path_within(candidate, package_path)
    if hook_file_dir is None:
        return resolved
    if candidate.exists() or candidate.is_symlink():
        return candidate
    fallback = hook_file_dir / rel_path
    ensure_path_within(fallback, package_path)
    return fallback if fallback.exists() or fallback.is_symlink() else candidate


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
                source_file = resolve_plugin_root_hook_script(
                    package_path,
                    plugin_root_relative_path(match.group(1)),
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
    hook_files: list[Path],
    parse_hook_json: Callable[[Path], dict | None],
    iter_bundle_files: Callable[..., list[Path]],
    merge_target_names: Iterable[str],
) -> HookSourceSelection:
    """Select descriptors and bundle assets for every supported target.

    Filenames never route: each supported target receives every descriptor.
    Targets where the retired filename convention selected fewer files are
    recorded so consumers fail closed instead of widening executable reach.
    """
    all_descriptors = set(hook_files)
    descriptors = configured_hook_file_selection(hook_files)
    conflicts: dict[str, tuple[str, ...]] = {}
    descriptors_by_target: dict[str, frozenset[Path]] = {}
    bundles_by_target: dict[str, frozenset[Path]] = {}
    parsed_hooks: dict[Path, dict | None] = {}
    supported_targets = {"copilot", "kiro", *merge_target_names}

    for target_name in dict.fromkeys(target_names):
        if target_name not in supported_targets:
            continue
        widened = legacy_routing_widening(hook_files, target_name)
        if widened:
            conflicts[target_name] = tuple(
                sorted(portable_relpath(path, package_path) for path in widened)
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

    return HookSourceSelection(descriptors_by_target, bundles_by_target, conflicts)
