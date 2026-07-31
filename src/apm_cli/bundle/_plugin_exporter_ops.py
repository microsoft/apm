"""Provenance-aware helpers for plugin_exporter.export_plugin_bundle.

These functions handle the lockfile-attested content pipeline introduced
in issue #1999: mapping deployed_files entries to plugin-native output
paths, verifying file hashes, and deciding whether the unattested
apm_modules cache holds packable content.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from ..models.dependency.subsets import skill_subset_filter_tokens
from ..utils.path_security import PathTraversalError, ensure_path_within
from ..utils.paths import portable_relpath
from .attest import verify_attested_file

if TYPE_CHECKING:
    from ..deps.lockfile import LockedDependency


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _deployed_path_parts(rel_path: str) -> tuple[str, ...]:
    """Return safe POSIX path parts for a lockfile deployed_files entry."""
    rel = rel_path.replace("\\", "/")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or PureWindowsPath(rel).is_absolute():
        raise ValueError(f"Refusing to pack absolute deployed file path: {rel_path!r}")
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Refusing to pack unsafe deployed file path: {rel_path!r}")
    return parts


def _skill_name_from_deployed_parts(parts: tuple[str, ...]) -> str | None:
    """Return the deployed skill name when *parts* point inside a skills dir."""
    if len(parts) >= 3 and parts[0].startswith(".") and parts[1] == "skills":
        return parts[2]
    if len(parts) >= 2 and parts[0] == "skills":
        return parts[1]
    return None


def _plugin_rel_for_deployed_path(
    rel_path: str,
    skill_subset: set[str] | None,
) -> str | None:
    """Map an installed deployed_files path back to plugin-native output layout."""
    parts = _deployed_path_parts(rel_path)
    if not parts:
        return None

    if parts[0] == "skills":
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_subset and (skill_name is None or skill_name not in skill_subset):
            return None
        plugin_parts = list(parts)
    elif parts[0] in {"agents", "commands", "instructions", "extensions"}:
        plugin_parts = list(parts)
    elif len(parts) >= 3 and parts[0].startswith("."):
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_name is not None:
            if skill_subset and skill_name not in skill_subset:
                return None
            plugin_parts = ["skills", *parts[2:]]
        elif parts[1] == "agents":
            plugin_parts = ["agents", *parts[2:]]
        elif parts[1] in {"commands", "prompts"}:
            plugin_parts = ["commands", *parts[2:]]
        elif parts[1] in {"instructions", "rules", "steering"}:
            plugin_parts = ["instructions", *parts[2:]]
        elif parts[1] == "extensions":
            plugin_parts = ["extensions", *parts[2:]]
        elif parts[1] == "hooks":
            plugin_parts = ["hooks", *parts[2:]]
        else:
            return None
    elif len(parts) >= 2 and parts[0].startswith(".") and parts[1] == "hooks.json":
        plugin_parts = ["hooks.json"]
    else:
        return None

    if plugin_parts[0] == "commands" and plugin_parts[-1].endswith(".prompt.md"):
        from .plugin_exporter import _rename_prompt

        plugin_parts[-1] = _rename_prompt(plugin_parts[-1])
    return PurePosixPath(*plugin_parts).as_posix()


# ---------------------------------------------------------------------------
# Explicit-includes pipeline
# ---------------------------------------------------------------------------


def _collect_explicit_local_components(
    project_root: Path,
    includes: list[str],
) -> tuple[list[tuple[Path, str]], dict]:
    """Collect only explicitly listed local paths and validate each path."""
    components: list[tuple[Path, str]] = []
    hooks: dict = {}
    for declared_path in includes:
        parts = _deployed_path_parts(declared_path)
        candidate = project_root.joinpath(*parts)
        if candidate.is_symlink():
            raise ValueError(
                f"includes path {declared_path!r} is a symlink. "
                "Replace it with a regular file or directory."
            )
        try:
            source = ensure_path_within(candidate, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"includes path {declared_path!r} escapes the project root. "
                "Fix the path in apm.yml."
            ) from exc
        if not source.exists():
            raise ValueError(
                f"includes path {declared_path!r} does not exist. "
                "Fix the path in apm.yml or create it."
            )
        entries = [source] if source.is_file() else sorted(source.rglob("*"))
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(
                    f"Symlink found inside includes path {declared_path!r}: "
                    f"{entry.name}. Remove the symlink or list a regular path."
                )
        for file_path in (entry for entry in entries if entry.is_file()):
            try:
                file_path = ensure_path_within(file_path, project_root)
            except PathTraversalError as exc:
                raise ValueError(
                    f"A file inside includes path {declared_path!r} escapes the "
                    "project root. Remove the symlink or fix the path in apm.yml."
                ) from exc
            repo_relative = portable_relpath(file_path, project_root)
            is_hook = (
                repo_relative == "hooks.json"
                or repo_relative.startswith("hooks/")
                or repo_relative.startswith(".apm/hooks/")
            )
            if is_hook:
                try:
                    hook_data = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError) as exc:
                    raise ValueError(
                        f"Explicit hook include is not valid JSON: {repo_relative}"
                    ) from exc
                if not isinstance(hook_data, dict):
                    raise ValueError(
                        f"Explicit hook include must contain a JSON object: {repo_relative}"
                    )
                from .plugin_exporter import _deep_merge

                _deep_merge(hooks, hook_data, overwrite=False)
                continue
            plugin_relative = _plugin_rel_for_deployed_path(repo_relative, None)
            if plugin_relative is None:
                raise ValueError(
                    f"Explicit include path is not a packable primitive: {repo_relative}"
                )
            components.append((file_path, plugin_relative))
    return components, hooks


# ---------------------------------------------------------------------------
# Attested-content pipeline
# ---------------------------------------------------------------------------


def _verify_attested_hash(
    source: Path,
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Fail when a packed deployed file diverges from its attested hash."""
    rel = source.relative_to(project_root).as_posix()
    expected = dep.deployed_file_hashes.get(rel) if dep.deployed_file_hashes else None
    verify_attested_file(source, expected, dep.repo_url, rel)


def _append_deployed_component(
    components: list[tuple[Path, str]],
    source: Path,
    output_rel: str,
    seen_outputs: set[str],
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Append one deployed component unless it is unsafe or already mapped."""
    from .plugin_exporter import _validate_output_rel

    if output_rel in seen_outputs or not _validate_output_rel(output_rel):
        return
    if source.is_file() and not source.is_symlink():
        _verify_attested_hash(source, project_root, dep)
        components.append((source, output_rel))
        seen_outputs.add(output_rel)


def _collect_deployed_components(
    project_root: Path,
    dep: LockedDependency,
) -> list[tuple[Path, str]]:
    """Collect dependency components from lockfile deployed_files entries."""
    components: list[tuple[Path, str]] = []
    missing: list[str] = []
    seen_outputs: set[str] = set()
    skill_subset = skill_subset_filter_tokens(dep.skill_subset)

    for rel_path in dep.deployed_files:
        try:
            plugin_rel = _plugin_rel_for_deployed_path(rel_path, skill_subset)
        except ValueError as exc:
            raise ValueError(f"Cannot pack dependency {dep.repo_url}: {exc}") from exc
        if plugin_rel is None:
            continue
        source = project_root / rel_path
        try:
            source = ensure_path_within(source, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"Cannot pack dependency {dep.repo_url}: deployed file path "
                f"escapes the project root: {rel_path!r}"
            ) from exc
        if not source.exists():
            missing.append(rel_path)
            continue
        if source.is_symlink():
            continue
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                if not child.is_file() or child.is_symlink():
                    continue
                try:
                    ensure_path_within(child, project_root)
                except PathTraversalError:
                    continue
                child_rel = child.relative_to(source).as_posix()
                child_output = (PurePosixPath(plugin_rel) / child_rel).as_posix()
                _append_deployed_component(
                    components, child, child_output, seen_outputs, project_root, dep
                )
        else:
            _append_deployed_component(
                components, source, plugin_rel, seen_outputs, project_root, dep
            )

    if missing:
        shown_missing = missing[:10]
        remaining = len(missing) - len(shown_missing)
        suffix = f"\n  ... and {remaining} more" if remaining else ""
        raise ValueError(
            f"Cannot pack dependency {dep.repo_url}: installed files recorded "
            "in apm.lock.yaml are missing on disk. Run 'apm install' to "
            "restore them, then pack again:\n"
            + "\n".join(f"  - {path}" for path in shown_missing)
            + suffix
        )
    return components


# ---------------------------------------------------------------------------
# Cache probes (detect-only; never copy cache bytes into the bundle)
# ---------------------------------------------------------------------------


def _cache_would_contribute_primitives(
    install_path: Path,
    dep: LockedDependency,
) -> bool:
    """Return True if the unattested apm_modules cache holds packable primitives."""
    if not install_path.is_dir():
        return False
    from .plugin_exporter import (
        _collect_apm_components,
        _collect_bare_skill,
        _collect_root_plugin_components,
    )

    probe: list[tuple[Path, str]] = []
    probe.extend(_collect_apm_components(install_path / ".apm"))
    probe.extend(_collect_root_plugin_components(install_path))
    _collect_bare_skill(install_path, dep, probe)
    return bool(probe)


def _cache_would_contribute_hooks_or_mcp(install_path: Path) -> bool:
    """Return True if the unattested cache holds hooks-config or MCP-config."""
    if not install_path.is_dir():
        return False
    from .plugin_exporter import (
        _collect_hooks_from_apm,
        _collect_hooks_from_root,
        _collect_mcp,
    )

    if _collect_mcp(install_path):
        return True
    if _collect_hooks_from_apm(install_path / ".apm"):
        return True
    return bool(_collect_hooks_from_root(install_path))
