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
    """Return explicit targets for a hook file, or None for universal files."""
    stem_lower = hook_file.stem.lower()
    for token, allowed_targets in _HOOK_FILE_TARGET_TOKENS.items():
        if (
            stem_lower == f"{token}-hooks"
            or stem_lower.endswith(f"-{token}-hooks")
            or stem_lower == f"hooks-{token}"
        ):
            return allowed_targets
    return None


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
        )
    )
    warned_packages.add(warning_key)


def _deprecated_filename_routing_warning(
    package_name: str,
    package_identity: str,
    hook_filename: str,
    matched_targets: list[str],
) -> str:
    """Return the user-facing filename-routing deprecation warning."""
    targets_csv = ", ".join(matched_targets)
    pkg_label = package_name or package_identity or "unknown"
    identity = package_identity or package_name or pkg_label
    return (
        f"[!] {pkg_label}: filename-based target routing is deprecated.\n"
        f"    '{hook_filename}' routes via suffix to [{targets_csv}].\n"
        "    Add to your apm.yml:\n"
        "\n"
        f"      - git: {identity}\n"
        f"        targets: [{targets_csv}]\n"
        "\n"
        "    See: https://apm.github.io/docs/guides/per-dep-targets"
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
