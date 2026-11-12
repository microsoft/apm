"""Path routing helpers for local bundle installation."""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, Any

from ..agent_plugins.constants import COM_MICROSOFT_APM_NAMESPACE
from ..utils.path_security import PathTraversalError, validate_path_segments

if TYPE_CHECKING:
    from ..bundle.local_bundle import LocalBundleInfo
    from ..integration.targets import TargetProfile

_APM_PRIMITIVE_SUFFIXES = (
    ".instructions.md",
    ".prompt.md",
    ".agent.md",
    ".mdc",
    ".toml",
    ".json",
    ".md",
)

_UNSUPPORTED_PLUGIN_FORMAT_IDS = frozenset({"gemini_command"})


def bundle_pack_files(bundle_info: LocalBundleInfo) -> dict[str, str]:
    """Read the packed file manifest from bundle lockfile metadata."""
    if not bundle_info.lockfile:
        return {}
    pack = bundle_info.lockfile.get("pack") or {}
    bundle_files = pack.get("bundle_files") or {}
    if not isinstance(bundle_files, dict):
        return {}
    return {str(path): str(digest) for path, digest in bundle_files.items()}


@cache
def known_bundle_deploy_prefixes() -> frozenset[str]:
    """Return all target-owned roots that can prefix packed bundle paths."""
    from ..integration.targets import KNOWN_TARGETS

    return frozenset(
        f"{root.strip('/')}/"
        for profile in KNOWN_TARGETS.values()
        for root in (
            profile.root_dir,
            *(
                mapping.deploy_root
                for mapping in profile.primitives.values()
                if mapping.deploy_root
            ),
        )
    )


def target_bundle_deploy_prefixes(target: TargetProfile) -> frozenset[str]:
    """Return deployment roots owned by one target."""
    return frozenset(
        f"{root.strip('/')}/"
        for root in (
            target.root_dir,
            *(mapping.deploy_root for mapping in target.primitives.values() if mapping.deploy_root),
        )
    )


def bundle_slug_validation_error(slug: object) -> str | None:
    """Return an error when a bundle slug is unsafe for path construction."""
    slug_text = str(slug)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if (
        not slug_text
        or any(char not in allowed for char in slug_text)
        or slug_text.startswith(".")
        or slug_text.endswith(".")
        or ".." in slug_text
    ):
        return "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
    try:
        validate_path_segments(slug_text, context="bundle slug")
    except PathTraversalError as exc:
        return str(exc)
    return None


def bundle_deploy_relative_path(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile | None = None,
) -> str | None:
    """Return a bundle path relative to a target-owned deployment root."""
    validate_path_segments(rel_path, context="bundle deploy path")
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    return _lower_to_target(deploy_rel, target)


def bundle_deploy_skip_warning(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile | None = None,
) -> str | None:
    """Return an explicit warning for a deliberately skipped bundle path."""
    validate_path_segments(rel_path, context="bundle deploy path")
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    route = _plugin_layout_route(deploy_rel, target)
    if route is None:
        return None
    _head, _primitive_kind, _spec, mapping = route
    if mapping.format_id not in _UNSUPPORTED_PLUGIN_FORMAT_IDS:
        return None
    return (
        f"Skipped packed bundle path {deploy_rel!r} for target {target.name}: "
        f"local bundle install cannot convert plugin-native files to "
        f"{mapping.format_id} ({mapping.extension}) yet. Repack for that "
        "target or install from source."
    )


def _deploy_relative_candidate(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
) -> str | None:
    """Strip any target deploy root prefix from a bundle path."""
    namespace_prefix = f"{COM_MICROSOFT_APM_NAMESPACE}/"
    if rel_path.startswith(namespace_prefix):
        rel_path = rel_path.removeprefix(namespace_prefix)
    parts = rel_path.split("/")
    path_prefixes = {"/".join(parts[:index]) + "/" for index in range(1, len(parts))}
    matched_prefixes = path_prefixes & known_deploy_prefixes
    allowed_matches = matched_prefixes & allowed_prefixes
    if matched_prefixes and not allowed_matches:
        return None
    matched_prefix = max(allowed_matches, key=len, default="")
    return rel_path[len(matched_prefix) :] if matched_prefix else rel_path


def _lower_to_target(rel_path: str, target: TargetProfile | None) -> str | None:
    """Lower a plugin-native bundle path into one target's deploy layout."""
    if target is None or "/" not in rel_path:
        return rel_path
    route = _plugin_layout_route(rel_path, target)
    if route is None:
        from ..bundle.plugin_layout import PLUGIN_LAYOUT

        head = rel_path.split("/", 1)[0]
        if head not in PLUGIN_LAYOUT:
            return rel_path
        if head == "instructions" and target.compile_family:
            return rel_path
        return None
    _head, _primitive_kind, spec, mapping = route
    if mapping.format_id in _UNSUPPORTED_PLUGIN_FORMAT_IDS:
        return None
    parts = rel_path.split("/", 1)[1].split("/")
    parts[-1] = _retarget_basename(parts[-1], mapping, spec.apm_basename_fn)
    lowered_tail = "/".join(parts)
    if not mapping.subdir:
        return lowered_tail
    return f"{mapping.subdir}/{lowered_tail}"


def _plugin_layout_route(
    rel_path: str, target: TargetProfile | None
) -> tuple[str, str, Any, Any] | None:
    """Return the plugin layout spec and target mapping for a bundle path."""
    if target is None or "/" not in rel_path:
        return None
    from ..bundle.plugin_layout import PLUGIN_LAYOUT

    head = rel_path.split("/", 1)[0]
    spec = PLUGIN_LAYOUT.get(head)
    if spec is None:
        return None
    for primitive_kind in spec.primitive_kinds:
        mapping = target.primitives.get(primitive_kind)
        if mapping is not None:
            return head, primitive_kind, spec, mapping
    return None


def _retarget_basename(name: str, mapping: Any, apm_basename_fn: Callable[[str], str]) -> str:
    """Retarget a plugin-native file basename through a PrimitiveMapping."""
    extension = mapping.extension
    if not extension or extension.startswith("/"):
        return name
    if not extension.startswith("."):
        return extension
    apm_name = apm_basename_fn(name)
    base_name = apm_name
    for suffix in _APM_PRIMITIVE_SUFFIXES:
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    return f"{base_name}{extension}"
