"""Fail-closed migration from historical filename-based hook selection.

Filenames never select a destination or a descriptor subset. Historical
recognition is retained only to detect executable reach that removal would
otherwise silently widen.
"""

from __future__ import annotations

from pathlib import Path

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


def filter_hook_files_for_target(
    hook_files: list[Path],
    target_key: str,
    *,
    package_name: str = "",
    package_identity: str = "",
    warned_packages: set[str] | None = None,
) -> list[Path]:
    """Admit the complete discovery inventory or require an explicit migration.

    The compatibility name is retained for the shared source-selection seam.
    Package/dependency target configuration has already selected *target_key*.
    Even an explicit target cannot make formerly shadowed files safe to add.
    """
    specific: list[Path] = []
    universal: list[Path] = []
    for hook_file in hook_files:
        allowed_targets = _hook_file_allowed_targets(hook_file)
        if allowed_targets is None:
            universal.append(hook_file)
        elif target_key in allowed_targets:
            specific.append(hook_file)

    historical = set(_dedupe_selected_hook_files(specific if specific else universal))
    newly_reachable = [path for path in hook_files if path not in historical]
    if newly_reachable:
        names = ", ".join(printable_ascii_text(path.name) for path in newly_reachable)
        package = printable_ascii_text(package_name or package_identity or "<package>")
        raise HookContractError(
            f"Cannot install hooks for {package} to {target_key}: {names} "
            "were previously excluded or shadowed by filename routing. "
            "Filename routing has been removed; installing all discovered hooks "
            "would widen executable reach. Split differing native definitions into "
            "separate packages with target/targets in apm.yml, retain only each package's "
            "intended hooks, and select those packages with dependency targets. "
            "A targets declaration alone does not resolve shadowed files. "
            "Review the scripts before renaming or moving them, then reinstall."
        )
    return list(hook_files)


def _hook_file_allowed_targets(hook_file: Path) -> set[str] | None:
    """Recognize historical reach solely for the migration check.

    This frozen vocabulary must never select destinations or filter sources.
    Unknown stems historically stayed universal.
    """
    stem_lower = hook_file.stem.lower()
    for token, allowed_targets in _HOOK_FILE_TARGET_TOKENS.items():
        if stem_lower == f"hooks-{token}":
            return allowed_targets

    if stem_lower.endswith("-hooks"):
        segments = stem_lower[: -len("-hooks")].split("-")
        suffix_targets = _target_suffix_segments(segments)
        if len(suffix_targets) >= 2:
            # Combined manifest (e.g. claude-codex-hooks): union of every
            # target suffix token, so the file is selected for each.
            return _union_target_sets(suffix_targets)
        if suffix_targets:
            # `<token>-hooks` or `*-<token>-hooks`: route by the final target
            # token. Earlier non-target segments are descriptive prefixes.
            return suffix_targets[0]

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


def _union_target_sets(target_sets: list[set[str]]) -> set[str]:
    """Return the union of target sets while preserving simple caller logic."""
    matched: set[str] = set()
    for segment_targets in target_sets:
        matched |= segment_targets
    return matched


def _dedupe_selected_hook_files(selected: list[Path]) -> list[Path]:
    """Reproduce historical duplicate suppression for migration evidence."""
    result: list[Path] = []
    seen_names: set[str] = set()
    for hook_file in selected:
        name_key = hook_file.name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        result.append(hook_file)
    return result
