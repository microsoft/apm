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

# Target formats that need a primitive renderer; an opaque bundle copy would
# write plugin-native Markdown under their extension (#3706).
_UNSUPPORTED_PLUGIN_FORMAT_IDS = frozenset({"gemini_command"})
# Target formats local bundle install renders from a packed Markdown source
# instead of copying it (#3714); any other source for them is skipped.
_RENDERED_PLUGIN_FORMAT_IDS = frozenset({"codex_agent"})


# Bundle metadata never deploys to consumer projects (#1207 D2.a).
_BUNDLE_METADATA_FILES = frozenset(
    {
        "plugin.json",
        ".mcp.json",
        "mcp.json",
        ".lsp.json",
        "lsp.json",
        "com.microsoft.apm/mcp.json",
        "com.microsoft.apm/lsp.json",
    }
)


def deployable_bundle_pack_files(bundle_info: LocalBundleInfo) -> dict[str, str]:
    """Return packed file digests a bundle may deploy, minus bundle metadata.

    An older bundle without ``pack.bundle_files`` falls back to hashing every
    regular file except ``apm.lock.yaml`` so it never deploys zero files.
    Metadata matches case-insensitively (case-folding filesystems) and is
    dropped before any per-target loop so it never inflates skip counts.
    """
    pack_files = bundle_pack_files(bundle_info)
    if not pack_files:
        import hashlib

        bundle_dir: Path = bundle_info.source_dir
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            if rel.lower() != "apm.lock.yaml":
                pack_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return {
        rel: digest
        for rel, digest in pack_files.items()
        if rel.lower() not in _BUNDLE_METADATA_FILES
    }


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
    source: Path | None = None,
    hooks_approved: bool,
    canvas_approved: bool,
    rendering_extension: str | None = None,
) -> str | None:
    """Return a bundle path relative to a target-owned deployment root.

    Only primitive files the target deploys are routed. Hook and canvas files
    additionally require *hooks_approved* / *canvas_approved*; so does a
    Markdown *source* whose frontmatter declares ``hooks``.
    *rendering_extension* is the extension the packing install gave the file
    (:func:`bundle_rendering_extensions`).
    """
    validate_path_segments(rel_path, context="bundle deploy path")
    if is_plugin_hook_path(rel_path):
        return None
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    if _unapproved_executable_kind(
        deploy_rel, hooks_approved=hooks_approved, canvas_approved=canvas_approved
    ) or _declares_unapproved_hooks(
        rel_path,
        allowed_prefixes,
        known_deploy_prefixes,
        target,
        source,
        hooks_approved=hooks_approved,
    ):
        return None
    return _lower_to_target(deploy_rel, target, rendering_extension)


def bundle_deploy_skip_warning(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile | None = None,
    hooks_approved: bool,
    canvas_approved: bool,
    source: Path | None = None,
) -> str | None:
    """Return an explicit warning for a deliberately skipped bundle path."""
    validate_path_segments(rel_path, context="bundle deploy path")
    if is_plugin_hook_path(rel_path):
        return None
    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    if deploy_rel is None:
        return None
    kind = _unapproved_executable_kind(
        deploy_rel, hooks_approved=hooks_approved, canvas_approved=canvas_approved
    )
    if kind is None and _declares_unapproved_hooks(
        rel_path,
        allowed_prefixes,
        known_deploy_prefixes,
        target,
        source,
        hooks_approved=hooks_approved,
    ):
        kind = "hooks"
    if kind:
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


def bundle_rule_format_id(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile,
) -> str | None:
    """Return the rule format a packed APM instruction must be converted to.

    Plugin bundles carry instructions in the APM source rendering
    (``x.instructions.md`` with ``applyTo``, #3686). A target whose rule
    format transforms instructions (``claude_rules`` and the other
    ``RULE_FORMATS``) gets that rendering converted, exactly as ``apm install``
    converts it from source; any other packed file is copied as is.
    """
    from ..bundle.plugin_layout import PLUGIN_LAYOUT, primitive_file_suffix
    from ..integration.targets import RULE_FORMATS

    deploy_rel = _deploy_relative_candidate(rel_path, allowed_prefixes, known_deploy_prefixes)
    route = _plugin_layout_route(deploy_rel, target) if deploy_rel else None
    if route is None or route[2] is not PLUGIN_LAYOUT["instructions"]:
        return None
    if primitive_file_suffix(deploy_rel) != ".instructions.md":
        return None
    return route[3].format_id if route[3].format_id in RULE_FORMATS else None


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


def markdown_declares_hooks(path: Path, *, unreadable: bool = True) -> bool:
    """Whether a packed file's frontmatter declares ``hooks``.

    Every packed file is checked, whatever its suffix: a primitive may deploy
    as Markdown from another packed name. A file without a frontmatter fence
    declares none. Frontmatter that cannot be read or decoded always counts as
    declaring hooks. Frontmatter that is not valid YAML returns *unreadable*:
    by default it counts as declaring hooks too, so it never deploys without
    the bundle hook approval; a converter that judges YAML itself passes
    ``False``.
    """
    import yaml

    from ..utils.yaml_io import loads_frontmatter, read_frontmatter_block

    try:
        text = read_frontmatter_block(path)
    except (OSError, UnicodeError):
        return True
    try:
        metadata = loads_frontmatter(text).metadata
    except (ValueError, yaml.YAMLError):
        return unreadable
    return not isinstance(metadata, dict) or "hooks" in metadata


def _declares_unapproved_hooks(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    target: TargetProfile | None,
    source: Path | None,
    *,
    hooks_approved: bool,
) -> bool:
    """Whether a packed Markdown *source* declares frontmatter hooks without approval."""
    if hooks_approved or source is None:
        return False
    converted = target is not None and bool(
        bundle_rule_format_id(rel_path, allowed_prefixes, known_deploy_prefixes, target=target)
        or bundle_render_format_id(rel_path, allowed_prefixes, known_deploy_prefixes, target=target)
    )
    # Invalid YAML in a converted file is skipped by its converter, with the reason.
    return markdown_declares_hooks(source, unreadable=not converted)


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


def _lower_to_target(
    rel_path: str, target: TargetProfile | None, rendering_extension: str | None = None
) -> str | None:
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
    _head, primitive_kind, spec, mapping = route
    if _is_unsupported_route(rel_path, mapping):
        return None
    from ..bundle.plugin_layout import deployed_file_extension

    parts = rel_path.split("/", 1)[1].split("/")
    parts[-1] = _retarget_basename(
        parts[-1],
        deployed_file_extension(primitive_kind, mapping),
        spec.apm_basename_fn,
        rendering_extension,
    )
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


def _retarget_basename(
    name: str,
    extension: str | None,
    apm_basename_fn: Callable[[str], str],
    rendering_extension: str | None = None,
) -> str:
    """Retarget a plugin-native file basename to a target's deployed *extension*.

    A known *rendering_extension* is stripped exactly, so a Claude agent named
    ``rv.agent.md`` keeps its ``rv.agent`` stem instead of colliding with
    ``rv.md``; otherwise the longest primitive suffix is stripped.
    """
    if not extension or extension.startswith("/"):
        return name
    if not extension.startswith("."):
        return extension
    from ..bundle.plugin_layout import primitive_file_suffix

    if rendering_extension and len(name) > len(rendering_extension):
        if name.endswith(rendering_extension):
            return f"{name[: -len(rendering_extension)]}{extension}"
    apm_name = apm_basename_fn(name)
    suffix = primitive_file_suffix(apm_name)
    base_name = apm_name[: -len(suffix)] if suffix else apm_name
    return f"{base_name}{extension}"


def bundle_rendering_extensions(bundle_info: LocalBundleInfo) -> dict[str, str]:
    """Map packed files to the extension their deploying target gave them (#3686).

    This provenance tells a Claude agent named ``rv.agent`` apart from a
    Copilot rendering of ``rv``. ``pack.renderings`` (written from the final
    packed winners) is authoritative; a path without an entry lowers by
    suffix. Bundles packed before it fall back to the lockfile's
    ``deployed_files``, ignoring a path that several deployed files claim
    with different extensions.
    """
    from ..bundle.plugin_exporter import _plugin_rel_for_deployed_path
    from ..bundle.plugin_layout import deployed_rendering_extension

    lock = bundle_info.lockfile if isinstance(bundle_info.lockfile, dict) else {}
    recorded = (
        (lock.get("pack") or {}).get("renderings") if isinstance(lock.get("pack"), dict) else None
    )
    if isinstance(recorded, dict):
        return {k: v for k, v in recorded.items() if isinstance(k, str) and isinstance(v, str)}
    candidates: dict[str, set[str]] = {}
    for dep in lock.get("dependencies") or []:
        for deployed in (dep.get("deployed_files") or []) if isinstance(dep, dict) else []:
            extension = deployed_rendering_extension(str(deployed))
            try:
                rel = _plugin_rel_for_deployed_path(str(deployed), None) if extension else None
            except ValueError:
                continue
            if rel is not None:
                candidates.setdefault(rel, set()).add(extension)
    return {rel: next(iter(exts)) for rel, exts in candidates.items() if len(exts) == 1}


def superseded_bundle_renderings(
    rel_paths: list[str],
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
    *,
    target: TargetProfile,
    renderings: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map each bundle path that another path lowering to the same file replaces.

    Bundles packed before #3686 can carry one primitive in several target
    renderings (``instructions/py.md`` and ``instructions/py.instructions.md``)
    that lower to the same file. The rendering whose suffix is *target*'s own
    extension is kept, then the rendering a plugin bundle prefers. Returns
    ``{skipped: kept}``; callers name every skipped path.
    """
    from ..bundle.plugin_layout import (
        PLUGIN_RENDERING_PREFERENCE,
        deployed_file_extension,
        primitive_file_suffix,
    )

    groups: dict[str, list[tuple[bool, int, str]]] = {}
    for rel in sorted(rel_paths):
        deploy_rel = _deploy_relative_candidate(rel, allowed_prefixes, known_deploy_prefixes)
        preference = PLUGIN_RENDERING_PREFERENCE.get((deploy_rel or "").partition("/")[0])
        route = _plugin_layout_route(deploy_rel, target) if preference else None
        if route is None or _is_unsupported_route(deploy_rel, route[3]):
            continue
        known = (renderings or {}).get(rel)
        lowered = _lower_to_target(deploy_rel, target, known)
        if lowered is None:
            continue
        suffix = known or primitive_file_suffix(deploy_rel)
        rank = preference.index(suffix) if suffix in preference else len(preference)
        own = deployed_file_extension(route[1], route[3])
        groups.setdefault(lowered, []).append((suffix != own, rank, rel))
    superseded: dict[str, str] = {}
    for members in groups.values():
        kept = min(members)[2]
        superseded.update({rel: kept for *_rank, rel in members if rel != kept})
    return superseded


def declared_bundle_exec_types(
    bundle_info, mcp_deps: list, lsp_deps: list, *, create_config: bool
) -> tuple[str, ...]:
    """Executable types the bundle ships, before trust filters them."""
    from ..core.experimental import is_enabled
    from ..integration.canvas_integrator import is_canvas_bundle_path

    root = Path(bundle_info.source_dir) if bundle_info.source_dir is not None else None
    files = list(bundle_pack_files(bundle_info))
    if not files and root is not None:
        files = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
    declared = []
    if any(is_plugin_hook_path(path) for path in files) or (
        root is not None and any(markdown_declares_hooks(root / path) for path in files)
    ):
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
