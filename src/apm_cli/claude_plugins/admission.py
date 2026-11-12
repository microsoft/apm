"""Admission of intact legacy plugins to Claude's native loader."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from apm_cli.core.scope import is_user_scope
from apm_cli.deps.plugin_parser import (
    has_normalized_plugin_skill_sources_receipt,
    parse_plugin_manifest,
)
from apm_cli.models.apm_package import PackageInfo, PackageType
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.utils.content_hash import compute_native_bundle_hash
from apm_cli.utils.path_security import PathTraversalError, has_symlink_component


class ClaudeNativePluginError(RuntimeError):
    """Native registration cannot safely honor the requested package."""


@dataclass(frozen=True, slots=True)
class ClaudeNativePlugin:
    """An admitted dependency, not a claim of native activation or execution."""

    dependency_key: str
    name: str
    source_root: Path
    bundle_hash: str
    source_pin: str = ""
    target_name: ClassVar[str] = "claude"


def fingerprint_native_claude_source(root: Path) -> str:
    """Translate canonical native fingerprint failures at the Claude boundary."""
    try:
        return compute_native_bundle_hash(root)
    except (OSError, PathTraversalError) as exc:
        raise ClaudeNativePluginError(
            f"Cannot fingerprint native Claude source {root}: unreadable data, unsafe path or "
            "symlink. Restore the intact package before retrying."
        ) from exc


_MANIFEST_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "commands",
        "agents",
        "skills",
        "hooks",
    }
)
_UNSUPPORTED_ROOTS = (
    ".mcp.json",
    ".lsp.json",
    "bin",
    ".apm/bin",
    ".apm/canvas",
    "settings.json",
    "output-styles",
    "rules",
)


def plan_native_claude_plugin(
    package_info: PackageInfo,
    *,
    package_name: str,
    targets: list[Any],
    scope: Any,
    skill_subset: tuple[str, ...] | None,
    hooks_approved: bool,
    skip_bin: bool,
) -> ClaudeNativePlugin | None:
    """Admit a complete, approved plugin without altering native selection.

    Normalized virtual plugins in older locks can have SKILL_BUNDLE type.
    Only the parser-owned normalization receipt admits that compatibility
    case; an incidental Claude manifest never overrides APM package intent.
    """
    if not is_user_scope(scope) or not any(t.name == "claude" for t in targets):
        return None
    root = Path(package_info.install_path)
    package_type = package_info.package_type
    reference = package_info.dependency_ref
    normalized_virtual = (
        package_type is PackageType.SKILL_BUNDLE
        and reference is not None
        and reference.is_virtual
        and has_normalized_plugin_skill_sources_receipt(root)
    )
    if package_type is not PackageType.MARKETPLACE_PLUGIN and not normalized_virtual:
        return None
    manifest_path = root / ".claude-plugin/plugin.json"
    if not manifest_path.is_file():
        raise ClaudeNativePluginError(
            "Native Claude registration requires an intact .claude-plugin/plugin.json."
        )
    if skill_subset_filter_tokens(skill_subset) is not None:
        raise ClaudeNativePluginError(
            "Native Claude plugins cannot honor a skill subset. Install the complete plugin "
            "or exclude Claude from this dependency's targets."
        )
    if root.is_symlink() or has_symlink_component(root, manifest_path):
        raise ClaudeNativePluginError("Native Claude manifest path must not contain a symlink.")
    try:
        manifest = parse_plugin_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise ClaudeNativePluginError("Cannot read the native Claude plugin manifest.") from exc
    unknown = set(manifest) - _MANIFEST_FIELDS
    unsupported = [name for name in _UNSUPPORTED_ROOTS if (root / name).exists()]
    if unknown or unsupported:
        raise ClaudeNativePluginError(
            "Native Claude plugin has unqualified capabilities: "
            + ", ".join(sorted(unknown) + unsupported)
            + ". This route supports intact commands, skills, agents and approved hooks only."
        )
    has_hooks = "hooks" in manifest or (root / "hooks").exists()
    if has_hooks and not hooks_approved:
        raise ClaudeNativePluginError(
            "Native Claude plugin hooks are not approved. Review and approve this dependency's "
            "hooks through allowExecutables, or exclude Claude from its targets."
        )
    name = manifest.get("name")
    if not isinstance(name, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name) is None:
        raise ClaudeNativePluginError("Native Claude plugin requires a safe, explicit plugin name.")
    return ClaudeNativePlugin(
        package_name, name, root.resolve(), fingerprint_native_claude_source(root)
    )
