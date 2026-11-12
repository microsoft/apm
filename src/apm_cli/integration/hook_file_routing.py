"""Migration guard for retired hook filename target routing (#3226).

Hook filenames no longer choose destinations: every discovered hook file
reaches every effective target (project active targets intersected with the
consumer's dependency ``targets:`` and the package's own ``target:``).

The retired convention (``*-<target>-hooks.json`` / ``hooks-<target>.json``)
preferred target-named files over generic ones. Dropping it can therefore
activate another target's scripts or previously shadowed generic hooks. This
module only reproduces the retired selection to detect that widening, so
legacy reliance fails with migration guidance instead of silently gaining
executable reach. It never routes hooks.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apm_cli.hook_contract import HookContractError
from apm_cli.utils.diagnostics import printable_ascii_text

_HOOK_FILE_TARGET_TOKENS: dict[str, set[str]] = {
    "copilot": {"copilot", "vscode"},
    "vscode": {"copilot", "vscode"},
    "cursor": {"cursor"},
    "claude": {"claude"},
    "codex": {"codex"},
    "gemini": {"gemini"},
    "antigravity": {"antigravity"},
    "windsurf": {"windsurf"},
    "kiro": {"kiro"},
}

_MIGRATION_DOCS = (
    "https://microsoft.github.io/apm/producer/author-primitives/hooks-and-commands/"
    "#migrating-from-filename-routing"
)


def configured_hook_file_selection(hook_files: list[Path]) -> list[Path]:
    """Return the hook files every effective target receives."""
    return _dedupe_selected_hook_files(hook_files)


def legacy_hook_file_selection(hook_files: list[Path], target_key: str) -> list[Path]:
    """Return what the retired filename convention selected for *target_key*."""
    specific: list[Path] = []
    universal: list[Path] = []
    for hook_file in hook_files:
        allowed_targets = _hook_file_allowed_targets(hook_file)
        if allowed_targets is None:
            universal.append(hook_file)
        elif target_key in allowed_targets:
            specific.append(hook_file)
    return _dedupe_selected_hook_files(specific if specific else universal)


def legacy_routing_widening(hook_files: list[Path], target_key: str) -> list[Path]:
    """Return files that configured routing activates beyond the retired selection."""
    legacy = set(legacy_hook_file_selection(hook_files, target_key))
    return [path for path in configured_hook_file_selection(hook_files) if path not in legacy]


def hook_package_label(package_info: Any) -> str:
    """Return a printable identity for hook diagnostics."""
    canonical = getattr(package_info, "get_canonical_dependency_string", None)
    label = ""
    if callable(canonical):
        try:
            label = str(canonical() or "")
        except (AttributeError, TypeError, ValueError):
            label = ""
    if not label:
        package = getattr(package_info, "package", None)
        label = str(getattr(package, "name", "") or "")
    if not label:
        label = Path(str(getattr(package_info, "install_path", "") or "package")).name
    return printable_ascii_text(label)


def raise_for_legacy_routing(
    conflicts: Mapping[str, tuple[str, ...]],
    package_label: str,
) -> None:
    """Fail actionably when retired filename routing hid hook files."""
    if not conflicts:
        return
    lines = [
        f"{package_label}: hook filename target routing was removed; every hook file "
        "now reaches every configured target.",
    ]
    for target_name in sorted(conflicts):
        files = ", ".join(printable_ascii_text(path) for path in conflicts[target_name])
        lines.append(f"    Target '{target_name}' would newly activate: {files}")
    lines.extend(
        [
            "    APM refused to install these hooks so no script gains executable reach",
            "    the old filename convention excluded. To migrate, declare the intended",
            "    targets in the package's own apm.yml (target: <name>) or narrow this",
            "    dependency with object-form 'targets: [...]' in your apm.yml, and move",
            "    hooks meant for different targets into separate packages.",
            f"    See: {_MIGRATION_DOCS}",
        ]
    )
    raise HookContractError("\n".join(lines))


def _hook_file_allowed_targets(hook_file: Path) -> set[str] | None:
    """Return the retired convention's targets for a hook file, or None if generic."""
    stem_lower = hook_file.stem.lower()
    for token, allowed_targets in _HOOK_FILE_TARGET_TOKENS.items():
        if stem_lower == f"hooks-{token}":
            return allowed_targets

    if stem_lower.endswith("-hooks"):
        segments = stem_lower[: -len("-hooks")].split("-")
        suffix_targets = _target_suffix_segments(segments)
        matched: set[str] = set()
        for segment_targets in suffix_targets:
            matched |= segment_targets
        if matched:
            return matched

    return None


def _target_suffix_segments(segments: list[str]) -> list[set[str]]:
    """Return contiguous target-token matches at the end of a stem."""
    suffix: list[set[str]] = []
    for segment in reversed(segments):
        segment_targets = _HOOK_FILE_TARGET_TOKENS.get(segment)
        if segment_targets is None:
            break
        suffix.append(segment_targets)
    suffix.reverse()
    return suffix


def _dedupe_selected_hook_files(selected: list[Path]) -> list[Path]:
    """Deduplicate selected hook files by filename while preserving order."""
    result: list[Path] = []
    seen_names: set[str] = set()
    for hook_file in selected:
        name_key = hook_file.name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        result.append(hook_file)
    return result
