"""Deprecated hook filename-routing helpers."""

from __future__ import annotations

from pathlib import Path

from apm_cli.utils.console import _rich_warning

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
    """Return only hook files intended for *target_key*."""
    warning_key = package_identity or package_name
    specific: list[Path] = []
    universal: list[Path] = []
    for hook_file in hook_files:
        allowed_targets = _hook_file_allowed_targets(hook_file)
        if allowed_targets is None:
            _warn_unresolved_target_tokens_if_needed(
                warned_packages,
                warning_key,
                package_name,
                package_identity,
                hook_file.name,
                _hook_file_unresolved_target_tokens(hook_file),
            )
            universal.append(hook_file)
        elif target_key in allowed_targets:
            _warn_if_needed(
                warned_packages,
                warning_key,
                package_name,
                package_identity,
                hook_file.name,
                sorted(allowed_targets),
            )
            specific.append(hook_file)

    return _dedupe_selected_hook_files(specific if specific else universal)


def _hook_file_allowed_targets(hook_file: Path) -> set[str] | None:
    """Return explicit targets for a hook file, or None for universal files.

    Filename routing is a deprecated package-authoring convenience, not an
    authorization boundary. Unknown stems intentionally stay universal.
    """
    stem_lower = hook_file.stem.lower()
    for token, allowed_targets in _HOOK_FILE_TARGET_TOKENS.items():
        if stem_lower == f"hooks-{token}":
            return allowed_targets

    if stem_lower.endswith("-hooks"):
        segments = stem_lower[: -len("-hooks")].split("-")
        per_segment = [_HOOK_FILE_TARGET_TOKENS.get(segment) for segment in segments]
        suffix_targets = _target_suffix_segments(segments)
        if len(suffix_targets) >= 2:
            # Combined manifest (e.g. claude-codex-hooks): union of every
            # target suffix token, so the file is selected for each.
            return _union_target_sets(suffix_targets)
        if per_segment and per_segment[-1]:
            # `<token>-hooks` or `*-<token>-hooks`: route by the final target
            # token. Earlier non-target segments are descriptive prefixes.
            return per_segment[-1]

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


def _hook_file_unresolved_target_tokens(hook_file: Path) -> list[str]:
    """Return known target tokens from stems that still resolve universal."""
    stem_lower = hook_file.stem.lower()
    if not stem_lower.endswith("-hooks"):
        return []
    segments = stem_lower[: -len("-hooks")].split("-")
    if _target_suffix_segments(segments):
        return []
    return sorted({segment for segment in segments if segment in _HOOK_FILE_TARGET_TOKENS})


def _warn_if_needed(
    warned_packages: set[str] | None,
    warning_key: str,
    package_name: str,
    package_identity: str,
    hook_filename: str,
    matched_targets: list[str],
) -> None:
    """Emit the deprecated filename-routing warning once per package."""
    if warned_packages is None or warning_key in warned_packages:
        return
    _rich_warning(
        _deprecated_filename_routing_warning(
            package_name,
            package_identity,
            hook_filename,
            matched_targets,
        ),
        symbol="warning",
    )
    warned_packages.add(warning_key)


def _warn_unresolved_target_tokens_if_needed(
    warned_packages: set[str] | None,
    warning_key: str,
    package_name: str,
    package_identity: str,
    hook_filename: str,
    token_names: list[str],
) -> None:
    """Emit a warning when a target-looking filename falls back to universal."""
    if warned_packages is None or not token_names:
        return
    unresolved_key = f"{warning_key}:unresolved:{hook_filename.lower()}"
    if unresolved_key in warned_packages:
        return
    _rich_warning(
        _unresolved_filename_routing_warning(
            package_name,
            package_identity,
            hook_filename,
            token_names,
        ),
        symbol="warning",
    )
    warned_packages.add(unresolved_key)


def _deprecated_filename_routing_warning(
    package_name: str,
    package_identity: str,
    hook_filename: str,
    matched_targets: list[str],
) -> str:
    """Return the user-facing filename-routing deprecation warning."""
    targets_csv = ", ".join(matched_targets)
    pkg_label = package_name or package_identity or "<owner/repo>"
    identity = package_identity or package_name or "<owner/repo>"
    return (
        f"{pkg_label}: filename-based target routing is deprecated.\n"
        f"    '{hook_filename}' routes via suffix to [{targets_csv}].\n"
        "    Update your apm.yml dependency to object form:\n"
        "\n"
        f"      - git: {identity}\n"
        f"        targets: [{targets_csv}]\n"
        "\n"
        "    See: https://microsoft.github.io/apm/reference/manifest-schema/#412-object-form"
    )


def _unresolved_filename_routing_warning(
    package_name: str,
    package_identity: str,
    hook_filename: str,
    token_names: list[str],
) -> str:
    """Return the warning for target-looking stems treated as universal."""
    tokens_csv = ", ".join(token_names)
    pkg_label = package_name or package_identity or "<owner/repo>"
    return (
        f"{pkg_label}: hook filename routing could not resolve '{hook_filename}'.\n"
        f"    The stem contains target token(s) [{tokens_csv}] but does not end\n"
        "    with a recognized target suffix, so APM treats it as universal.\n"
        "    Rename the file to '<prefix>-<target>-hooks.json' or prefer\n"
        "    per-dependency targets: [...] in apm.yml."
    )


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
