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
# with the rendering a plugin bundle keeps, best first (#3686). The Claude
# plugin rendering leads because Claude Code and Copilot CLI both load it.
PLUGIN_RENDERING_PREFERENCE: dict[str, tuple[str, ...]] = {
    "agents": (".md", ".agent.md", ".toml"),
    "commands": (".md", ".prompt.md", ".toml"),
    "instructions": (".md", ".instructions.md", ".mdc"),
}


def plugin_rendering_key(plugin_rel: str) -> tuple[str, str, int] | None:
    """Return ``(primitive, suffix, rank)`` for a path that renders a primitive.

    Two paths with the same primitive are renderings of one source primitive
    for different targets, e.g. ``agents/rv.md`` and ``agents/rv.agent.md``;
    the lower *rank* is the rendering a plugin bundle keeps.
    """
    head, _, tail = plugin_rel.partition("/")
    preference = PLUGIN_RENDERING_PREFERENCE.get(head)
    suffix = primitive_file_suffix(tail) if preference and tail else None
    if suffix is None or suffix not in preference:
        return None
    return f"{head}/{tail[: -len(suffix)]}", suffix, preference.index(suffix)


def duplicate_plugin_renderings(plugin_rels: list[str]) -> set[str]:
    """Return the paths that duplicate a better rendering of the same primitive."""
    best: dict[str, tuple[int, str]] = {}
    for rel in plugin_rels:
        if (key := plugin_rendering_key(rel)) is not None:
            best[key[0]] = min(best.get(key[0], (key[2], rel)), (key[2], rel))
    return {
        rel
        for rel in plugin_rels
        if (key := plugin_rendering_key(rel)) is not None and best[key[0]][1] != rel
    }


PLUGIN_LAYOUT: dict[str, PluginDirSpec] = {
    "agents": PluginDirSpec("agents", ("agents",), _identity),
    "skills": PluginDirSpec("skills", ("skills",), _identity),
    "commands": PluginDirSpec("commands", ("commands", "prompts"), plugin_command_prompt_name),
    "instructions": PluginDirSpec("instructions", ("instructions",), _identity),
    "extensions": PluginDirSpec("extensions", ("canvas",), _identity),
    "hooks": PluginDirSpec("hooks", ("hooks",), _identity),
}
