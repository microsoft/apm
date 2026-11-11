"""Target-aware source selection for deployable hook descriptors and bundles."""

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from apm_cli.hook_contract import HookContractError, walk_hook_commands
from apm_cli.integration.hook_bundle import _hook_source_root
from apm_cli.integration.hook_command_paths import (
    iter_plugin_root_paths,
    iter_relative_script_paths,
    normalize_quoted_plugin_root,
    plugin_root_relative_path,
    unresolved_plugin_root_references,
)
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)


@dataclass(frozen=True)
class HookSourceSelection:
    """Authorized hook descriptors and bundle assets grouped by target."""

    descriptor_files: dict[str, frozenset[Path]]
    bundle_files: dict[str, frozenset[Path]]
    inline_documents: dict[Path, dict] = field(default_factory=dict)

    def descriptors_for(self, target_name: str) -> list[Path]:
        """Return target-routed descriptors in stable path order."""
        return sorted(
            self.descriptor_files.get(target_name, frozenset()) - self.inline_documents.keys()
        )

    def inline_for(self, target_name: str) -> dict[Path, dict]:
        """Return native scoped documents, never inputs to global hook rendering."""
        return {
            path: self.inline_documents[path]
            for path in sorted(self.descriptor_files.get(target_name, frozenset()))
            if path in self.inline_documents
        }

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
    *,
    reject_symlinks: bool = False,
) -> Path | None:
    """Resolve a relative hook script path without escaping the package."""
    last_candidate: Path | None = None
    for base in _relative_hook_script_bases(package_path, hook_file_dir):
        lexical = base / rel_path
        if reject_symlinks and has_symlink_component(package_path, lexical):
            raise HookContractError(f"Hook resource {rel_path!r} crosses a symlink.")
        try:
            candidate = ensure_path_within(lexical, package_path)
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
    *,
    strict: bool = False,
) -> set[Path]:
    """Resolve existing package files referenced by a parsed hook document."""
    source_files: set[Path] = set()

    def admit(source_file: Path | None, reference: str) -> None:
        if (
            source_file is not None
            and source_file.is_file()
            and (not strict or not has_symlink_component(package_path, source_file))
        ):
            source_files.add(source_file)
        elif strict:
            raise HookContractError(
                f"Inline hook resource {reference!r} is missing, unsafe or not a file. "
                "Keep required resources inside the package and reinstall."
            )

    for declaration in walk_hook_commands(data):
        command = normalize_quoted_plugin_root(declaration.command)
        matched_references: set[str] = set()
        for match in iter_plugin_root_paths(command):
            matched_references.add(match.group(0))
            relative = plugin_root_relative_path(match.group(1))
            if strict and has_symlink_component(package_path, package_path / relative):
                raise HookContractError(f"Hook resource {relative!r} crosses a symlink.")
            try:
                source_file = resolve_plugin_root_hook_script(
                    package_path,
                    relative,
                )
            except PathTraversalError:
                if strict:
                    raise
                continue
            admit(source_file, match.group(0))
        if strict and set(unresolved_plugin_root_references(command)) - matched_references:
            raise HookContractError(
                "Inline hook has an unrepresentable package resource reference. "
                "Use a literal ${PLUGIN_ROOT}/path inside the package and reinstall."
            )
        for match in iter_relative_script_paths(command):
            source_file = _resolve_relative_hook_script(
                package_path,
                hook_file_dir,
                match.group(1)[2:].replace("\\", "/"),
                reject_symlinks=strict,
            )
            admit(source_file, match.group(1))
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
    inline_documents: dict[Path, dict] | None = None,
) -> HookSourceSelection:
    """Select descriptors and bundle assets for every supported target."""
    inline_documents = inline_documents or {}
    all_descriptors = set(hook_files) | inline_documents.keys()
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
        if target_name == "claude":
            descriptors.extend(sorted(inline_documents))
        descriptors_by_target[target_name] = frozenset(descriptors)
        source_roots: set[Path] = set()
        for hook_file in descriptors:
            if hook_file not in parsed_hooks:
                parsed_hooks[hook_file] = (
                    inline_documents[hook_file]
                    if hook_file in inline_documents
                    else parse_hook_json(hook_file)
                )
            data = parsed_hooks[hook_file]
            if data is None:
                continue
            for source_file in _referenced_hook_source_files(
                data,
                package_path,
                hook_file.parent,
                strict=hook_file in inline_documents,
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

    selected = set().union(*descriptors_by_target.values()) if descriptors_by_target else set()
    return HookSourceSelection(
        descriptors_by_target,
        bundles_by_target,
        inline_documents={
            path: data for path, data in inline_documents.items() if path in selected
        },
    )
