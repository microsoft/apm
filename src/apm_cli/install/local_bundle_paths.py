"""Path routing helpers for local bundle installation."""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path
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

# Target formats that need a primitive renderer; an opaque bundle copy would
# write plugin-native Markdown under their extension (#3706).
_UNSUPPORTED_PLUGIN_FORMAT_IDS = frozenset({"gemini_command"})
# Target formats local bundle install renders from a packed Markdown source
# instead of copying it (#3714); any other source for them is skipped.
_RENDERED_PLUGIN_FORMAT_IDS = frozenset({"codex_agent"})


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
    hooks_approved: bool,
    canvas_approved: bool,
) -> str | None:
    """Return a bundle path relative to a target-owned deployment root.

    Only primitive files the target deploys are routed. Hook and canvas files
    additionally require *hooks_approved* / *canvas_approved*.
    """
    validate_path_segments(rel_path, context="bundle deploy path")
    if is_plugin_hook_path(rel_path):
        return None
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    if _unapproved_executable_kind(
        deploy_rel, hooks_approved=hooks_approved, canvas_approved=canvas_approved
    ):
        return None
    return _lower_to_target(deploy_rel, target)


def bundle_deploy_skip_warning(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile | None = None,
    hooks_approved: bool,
    canvas_approved: bool,
) -> str | None:
    """Return an explicit warning for a deliberately skipped bundle path."""
    validate_path_segments(rel_path, context="bundle deploy path")
    if is_plugin_hook_path(rel_path):
        return None
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    if kind := _unapproved_executable_kind(
        deploy_rel, hooks_approved=hooks_approved, canvas_approved=canvas_approved
    ):
        return (
            f"Skipped packed bundle {kind} file {rel_path!r}: {kind} from this bundle "
            "is not approved for its exact content. Run 'apm policy explain' "
            "and approve the bundle only if policy permits."
        )
    target_name = target.name if target is not None else "this target"
    if target is not None and not _is_primitive_deploy_path(deploy_rel, target):
        return (
            f"Skipped packed bundle path {rel_path!r} for target {target_name}: "
            "it is not a primitive file APM deploys for that target."
        )
    route = _plugin_layout_route(deploy_rel, target)
    if route is None:
        return None
    _head, _primitive_kind, _spec, mapping = route
    if not _is_unsupported_route(deploy_rel, mapping):
        return None
    return (
        f"Skipped packed bundle path {deploy_rel!r} for target {target.name}: "
        f"local bundle install cannot convert plugin-native files to "
        f"{mapping.format_id} ({mapping.extension}) yet. Repack for that "
        "target or install from source."
    )


def bundle_render_format_id(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile,
) -> str | None:
    """Return the target format a routed bundle path must be rendered to.

    ``None`` means the file is copied as packed. Only a plugin-native Markdown
    source whose target mapping is a rendered format (``codex_agent``) is
    rendered; the caller pairs this with :func:`bundle_deploy_relative_path`.
    """
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    route = _plugin_layout_route(deploy_rel, target)
    if route is None or not _renders_from_markdown(deploy_rel, route[3]):
        return None
    return route[3].format_id


def _renders_from_markdown(deploy_rel: str, mapping: Any) -> bool:
    return mapping.format_id in _RENDERED_PLUGIN_FORMAT_IDS and deploy_rel.endswith(".md")


def _is_unsupported_route(deploy_rel: str, mapping: Any) -> bool:
    if mapping.format_id in _UNSUPPORTED_PLUGIN_FORMAT_IDS:
        return True
    return mapping.format_id in _RENDERED_PLUGIN_FORMAT_IDS and not _renders_from_markdown(
        deploy_rel, mapping
    )


def is_plugin_hook_path(rel_path: str) -> bool:
    """Return whether a packed path is plugin-native hook config or resources.

    Plugin hooks are registered by ``local_bundle_hooks`` through the hook
    integrator; copying them as files would leave ``hooks.json`` unregistered
    and lower hook scripts through the hook descriptor extension.
    """
    from ..bundle.plugin_layout import PLUGIN_HOOKS_CONFIG, PLUGIN_LAYOUT

    rel_path = rel_path.removeprefix(f"{COM_MICROSOFT_APM_NAMESPACE}/")
    return (
        rel_path == PLUGIN_HOOKS_CONFIG
        or rel_path.split("/", 1)[0] == PLUGIN_LAYOUT["hooks"].plugin_dir
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


def _unapproved_executable_kind(
    deploy_rel: str, *, hooks_approved: bool, canvas_approved: bool
) -> str | None:
    """Return the executable kind of a target-relative path that lacks approval."""
    from ..bundle.plugin_layout import PLUGIN_LAYOUT

    head = deploy_rel.split("/", 1)[0]
    if not hooks_approved and head == PLUGIN_LAYOUT["hooks"].plugin_dir:
        return "hooks"
    if not canvas_approved and head == PLUGIN_LAYOUT["extensions"].plugin_dir:
        return "canvas"
    return None


def _is_primitive_deploy_path(deploy_rel: str, target: TargetProfile) -> bool:
    """Return whether a target-relative path is under a primitive directory.

    Files directly under a target root (runtime settings, MCP configuration)
    and directories no primitive mapping owns are never deployed from a bundle.
    """
    from ..bundle.plugin_layout import PLUGIN_LAYOUT

    if "/" not in deploy_rel:
        return False
    head = deploy_rel.split("/", 1)[0]
    if head in PLUGIN_LAYOUT:
        return True
    return any(
        mapping.subdir and mapping.subdir.split("/", 1)[0] == head
        for mapping in (target.primitives or {}).values()
    )


def _lower_to_target(rel_path: str, target: TargetProfile | None) -> str | None:
    """Lower a plugin-native bundle path into one target's deploy layout."""
    if target is None:
        return rel_path
    if not _is_primitive_deploy_path(rel_path, target):
        return None
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
    if _is_unsupported_route(rel_path, mapping):
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


def declared_bundle_exec_types(
    bundle_info, mcp_deps: list, lsp_deps: list, *, create_config: bool
) -> tuple[str, ...]:
    """Executable types the bundle ships, before trust filters them."""
    from ..core.experimental import is_enabled
    from ..integration.canvas_integrator import is_canvas_bundle_path

    files = list(bundle_pack_files(bundle_info))
    if not files and bundle_info.source_dir is not None:
        root = Path(bundle_info.source_dir)
        files = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
    declared = []
    if any(is_plugin_hook_path(path) for path in files):
        declared.append("hooks")
    if mcp_deps:
        declared.append("mcp")
    if lsp_deps:
        declared.append("lsp")
    if any(is_canvas_bundle_path(path) for path in files) and is_enabled(
        "canvas", create_config=create_config
    ):
        declared.append("canvas")
    return tuple(declared)
