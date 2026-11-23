"""Plugin-native source-layout conventions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _identity(name: str) -> str:
    """Return *name* unchanged."""
    return name


@dataclass(frozen=True)
class PluginDirSpec:
    """How one plugin-native root lowers into APM primitive space."""

    plugin_dir: str
    primitive_kinds: tuple[str, ...]
    apm_basename_fn: Callable[[str], str]


PLUGIN_HOOKS_CONFIG = "hooks.json"

PLUGIN_ROOT_DIRS = (
    "agents",
    "skills",
    "commands",
    "instructions",
    "extensions",
    "hooks",
)


def find_plugin_root_sources(project_root: Path) -> list[str]:
    """Return plugin-native root sources that exist."""
    sources = [
        name
        for name in PLUGIN_ROOT_DIRS
        if (project_root / name).is_dir() and not (project_root / name).is_symlink()
    ]
    if (project_root / PLUGIN_HOOKS_CONFIG).is_file():
        sources.append(PLUGIN_HOOKS_CONFIG)
    return sources


def plugin_command_prompt_name(name: str) -> str:
    """Return the APM prompt filename represented by a plugin command."""
    if name.endswith(".prompt.md"):
        return name
    if name.endswith(".md"):
        return f"{name[: -len('.md')]}.prompt.md"
    return name


PRIMITIVE_FILE_SUFFIXES = (
    ".instructions.md",
    ".prompt.md",
    ".agent.md",
    ".mdc",
    ".toml",
    ".json",
    ".md",
)
"""Per-target rendering suffixes of primitive files, longest first."""


def primitive_file_suffix(name: str) -> str | None:
    """Return the rendering suffix of a primitive filename, if any."""
    return next((suffix for suffix in PRIMITIVE_FILE_SUFFIXES if name.endswith(suffix)), None)


# Plugin directories whose files are per-target renderings of one primitive,
# with the rendering a plugin bundle keeps, best first (#3686). Agents and
# commands keep the Claude plugin rendering, which Claude Code and Copilot CLI
# both load. Instructions keep the APM rendering (``applyTo``), which local
# bundle install converts into each target's rule format.
PLUGIN_RENDERING_PREFERENCE: dict[str, tuple[str, ...]] = {
    "agents": (".md", ".agent.md", ".toml"),
    "commands": (".md", ".prompt.md", ".toml"),
    "instructions": (".instructions.md", ".md", ".mdc"),
}

# Each rule format scopes paths its own way and none converts to another, so
# instruction renderings are dropped only next to the APM rendering.
_DROP_ONLY_BESIDE: dict[str, str] = {"instructions": ".instructions.md"}


# Pack renames these deployed suffixes in the plugin path (``review.prompt.md``
# becomes ``commands/review.md``, see plugin_exporter._rename_prompt).
_PLUGIN_EXPORT_SUFFIX: dict[str, str] = {".prompt.md": ".md"}


def deployed_rendering_extension(deployed_rel: str) -> str | None:
    """Return the extension a deployed agent, command or instruction carries in a bundle.

    *deployed_rel* is a project-relative deployed path such as
    ``.github/agents/rv.agent.md``; ``None`` when no target deploys it. The
    extension is the one of the exported plugin filename, so a Copilot prompt
    (``.github/prompts/review.prompt.md`` -> ``commands/review.md``) reports
    ``.md``.
    """
    suffix = deployed_rendering_suffix(deployed_rel)
    return _PLUGIN_EXPORT_SUFFIX.get(suffix, suffix) if suffix else None


def deployed_rendering_rank(deployed_rel: str) -> int | None:
    """Rank a deployed rendering within its own plugin directory's preference (lower is kept)."""
    found = _deployed_rendering(deployed_rel)
    if found is None:
        return None
    preference = PLUGIN_RENDERING_PREFERENCE[found[0]]
    return preference.index(found[1]) if found[1] in preference else len(preference)


def kept_shared_path_rendering(deployed_rels: list[str]) -> int | None:
    """Return the index of the rendering kept among deployed files sharing one plugin path.

    ``None`` keeps them all as a collision: when one is no target rendering, or
    when the preferred one is not a rendering the others may be dropped beside
    (Claude ``.claude/rules/py.md`` and Kiro ``.kiro/steering/py.md`` are two
    native rule formats and neither converts to the other, #3686).
    """
    found = [_deployed_rendering(rel) for rel in deployed_rels]
    if any(rendering is None for rendering in found):
        return None
    ranks = [deployed_rendering_rank(rel) for rel in deployed_rels]
    kept = min(range(len(found)), key=lambda index: (ranks[index], index))
    head, extension = found[kept]
    return kept if _DROP_ONLY_BESIDE.get(head, extension) == extension else None


def deployed_rendering_suffix(deployed_rel: str) -> str | None:
    """Return the deploying target's own extension for *deployed_rel*, if any."""
    found = _deployed_rendering(deployed_rel)
    return found[1] if found else None


def _deployed_rendering(deployed_rel: str) -> tuple[str, str] | None:
    """Return ``(plugin directory, target extension)`` of a deployed rendering."""
    from ..integration.targets import KNOWN_TARGETS

    heads = {
        kind: head
        for head in PLUGIN_RENDERING_PREFERENCE
        for kind in PLUGIN_LAYOUT[head].primitive_kinds
    }
    for profile in KNOWN_TARGETS.values():
        for kind, mapping in profile.primitives.items():
            root = mapping.deploy_root or profile.root_dir
            prefix = f"{root.strip('/')}/{mapping.subdir}/"
            if kind not in heads or not mapping.subdir or not mapping.extension:
                continue
            if deployed_rel.startswith(prefix) and deployed_rel.endswith(mapping.extension):
                return heads[kind], mapping.extension
    return None


def duplicate_plugin_renderings(renderings: dict[str, str | None]) -> set[str]:
    """Return plugin paths that duplicate a kept rendering of one primitive.

    *renderings* maps each plugin path of one package to the extension of the
    target that deployed it (:func:`deployed_rendering_extension`). Two paths
    render one primitive only when their names match once each target's own
    extension is removed, so ``agents/rv.md`` (Claude) and
    ``agents/rv.agent.md`` (Copilot) pair up, while two Claude agents never do.
    """
    groups: dict[str, list[tuple[int, str]]] = {}
    for rel, extension in renderings.items():
        head, _, tail = rel.partition("/")
        preference = PLUGIN_RENDERING_PREFERENCE.get(head)
        if not preference or not extension or len(tail) <= len(extension):
            continue
        if not tail.endswith(extension):
            continue
        rank = preference.index(extension) if extension in preference else len(preference)
        groups.setdefault(f"{head}/{tail[: -len(extension)]}", []).append((rank, rel))
    dropped: set[str] = set()
    for primitive, members in groups.items():
        rank, kept = min(members)
        preference = PLUGIN_RENDERING_PREFERENCE[primitive.partition("/")[0]]
        kept_extension = preference[rank] if rank < len(preference) else None
        if _DROP_ONLY_BESIDE.get(primitive.partition("/")[0], kept_extension) != kept_extension:
            continue
        dropped.update(rel for _rank, rel in members if rel != kept)
    return dropped


PLUGIN_LAYOUT: dict[str, PluginDirSpec] = {
    "agents": PluginDirSpec("agents", ("agents",), _identity),
    "skills": PluginDirSpec("skills", ("skills",), _identity),
    "commands": PluginDirSpec("commands", ("commands", "prompts"), plugin_command_prompt_name),
    "instructions": PluginDirSpec("instructions", ("instructions",), _identity),
    "extensions": PluginDirSpec("extensions", ("canvas",), _identity),
    "hooks": PluginDirSpec("hooks", ("hooks",), _identity),
}
