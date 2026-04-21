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
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml

from ..utils.path_security import ensure_path_within, PathTraversalError


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
            "Skipping %s entry: path escapes plugin root", component,
        )
        return False
    return True


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

    if not manifest.get('name'):
        logging.getLogger("apm").warning(
            "plugin.json at %s is missing 'name' field; falling back to directory name",
            plugin_json_path,
        )

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
    _map_plugin_artifacts(plugin_path, apm_dir, manifest)

    # Extract MCP servers from plugin and convert to dependency format
    mcp_servers = _extract_mcp_servers(plugin_path, manifest)
    if mcp_servers:
        mcp_deps = _mcp_servers_to_apm_deps(mcp_servers, plugin_path)
        if mcp_deps:
            manifest['_mcp_deps'] = mcp_deps

    # Generate apm.yml from plugin metadata
    apm_yml_content = _generate_apm_yml(manifest)
    apm_yml_path = plugin_path / "apm.yml"

    with open(apm_yml_path, 'w', encoding='utf-8') as f:
        f.write(apm_yml_content)

    return apm_yml_path


def _ignore_symlinks(directory, contents):
    """Ignore function for shutil.copytree that skips symlinks."""
    return [name for name in contents if (Path(directory) / name).is_symlink()]


def _extract_mcp_servers(plugin_path: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Extract MCP server definitions from a plugin manifest.

    Resolves ``mcpServers`` by type (per Claude Code spec):
    - ``str``  -> read that file path relative to plugin root, parse JSON,
      extract ``mcpServers`` key.
    - ``list`` -> read each file path, merge (last-wins on name conflict).
    - ``dict`` -> use directly as inline server definitions.

    When ``mcpServers`` is absent and ``.mcp.json`` (or ``.github/.mcp.json``)
    exists at plugin root, read it as the default (matches Claude Code
    auto-discovery).

    Security: symlinks are skipped, JSON parse errors are logged as warnings.

    ``${CLAUDE_PLUGIN_ROOT}`` in string values is replaced with the absolute
    plugin path.

    Args:
        plugin_path: Root of the plugin directory.
        manifest: Parsed plugin.json dict.

    Returns:
        dict mapping server name -> server config.  Empty on failure.
    """
    logger = logging.getLogger("apm")
    mcp_value = manifest.get("mcpServers")

    if mcp_value is not None:
        # Manifest explicitly defines mcpServers
        if isinstance(mcp_value, dict):
            servers = dict(mcp_value)
        elif isinstance(mcp_value, str):
            servers = _read_mcp_file(plugin_path, mcp_value, logger)
        elif isinstance(mcp_value, list):
            servers = {}
            for entry in mcp_value:
                if isinstance(entry, str):
                    servers.update(_read_mcp_file(plugin_path, entry, logger))
                else:
                    logger.warning("Ignoring non-string entry in mcpServers array: %s", entry)
        else:
            logger.warning("Unsupported mcpServers type %s; ignoring", type(mcp_value).__name__)
            return {}
    else:
        # Fall back to auto-discovery: .mcp.json then .github/.mcp.json
        servers = {}
        for fallback in (".mcp.json", ".github/.mcp.json"):
            candidate = plugin_path / fallback
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                servers = _read_mcp_json(candidate, logger)
                if servers:
                    break

    # Substitute ${CLAUDE_PLUGIN_ROOT} in all string values
    if servers:
        abs_root = str(plugin_path.resolve())
        servers = _substitute_plugin_root(servers, abs_root, logger)

    return servers


def _read_mcp_file(plugin_path: Path, rel_path: str, logger: logging.Logger) -> Dict[str, Any]:
    """Read a JSON file relative to *plugin_path* and return its ``mcpServers`` dict."""
    target = (plugin_path / rel_path).resolve()
    # Security: must stay inside plugin_path and not be a symlink
    try:
        target.relative_to(plugin_path.resolve())
    except ValueError:
        logger.warning("MCP file path escapes plugin root: %s", rel_path)
        return {}
    candidate = plugin_path / rel_path
    if not candidate.exists() or not candidate.is_file():
        logger.warning("MCP file not found: %s", candidate)
        return {}
    if candidate.is_symlink():
        logger.warning("Skipping symlinked MCP file: %s", candidate)
        return {}
    return _read_mcp_json(candidate, logger)


def _read_mcp_json(path: Path, logger: logging.Logger) -> Dict[str, Any]:
    """Parse a JSON file and return the ``mcpServers`` mapping."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read MCP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def _substitute_plugin_root(
    servers: Dict[str, Any], abs_root: str, logger: logging.Logger
) -> Dict[str, Any]:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` in server config string values."""
    token = "${CLAUDE_PLUGIN_ROOT}"
    substituted = False

    def _walk(obj: Any) -> Any:
        nonlocal substituted
        if isinstance(obj, str) and token in obj:
            substituted = True
            return obj.replace(token, abs_root)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        return obj

    result = {name: _walk(cfg) for name, cfg in servers.items()}
    if substituted:
        logger.info("Substituted ${CLAUDE_PLUGIN_ROOT} with %s", abs_root)
    return result


def _mcp_servers_to_apm_deps(
    servers: Dict[str, Any], plugin_path: Path
) -> List[Dict[str, Any]]:
    """Convert raw MCP server configs to ``dependencies.mcp`` dicts.

    Transport inference:
    - ``command`` present -> stdio
    - ``url`` present -> http (or ``type`` if it's a valid transport)
    - Neither -> skipped with warning

    Every entry gets ``registry: false`` (self-defined, not registry lookups).

    All resulting entries are routed through ``MCPDependency.from_dict()``
    so plugin-synthesized servers must clear the same security validation
    chokepoint as CLI-authored or manually edited entries (name shape, URL
    scheme allowlist, header CRLF, command path-traversal). Entries that
    fail validation are skipped with a warning rather than crashing the
    plugin install -- a single malformed server should not block the
    whole plugin.

    Args:
        servers: Mapping of server name -> server config dict.
        plugin_path: Plugin root (used for log context only).

    Returns:
        List of dicts consumable by ``MCPDependency.from_dict()``.
    """
    from ..models.dependency.mcp import MCPDependency

    logger = logging.getLogger("apm")
    deps: List[Dict[str, Any]] = []

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("Skipping non-dict MCP server config '%s'", name)
            continue

        dep: Dict[str, Any] = {"name": name, "registry": False}

        if "command" in cfg:
            dep["transport"] = "stdio"
            dep["command"] = cfg["command"]
            if "args" in cfg:
                dep["args"] = cfg["args"]
        elif "url" in cfg:
            raw_type = cfg.get("type", "http")
            valid_transports = {"http", "sse", "streamable-http"}
            dep["transport"] = raw_type if raw_type in valid_transports else "http"
            dep["url"] = cfg["url"]
            if "headers" in cfg:
                dep["headers"] = cfg["headers"]
        else:
            logger.warning(
                "Skipping MCP server '%s' from plugin '%s': no 'command' or 'url'",
                name, plugin_path.name,
            )
            continue

        if "env" in cfg:
            dep["env"] = cfg["env"]
        if "tools" in cfg:
            dep["tools"] = cfg["tools"]

        # Route through the validation chokepoint. Plugins are an ingress
        # path: a malicious plugin could otherwise smuggle path traversal,
        # CRLF, or unsafe URL schemes that bypass MCPDependency.validate().
        try:
            MCPDependency.from_dict(dep)
        except (ValueError, Exception) as exc:
            logger.warning(
                "Skipping invalid MCP server '%s' from plugin '%s': %s",
                name, plugin_path.name, exc,
            )
            continue

        deps.append(dep)

    return deps


def _map_plugin_artifacts(plugin_path: Path, apm_dir: Path, manifest: Optional[Dict[str, Any]] = None) -> None:
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
        target_agents = apm_dir / "agents"
        if target_agents.exists():
            shutil.rmtree(target_agents)
        agent_dirs = [s for s in agent_sources if s.is_dir()]
        agent_files = [s for s in agent_sources if s.is_file()]
        if agent_dirs:
            shutil.copytree(agent_dirs[0], target_agents, ignore=_ignore_symlinks)
            for extra in agent_dirs[1:]:
                shutil.copytree(extra, target_agents, dirs_exist_ok=True, ignore=_ignore_symlinks)
        if agent_files:
            target_agents.mkdir(parents=True, exist_ok=True)
            for f in agent_files:
                shutil.copy2(f, target_agents / f.name)

    # Map skills/
    skill_sources = _resolve_sources("skills", "skills")
    if skill_sources:
        target_skills = apm_dir / "skills"
        if target_skills.exists():
            shutil.rmtree(target_skills)
        skill_dirs = [s for s in skill_sources if s.is_dir()]
        skill_files = [s for s in skill_sources if s.is_file()]
        is_custom_list = isinstance(manifest.get("skills"), list)
        if is_custom_list and skill_dirs:
            target_skills.mkdir(parents=True, exist_ok=True)
            for d in skill_dirs:
                shutil.copytree(
                    d, target_skills / d.name,
                    ignore=_ignore_symlinks, dirs_exist_ok=True,
                )
        elif skill_dirs:
            shutil.copytree(skill_dirs[0], target_skills, ignore=_ignore_symlinks)
            for extra in skill_dirs[1:]:
                shutil.copytree(extra, target_skills, dirs_exist_ok=True, ignore=_ignore_symlinks)
        if skill_files:
            target_skills.mkdir(parents=True, exist_ok=True)
            for f in skill_files:
                shutil.copy2(f, target_skills / f.name)

    # Map commands/ -> .apm/prompts/ (normalize .md -> .prompt.md)
    command_sources = _resolve_sources("commands", "commands")
    if command_sources:
        target_prompts = apm_dir / "prompts"
        if target_prompts.exists():
            shutil.rmtree(target_prompts)
        target_prompts.mkdir(parents=True, exist_ok=True)

        def _copy_command_file(source_file: Path, dest_dir: Path, rel_to: Path = None):
            """Copy a command file, normalizing .md -> .prompt.md."""
            if rel_to:
                relative_path = source_file.relative_to(rel_to)
                target_path = dest_dir / relative_path
            else:
                target_path = dest_dir / source_file.name
            if not source_file.name.endswith(".prompt.md") and source_file.suffix == ".md":
                target_path = target_path.with_name(f"{source_file.stem}.prompt.md")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_path)

        for source in command_sources:
            if source.is_file() and not source.is_symlink():
                _copy_command_file(source, target_prompts)
            elif source.is_dir():
                for source_file in source.rglob("*"):
                    if not source_file.is_file() or source_file.is_symlink():
                        continue
                    _copy_command_file(source_file, target_prompts, rel_to=source)

    # Map hooks/  -- the spec allows a directory path, a config file path,
    # or an inline object.  Handle all three forms.
    hooks_value = manifest.get("hooks")
    if isinstance(hooks_value, dict):
        # Inline hooks object -> write as .apm/hooks/hooks.json
        target_hooks = apm_dir / "hooks"
        target_hooks.mkdir(parents=True, exist_ok=True)
        (target_hooks / "hooks.json").write_text(
            json.dumps(hooks_value, indent=2)
        )
    elif isinstance(hooks_value, str) and (plugin_path / hooks_value).is_file():
        # Config file path (e.g. "hooks": "hooks.json")
        src_file = plugin_path / hooks_value
        if src_file.is_symlink() or not _is_within_plugin(
            src_file, plugin_path, component="hooks"
        ):
            pass
        else:
            target_hooks = apm_dir / "hooks"
            target_hooks.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target_hooks / "hooks.json")
    else:
        # Directory path(s)  -- standard flow
        hook_sources = _resolve_sources("hooks", "hooks")
        if hook_sources:
            target_hooks = apm_dir / "hooks"
            if target_hooks.exists():
                shutil.rmtree(target_hooks)
            shutil.copytree(hook_sources[0], target_hooks, ignore=_ignore_symlinks)
            for extra in hook_sources[1:]:
                shutil.copytree(extra, target_hooks, dirs_exist_ok=True, ignore=_ignore_symlinks)

    # Pass-through files required for MCP/LSP plugins to function
    for passthrough in (".mcp.json", ".lsp.json", "settings.json"):
        source_file = plugin_path / passthrough
        if source_file.exists() and not source_file.is_symlink():
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

    # Inject MCP deps extracted from plugin mcpServers / .mcp.json
    mcp_deps = manifest.get('_mcp_deps')
    if mcp_deps:
        apm_package.setdefault('dependencies', {})['mcp'] = mcp_deps

    # Install behavior is driven by file presence (SKILL.md, etc.), not this
    # field.  Default to hybrid so the standard pipeline handles all components.
    apm_package['type'] = 'hybrid'

    from ..utils.yaml_io import yaml_to_str
    return yaml_to_str(apm_package)


def synthesize_plugin_json_from_apm_yml(apm_yml_path: Path) -> dict:
    """Create a minimal ``plugin.json`` dict from ``apm.yml`` identity fields.

    Reads ``apm.yml`` and extracts ``name``, ``version``, ``description``,
    ``author``, and ``license``.  The ``author`` string is mapped to the plugin
    spec's ``{"name": author}`` object format.

    Args:
        apm_yml_path: Path to the ``apm.yml`` file.

    Returns:
        dict suitable for writing as ``plugin.json``.

    Raises:
        ValueError: If ``name`` is missing from ``apm.yml``.
        FileNotFoundError: If the file does not exist.
    """
    if not apm_yml_path.exists():
        raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")

    try:
        from ..utils.yaml_io import load_yaml
        data = load_yaml(apm_yml_path)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {apm_yml_path}: {exc}") from exc

    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError(
            "apm.yml must contain at least a 'name' field to synthesize plugin.json"
        )

    result: Dict[str, Any] = {"name": data["name"]}

    if data.get("version"):
        result["version"] = data["version"]
    if data.get("description"):
        result["description"] = data["description"]
    if data.get("author"):
        result["author"] = {"name": str(data["author"])}
    if data.get("license"):
        result["license"] = data["license"]

    return result


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
