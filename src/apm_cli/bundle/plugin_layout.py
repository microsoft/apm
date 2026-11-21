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


PLUGIN_LAYOUT: dict[str, PluginDirSpec] = {
    "agents": PluginDirSpec("agents", ("agents",), _identity),
    "skills": PluginDirSpec("skills", ("skills",), _identity),
    "commands": PluginDirSpec("commands", ("commands", "prompts"), plugin_command_prompt_name),
    "instructions": PluginDirSpec("instructions", ("instructions",), _identity),
    "extensions": PluginDirSpec("extensions", ("canvas",), _identity),
    "hooks": PluginDirSpec("hooks", ("hooks",), _identity),
}
