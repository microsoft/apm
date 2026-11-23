"""Plugin-native source-layout conventions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

# A target without a rule transform (Copilot, Grok Build) copies an APM
# instruction verbatim under its source name (InstructionIntegrator), so its
# files carry the APM suffix whatever extension its mapping declares.
_APM_INSTRUCTION_SUFFIX = ".instructions.md"


def deployed_file_extension(primitive_kind: str, mapping: Any) -> str | None:
    """Return the extension a target's deployed *primitive_kind* files carry."""
    if primitive_kind == "instructions" and not mapping.output_compare:
        return _APM_INSTRUCTION_SUFFIX
    return mapping.extension


# Formats a Claude plugin loads as-is: preferred over another target's
# rendering with the same extension (a Kiro prompt drops its frontmatter).
PLUGIN_NATIVE_FORMATS = frozenset({"claude_agent", "claude_command"})

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
    return None if found is None else _extension_rank(found[0], found[1])


def _extension_rank(head: str, extension: str) -> int:
    preference = PLUGIN_RENDERING_PREFERENCE[head]
    return preference.index(extension) if extension in preference else len(preference)


def deployed_rendering_suffix(deployed_rel: str) -> str | None:
    """Return the deploying target's own extension for *deployed_rel*, if any."""
    found = _deployed_rendering(deployed_rel)
    return found[1] if found else None


def _deployed_rendering(deployed_rel: str) -> tuple[str, str, str, str] | None:
    """Return ``(plugin directory, target extension, format id, stem)`` of a rendering.

    The stem is the path under the target's primitive directory without the
    target's own extension, the identity every rendering of a primitive shares.
    """
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
            extension = deployed_file_extension(kind, mapping)
            if kind not in heads or not mapping.subdir or not extension:
                continue
            stem = deployed_rel[len(prefix) : -len(extension)]
            if deployed_rel.startswith(prefix) and deployed_rel.endswith(extension) and stem:
                return heads[kind], extension, mapping.format_id, stem
    return None


def superseded_plugin_renderings(deployed_rels: list[str | None]) -> dict[int, int]:
    """Return ``{dropped index: kept index}`` over one package's deployed files (#3686).

    Renderings of one primitive share a plugin directory and the name left once
    each deploying target's own extension is removed: ``.claude/agents/rv.md``
    and ``.github/agents/rv.agent.md`` are both ``agents/rv``, while a Claude
    ``rv.agent.md`` is another primitive, ``agents/rv.agent``. Each primitive
    keeps its preferred rendering (:data:`PLUGIN_RENDERING_PREFERENCE`, then a
    Claude plugin format over another target's same-extension rendering).
    Instruction renderings drop only beside the APM rendering, since native rule
    formats never convert. Different primitives that share a plugin path are
    left for the caller to report as a collision.
    """
    groups: dict[tuple[str, str], list[tuple[tuple[int, int], int, str]]] = {}
    for index, rel in enumerate(deployed_rels):
        found = _deployed_rendering(rel) if rel else None
        if found is not None:
            head, extension, format_id, stem = found
            key = (_extension_rank(head, extension), format_id not in PLUGIN_NATIVE_FORMATS)
            groups.setdefault((head, stem), []).append((key, index, extension))
    superseded: dict[int, int] = {}
    for (head, _stem), members in groups.items():
        _key, kept, extension = min(members)
        if _DROP_ONLY_BESIDE.get(head, extension) == extension:
            superseded.update({index: kept for _key, index, _ext in members if index != kept})
    return superseded


PLUGIN_LAYOUT: dict[str, PluginDirSpec] = {
    "agents": PluginDirSpec("agents", ("agents",), _identity),
    "skills": PluginDirSpec("skills", ("skills",), _identity),
    "commands": PluginDirSpec("commands", ("commands", "prompts"), plugin_command_prompt_name),
    "instructions": PluginDirSpec("instructions", ("instructions",), _identity),
    "extensions": PluginDirSpec("extensions", ("canvas",), _identity),
    "hooks": PluginDirSpec("hooks", ("hooks",), _identity),
}
