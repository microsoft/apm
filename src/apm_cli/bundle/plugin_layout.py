"""Plugin-native source-layout conventions."""

from pathlib import Path

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
    sources = [name for name in PLUGIN_ROOT_DIRS if (project_root / name).is_dir()]
    if (project_root / "hooks.json").is_file():
        sources.append("hooks.json")
    return sources
