"""Parser for Claude plugins (plugin.json format).

Aligns with the Claude Code plugin spec:
  https://docs.anthropic.com/en/docs/claude-code/plugins

Key spec rules:
- The manifest (.claude-plugin/plugin.json) is **optional**.
- When present, only `name` is required; everything else is optional metadata.
- When absent, the plugin name is derived from the directory name.
- Standard component directories: agents/, commands/, skills/, hooks/
- Pass-through files: .mcp.json, .lsp.json, settings.json
"""

import json
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
import yaml


def parse_plugin_manifest(plugin_json_path: Path) -> Dict[str, Any]:
    """Parse a plugin.json manifest file.

    Args:
        plugin_json_path: Path to the plugin.json file

    Returns:
        dict: Parsed plugin manifest

    Raises:
        FileNotFoundError: If plugin.json does not exist
        ValueError: If plugin.json is invalid JSON
    """
    if not plugin_json_path.exists():
        raise FileNotFoundError(f"plugin.json not found: {plugin_json_path}")

    try:
        with open(plugin_json_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in plugin.json: {e}")

    return manifest


def normalize_plugin_directory(plugin_path: Path, plugin_json_path: Optional[Path] = None) -> Path:
    """Normalize a Claude plugin directory into an APM package.

    Works with or without plugin.json.  When plugin.json is present it is
    treated as optional metadata; when absent the plugin name is derived from
    the directory name.

    Auto-discovers the standard component directories defined by the spec:
    agents/, commands/, skills/, hooks/, and pass-through files
    (.mcp.json, .lsp.json, settings.json).

    Args:
        plugin_path: Root of the plugin directory.
        plugin_json_path: Optional path to plugin.json (may be None).

    Returns:
        Path: Path to the generated apm.yml.
    """
    manifest: Dict[str, Any] = {}

    if plugin_json_path is not None and plugin_json_path.exists():
        try:
            manifest = parse_plugin_manifest(plugin_json_path)
        except (ValueError, FileNotFoundError):
            pass  # Treat as empty manifest; fall back to dir-name defaults

    # Derive name from directory if not in manifest
    if 'name' not in manifest or not manifest['name']:
        manifest['name'] = plugin_path.name

    return synthesize_apm_yml_from_plugin(plugin_path, manifest)


def synthesize_apm_yml_from_plugin(plugin_path: Path, manifest: Dict[str, Any]) -> Path:
    """Synthesize apm.yml from plugin metadata.

    Maps the plugin's agents/, skills/, commands/, hooks/ directories and
    pass-through files (.mcp.json, .lsp.json, settings.json) into .apm/,
    then generates apm.yml.

    Args:
        plugin_path: Path to the plugin directory.
        manifest: Plugin metadata dict (only `name` is required; all other
                  fields are optional and default gracefully).

    Returns:
        Path: Path to the generated apm.yml.
    """
    if not manifest.get('name'):
        manifest['name'] = plugin_path.name

    # Create .apm directory structure
    apm_dir = plugin_path / ".apm"
    apm_dir.mkdir(exist_ok=True)

    # Map plugin structure into .apm/ subdirectories
    _map_plugin_artifacts(plugin_path, apm_dir)

    # Generate apm.yml from plugin metadata
    apm_yml_content = _generate_apm_yml(manifest)
    apm_yml_path = plugin_path / "apm.yml"

    with open(apm_yml_path, 'w', encoding='utf-8') as f:
        f.write(apm_yml_content)

    return apm_yml_path


def _map_plugin_artifacts(plugin_path: Path, apm_dir: Path) -> None:
    """Map plugin artifacts to .apm/ subdirectories and copy pass-through files.

    Copies:
    - agents/     → .apm/agents/
    - skills/     → .apm/skills/
    - commands/   → .apm/prompts/  (*.md normalized to *.prompt.md)
    - hooks/      → .apm/hooks/
    - .mcp.json   → .apm/.mcp.json  (MCP-based plugins need this to function)
    - .lsp.json   → .apm/.lsp.json
    - settings.json → .apm/settings.json

    Args:
        plugin_path: Root of the plugin directory.
        apm_dir: Path to the .apm/ directory.
    """
    # Map agents/
    source_agents = plugin_path / "agents"
    if source_agents.exists() and source_agents.is_dir():
        target_agents = apm_dir / "agents"
        if target_agents.exists():
            shutil.rmtree(target_agents)
        shutil.copytree(source_agents, target_agents)

    # Map skills/
    source_skills = plugin_path / "skills"
    if source_skills.exists() and source_skills.is_dir():
        target_skills = apm_dir / "skills"
        if target_skills.exists():
            shutil.rmtree(target_skills)
        shutil.copytree(source_skills, target_skills)

    # Map commands/ → .apm/prompts/ (normalize .md → .prompt.md)
    source_commands = plugin_path / "commands"
    if source_commands.exists() and source_commands.is_dir():
        target_prompts = apm_dir / "prompts"
        if target_prompts.exists():
            shutil.rmtree(target_prompts)
        target_prompts.mkdir(parents=True, exist_ok=True)

        for source_file in source_commands.rglob("*"):
            if not source_file.is_file():
                continue
            relative_path = source_file.relative_to(source_commands)
            target_path = target_prompts / relative_path
            if source_file.name.endswith(".prompt.md"):
                pass
            elif source_file.suffix == ".md":
                target_path = target_path.with_name(f"{source_file.stem}.prompt.md")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_path)

    # Map hooks/
    source_hooks = plugin_path / "hooks"
    if source_hooks.exists() and source_hooks.is_dir():
        target_hooks = apm_dir / "hooks"
        if target_hooks.exists():
            shutil.rmtree(target_hooks)
        shutil.copytree(source_hooks, target_hooks)

    # Pass-through files required for MCP/LSP plugins to function
    for passthrough in (".mcp.json", ".lsp.json", "settings.json"):
        source_file = plugin_path / passthrough
        if source_file.exists():
            shutil.copy2(source_file, apm_dir / passthrough)


def _generate_apm_yml(manifest: Dict[str, Any]) -> str:
    """Generate apm.yml content from plugin metadata.

    Args:
        manifest: Plugin metadata dict.

    Returns:
        str: YAML content for apm.yml.
    """
    apm_package: Dict[str, Any] = {
        'name': manifest.get('name'),
        'version': manifest.get('version', '0.0.0'),
        'description': manifest.get('description', ''),
    }

    # author: spec defines it as {name, email, url} object; accept string too
    if 'author' in manifest:
        author = manifest['author']
        if isinstance(author, dict):
            apm_package['author'] = author.get('name', '')
        else:
            apm_package['author'] = str(author)

    for field in ('license', 'repository', 'homepage', 'tags'):
        if field in manifest:
            apm_package[field] = manifest[field]

    if manifest.get('dependencies'):
        apm_package['dependencies'] = {'apm': manifest['dependencies']}

    apm_package['type'] = 'hybrid'

    return yaml.dump(apm_package, default_flow_style=False, sort_keys=False)


def validate_plugin_package(plugin_path: Path) -> bool:
    """Check whether a directory looks like a Claude plugin.

    A directory is a valid plugin if it has plugin.json (with at least a name),
    or if it contains at least one standard component directory.

    Args:
        plugin_path: Path to the plugin directory.

    Returns:
        bool: True if the directory appears to be a Claude plugin.
    """
    # Check for plugin.json (optional; only name is required when present)
    from ..utils.helpers import find_plugin_json
    plugin_json = find_plugin_json(plugin_path)
    if plugin_json is not None:
        try:
            with open(plugin_json, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            return bool(manifest.get('name'))
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: presence of any standard component directory
    for component_dir in ("agents", "commands", "skills", "hooks"):
        if (plugin_path / component_dir).is_dir():
            return True

    return False
