"""Shared builders for native Copilot Agent Plugin registration tests."""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def write_agent_plugin(
    root: Path,
    *,
    name: str,
    version: str = "1.0.0",
    description: str = "Portable Agent Plugin fixture",
    with_mcp: bool = True,
    skill_body: str = "Use the plugin.",
) -> Path:
    """Materialize an exact Agent Plugins v1.0.0 package at *root*."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "$schema": PLUGIN_SCHEMA_ID,
        "name": name,
        "version": version,
        "description": description,
        "license": "MIT",
    }
    (root / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="ascii")
    skill_dir = root / "skills" / f"{name}-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: {description}\n---\n\n{skill_body}\n",
        encoding="ascii",
    )
    if with_mcp:
        (root / "mcp.json").write_text(
            json.dumps(
                {
                    "$schema": MCP_SCHEMA_ID,
                    "mcpServers": {
                        f"{name}-mcp": {
                            "type": "stdio",
                            "command": "printf",
                            "args": ["${PLUGIN_ROOT}/probe"],
                        }
                    },
                },
                indent=2,
            ),
            encoding="ascii",
        )
    return root


def write_legacy_package(
    root: Path,
    *,
    name: str,
    dependencies: list[str] | None = None,
) -> Path:
    """Materialize a legacy APM package that must keep decomposing."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "description": name}
    if dependencies:
        manifest["dependencies"] = {"apm": dependencies}
    (root / "apm.yml").write_text(json.dumps(manifest), encoding="ascii")
    skills = root / ".apm" / "skills" / f"{name}-skill"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: legacy\n---\n\nLegacy skill.\n",
        encoding="ascii",
    )
    return root


def read_json(path: Path) -> dict:
    """Read one JSON document from *path*."""
    return json.loads(path.read_text(encoding="utf-8"))
