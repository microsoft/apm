"""Plugin artifact mapping helpers – copies component dirs into .apm/.

Private module – imported only via :mod:`apm_cli.deps.plugin_parser`.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ...utils.path_security import PathTraversalError, ensure_path_within

_logger = logging.getLogger(__name__)


def _is_within_plugin(candidate: Path, plugin_root: Path, *, component: str) -> bool:
    """Return True iff *candidate* resolves inside *plugin_root*.

    Logs a warning and returns False when the path escapes the plugin
    root (absolute path, ``..`` traversal, or symlink pointing outside).
    Used to enforce the trust boundary on attacker-controlled manifest
    fields (agents/skills/commands/hooks) during plugin normalization.

    The rejected path string and resolved exception are deliberately
    omitted from log output: manifest values are externally controlled
    and static-analysis tooling treats them as tainted/sensitive. The
    component name alone is sufficient to identify which manifest field
    was rejected; operators that need the full value can reproduce
    locally with a clean checkout.
    """
    try:
        ensure_path_within(candidate, plugin_root)
    except PathTraversalError:
        _logger.warning(
            "Skipping %s entry: path escapes plugin root",
            component,
        )
        return False
    return True


def _copy_agent_artifacts(
    apm_dir: Path,
    agent_sources: list,
    ignore_non_content: "object",
) -> None:
    """Copy agent source files/directories into ``.apm/agents/``.

    Extracted from :func:`_map_plugin_artifacts` to reduce its branch and
    statement count within the configured Ruff thresholds.
    """
    target_agents = apm_dir / "agents"
    if target_agents.exists():
        shutil.rmtree(target_agents)
    agent_dirs = [s for s in agent_sources if s.is_dir()]
    agent_files = [s for s in agent_sources if s.is_file()]
    if agent_dirs:
        shutil.copytree(agent_dirs[0], target_agents, ignore=ignore_non_content)
        for extra in agent_dirs[1:]:
            shutil.copytree(extra, target_agents, dirs_exist_ok=True, ignore=ignore_non_content)
    if agent_files:
        target_agents.mkdir(parents=True, exist_ok=True)
        for f in agent_files:
            shutil.copy2(f, target_agents / f.name)


def _copy_skill_artifacts(
    apm_dir: Path,
    skill_sources: list,
    is_custom_list: bool,
    ignore_non_content: "object",
) -> None:
    """Copy skill source files/directories into ``.apm/skills/``.

    Extracted from :func:`_map_plugin_artifacts` to reduce its branch and
    statement count within the configured Ruff thresholds.
    """
    target_skills = apm_dir / "skills"
    if target_skills.exists():
        shutil.rmtree(target_skills)
    skill_dirs = [s for s in skill_sources if s.is_dir()]
    skill_files = [s for s in skill_sources if s.is_file()]
    if is_custom_list and skill_dirs:
        target_skills.mkdir(parents=True, exist_ok=True)
        for d in skill_dirs:
            shutil.copytree(
                d,
                target_skills / d.name,
                ignore=ignore_non_content,
                dirs_exist_ok=True,
            )
    elif skill_dirs:
        shutil.copytree(skill_dirs[0], target_skills, ignore=ignore_non_content)
        for extra in skill_dirs[1:]:
            shutil.copytree(extra, target_skills, dirs_exist_ok=True, ignore=ignore_non_content)
    if skill_files:
        target_skills.mkdir(parents=True, exist_ok=True)
        for f in skill_files:
            shutil.copy2(f, target_skills / f.name)


def _copy_command_file(source_file: Path, dest_dir: Path, rel_to: Path | None = None) -> None:
    """Copy a command file, normalizing ``.md`` -> ``.prompt.md``."""
    if rel_to:
        relative_path = source_file.relative_to(rel_to)
        target_path = dest_dir / relative_path
    else:
        target_path = dest_dir / source_file.name
    if not source_file.name.endswith(".prompt.md") and source_file.suffix == ".md":
        target_path = target_path.with_name(f"{source_file.stem}.prompt.md")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, target_path)


def _map_commands(command_sources: list, apm_dir: Path) -> None:
    """Copy command sources into ``.apm/prompts/``, normalizing ``.md`` -> ``.prompt.md``.

    Extracted from :func:`_map_plugin_artifacts` to reduce its branch and
    statement count within the configured Ruff thresholds.
    """
    target_prompts = apm_dir / "prompts"
    if target_prompts.exists():
        shutil.rmtree(target_prompts)
    target_prompts.mkdir(parents=True, exist_ok=True)
    for source in command_sources:
        if source.is_file() and not source.is_symlink():
            _copy_command_file(source, target_prompts)
        elif source.is_dir():
            for source_file in source.rglob("*"):
                if not source_file.is_file() or source_file.is_symlink():
                    continue
                _copy_command_file(source_file, target_prompts, rel_to=source)


def _map_hooks(
    hooks_value: Any,
    plugin_path: Path,
    apm_dir: Path,
    resolve_sources_fn: Any,
    ignore_non_content: Any,
) -> None:
    """Map hooks from manifest to ``.apm/hooks/``.

    Handles the three forms allowed by the spec: inline dict, config-file
    path string, and directory path(s).  Extracted from
    :func:`_map_plugin_artifacts` to reduce its branch and statement count
    within the configured Ruff thresholds.
    """
    if isinstance(hooks_value, dict):
        # Inline hooks object -> write as .apm/hooks/hooks.json
        target_hooks = apm_dir / "hooks"
        target_hooks.mkdir(parents=True, exist_ok=True)
        (target_hooks / "hooks.json").write_text(json.dumps(hooks_value, indent=2))
    elif isinstance(hooks_value, str) and (plugin_path / hooks_value).is_file():
        # Config file path (e.g. "hooks": "hooks.json")
        src_file = plugin_path / hooks_value
        if not src_file.is_symlink() and _is_within_plugin(
            src_file, plugin_path, component="hooks"
        ):
            target_hooks = apm_dir / "hooks"
            target_hooks.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target_hooks / "hooks.json")
    else:
        # Directory path(s)  -- standard flow
        hook_sources = resolve_sources_fn("hooks", "hooks")
        if hook_sources:
            target_hooks = apm_dir / "hooks"
            if target_hooks.exists():
                shutil.rmtree(target_hooks)
            shutil.copytree(hook_sources[0], target_hooks, ignore=ignore_non_content)
            for extra in hook_sources[1:]:
                shutil.copytree(extra, target_hooks, dirs_exist_ok=True, ignore=ignore_non_content)


def _map_plugin_artifacts(
    plugin_path: Path, apm_dir: Path, manifest: dict[str, Any] | None = None
) -> None:
    """Map plugin artifacts to .apm/ subdirectories and copy pass-through files.

    Copies:
    - agents/     -> .apm/agents/
    - skills/     -> .apm/skills/
    - commands/   -> .apm/prompts/  (*.md normalized to *.prompt.md)
    - hooks/      -> .apm/hooks/    (directory, config file, or inline object)
    - .mcp.json   -> .apm/.mcp.json  (MCP-based plugins need this to function)
    - .lsp.json   -> .apm/.lsp.json
    - settings.json -> .apm/settings.json

    When the manifest specifies custom component paths (e.g. ``"agents": ["custom/"]``),
    those paths are used instead of the defaults.

    Symlinks are skipped entirely to prevent content exfiltration attacks.

    Args:
        plugin_path: Root of the plugin directory.
        apm_dir: Path to the .apm/ directory.
        manifest: Optional plugin.json metadata; used for custom component paths.
    """
    if manifest is None:
        manifest = {}

    from apm_cli.security.gate import ignore_non_content

    # Resolve source paths  -- use manifest arrays if present, else defaults.
    # Custom paths may be directories OR individual files.
    #
    # Security: every manifest-controlled path is verified to resolve
    # inside *plugin_path* before it is copied.  Without this guard, a
    # malicious plugin could set ``"commands": "/etc/passwd"`` or
    # ``"agents": ["../../host"]`` and trick ``apm install`` into copying
    # arbitrary host files into the project's ``.apm/`` tree (and from
    # there into ``.github/prompts/`` via auto-integration).
    def _resolve_sources(component: str, default_dir: str):
        """Return list of existing source paths (dirs or files) for a component."""
        custom = manifest.get(component)
        if isinstance(custom, list):
            paths = []
            for p in custom:
                raw = str(p)
                src = plugin_path / raw
                if (
                    src.exists()
                    and not src.is_symlink()
                    and _is_within_plugin(src, plugin_path, component=component)
                ):
                    paths.append(src)
            return paths
        elif isinstance(custom, str):
            src = plugin_path / custom
            if (
                src.exists()
                and not src.is_symlink()
                and _is_within_plugin(src, plugin_path, component=component)
            ):
                return [src]
            return []
        default = plugin_path / default_dir
        if (
            default.exists()
            and not default.is_symlink()
            and default.is_dir()
            and _is_within_plugin(default, plugin_path, component=component)
        ):
            return [default]
        return []

    # Map agents/
    # Unlike skills (which are named directories containing SKILL.md), agents
    # are flat files  -- each .md is one agent.  So we always merge directory
    # contents directly into .apm/agents/ (no nesting by dir name).
    agent_sources = _resolve_sources("agents", "agents")
    if agent_sources:
        _copy_agent_artifacts(apm_dir, agent_sources, ignore_non_content)

    # Map skills/
    skill_sources = _resolve_sources("skills", "skills")
    if skill_sources:
        is_custom_list = isinstance(manifest.get("skills"), list)
        _copy_skill_artifacts(apm_dir, skill_sources, is_custom_list, ignore_non_content)

    # Map commands/ -> .apm/prompts/ (normalize .md -> .prompt.md)
    command_sources = _resolve_sources("commands", "commands")
    if command_sources:
        _map_commands(command_sources, apm_dir)

    # Map hooks/  -- the spec allows a directory path, a config file path,
    # or an inline object.  Handle all three forms.
    hooks_value = manifest.get("hooks")
    _map_hooks(hooks_value, plugin_path, apm_dir, _resolve_sources, ignore_non_content)

    # Pass-through files required for MCP/LSP plugins to function
    for passthrough in (".mcp.json", ".lsp.json", "settings.json"):
        source_file = plugin_path / passthrough
        if source_file.exists() and not source_file.is_symlink():
            shutil.copy2(source_file, apm_dir / passthrough)
