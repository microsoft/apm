"""Packages of the lifecycle world, shaped like real APM packages (#4094).

One ``LocalPackageFactory`` owns every package, so the diamond
``p-parent`` / ``p-parent2`` -> ``p-child`` uses portable relative paths.
Each package is authored once per template; ``builder.py`` also publishes the
leaf packages as git repositories served by ``services.py``.

Kinds follow ``tests/lifecycle/model/state.py``: ``skill`` and ``other-file``
are ungated; ``hook``, ``mcp``, ``lsp``, ``bin``, ``canvas`` and ``native``
need an approval before they deploy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tests.utils.local_package import LocalPackage, LocalPackageFactory

PACKAGE_NAMES = (
    "p-skill",
    "p-exec",
    "p-lsp",
    "p-child",
    "p-parent",
    "p-parent2",
    "p-native-claude",
    "p-native-codex",
    "p-canvas",
    "p-native-claude-bin",
    "p-native-claude-exec",
    "p-kit",
    "p-pack",
)
# The packages whose source is a git repository on the world's local HTTPS server
# (also listed in the world marketplace).
GIT_PUBLISHED = ("p-skill", "p-exec", "p-lsp", "p-native-claude", "p-native-claude-bin")
# Served from git and listed in the world marketplace too (so ``apm pack``, which
# refuses local-path dependencies, can bundle a canvas, and M-SOURCE can install a
# canvas from git and from the catalog); not a covering-row source.
GIT_PACK_ONLY = ("p-canvas",)
MCP_SERVER = "p-exec-mcp"
LSP_SERVER = "p-lsp-server"
# A command that must never run: a deployed MCP or LSP entry is configuration only.
INERT_COMMAND = "apm-lifecycle-inert-server"
_SESSION_HOOK = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo p-exec"}]}]}
}
_BIN = "#!/bin/sh\necho p-exec\n"
_CANVAS = "export default function render() {\n  return 'p-canvas';\n}\n"


@dataclass(frozen=True)
class AuthoredPackage:
    name: str
    root: Path
    kinds: frozenset[str]


def _skill(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: Lifecycle world skill {name}.\n---\n\n{body}\n"


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="ascii")


def _skill_package(factory: LocalPackageFactory, name: str) -> LocalPackage:
    package = factory.create(name, version="1.0.0")
    factory.add_skill(package, name, _skill(name, f"Use {name}."))
    return package


def _exec_package(factory: LocalPackageFactory) -> LocalPackage:
    package = factory.create(
        "p-exec",
        version="1.0.0",
        mcp_dependencies=[
            {
                "name": MCP_SERVER,
                "registry": False,
                "transport": "stdio",
                "command": INERT_COMMAND,
                "args": ["--stdio"],
            }
        ],
    )
    factory.add_hook(package, "session", _SESSION_HOOK)
    script = package.root / "bin" / "p-exec"
    script.parent.mkdir(parents=True)
    script.write_text(_BIN, encoding="ascii")
    script.chmod(0o755)
    return package


def _lsp_package(factory: LocalPackageFactory) -> LocalPackage:
    server = {
        "name": LSP_SERVER,
        "command": INERT_COMMAND,
        "args": ["--stdio"],
        "extensionToLanguage": {".fixture": "fixture"},
    }
    return factory.create("p-lsp", version="1.0.0", lsp_dependencies=[server])


def _kit_package(factory: LocalPackageFactory, name: str) -> LocalPackage:
    """A package carrying one of each deployable kind except bin and MCP: skill, session
    hook, canvas, instruction (other-file) and an LSP server ("<pkg>-server").

    p-kit is published to the package registry and p-pack is packed as a local bundle
    (distinct names: an installed bundle owns its LSP server name), so M-SOURCE can
    decide, audit, compile and lock each kind from those sources."""
    server = {
        "name": f"{name}-server",
        "command": INERT_COMMAND,
        "args": ["--stdio"],
        "extensionToLanguage": {".kit": "kit"},
    }
    package = factory.create(name, version="1.0.0", lsp_dependencies=[server])
    factory.add_skill(package, name, _skill(name, f"Use {name}."))
    hook = {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"echo {name}"}]}]}
    }
    factory.add_hook(package, "session", hook)
    factory.add_canvas(package, name, _CANVAS.replace("p-canvas", name))
    factory.add_instruction(
        package, name, f'---\napplyTo: "**"\n---\n\nFollow the {name} conventions.\n'
    )
    return package


def _native_claude(root: Path) -> None:
    """A Claude Code plugin: manifest, one skill and one stdio MCP server."""
    _write_json(
        root / ".claude-plugin" / "plugin.json",
        {
            "name": "p-native-claude",
            "version": "1.0.0",
            "description": "Lifecycle world native Claude plugin.",
        },
    )
    skill = root / "skills" / "p-native-claude" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill("p-native-claude", "Use the Claude plugin."), encoding="ascii")
    _write_json(
        root / ".mcp.json",
        {"mcpServers": {"p-native-claude-mcp": {"command": INERT_COMMAND, "args": ["--stdio"]}}},
    )


def _native_codex(root: Path) -> None:
    """A Codex plugin: manifest, one skill and one stdio MCP server."""
    _write_json(
        root / ".codex-plugin" / "plugin.json",
        {
            "name": "p-native-codex",
            "version": "1.0.0",
            "skills": "./skills",
            "mcpServers": "./.mcp.codex.json",
        },
    )
    skill = root / "skills" / "p-native-codex" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill("p-native-codex", "Use the Codex plugin."), encoding="ascii")
    _write_json(
        root / ".mcp.codex.json",
        {"mcpServers": {"p-native-codex-mcp": {"command": INERT_COMMAND, "args": ["--stdio"]}}},
    )


def _native_claude_bin(root: Path) -> None:
    """A Claude Code plugin that ships bin/: only a marketplace plugin deploys bin/
    (for Claude, at user scope; install/exec_gate.py plugin_bin_deployable)."""
    _write_json(
        root / ".claude-plugin" / "plugin.json",
        {
            "name": "p-native-claude-bin",
            "version": "1.0.0",
            "description": "Lifecycle world native Claude plugin with bin/.",
        },
    )
    skill = root / "skills" / "p-native-claude-bin" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill("p-native-claude-bin", "Use the bin plugin."), encoding="ascii")
    script = root / "bin" / "p-native-claude-bin"
    script.parent.mkdir()
    script.write_text(_BIN, encoding="ascii")
    script.chmod(0o755)


def _native_claude_exec(root: Path) -> None:
    """A Claude Code plugin with executables: one skill, a session hook (hooks/hooks.json)
    and a stdio LSP server (.lsp.json)."""
    name = "p-native-claude-exec"
    _write_json(
        root / ".claude-plugin" / "plugin.json",
        {"name": name, "version": "1.0.0", "description": "Lifecycle world native exec plugin."},
    )
    skill = root / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_skill(name, "Use the exec plugin."), encoding="ascii")
    hook = {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"echo {name}"}]}]}
    }
    _write_json(root / "hooks" / "hooks.json", hook)
    server = {"command": INERT_COMMAND, "args": ["--stdio"], "extensionToLanguage": {".fx": "fx"}}
    _write_json(root / ".lsp.json", {f"{name}-server": server})


def author_packages(root: Path) -> dict[str, AuthoredPackage]:
    """Author every world package under *root*; one factory owns them all."""
    factory = LocalPackageFactory(root)
    skill = _skill_package(factory, "p-skill")
    exec_ = _exec_package(factory)
    lsp = _lsp_package(factory)
    child = _skill_package(factory, "p-child")
    parents = [_skill_package(factory, name) for name in ("p-parent", "p-parent2")]
    for parent in parents:
        factory.add_relative_dependency(parent, child)
    canvas = factory.create("p-canvas", version="1.0.0")
    factory.add_canvas(canvas, "p-canvas", _CANVAS, assets={PurePosixPath("icon.txt"): b"c\n"})
    claude = root / "p-native-claude"
    _native_claude(claude)
    codex = root / "p-native-codex"
    _native_codex(codex)
    claude_bin = root / "p-native-claude-bin"
    _native_claude_bin(claude_bin)
    claude_exec = root / "p-native-claude-exec"
    _native_claude_exec(claude_exec)
    kit = _kit_package(factory, "p-kit")
    pack = _kit_package(factory, "p-pack")
    authored = {
        "p-skill": (skill.root, {"skill"}),
        "p-exec": (exec_.root, {"hook", "bin", "mcp"}),
        "p-lsp": (lsp.root, {"lsp"}),
        "p-child": (child.root, {"skill"}),
        "p-parent": (parents[0].root, {"skill"}),
        "p-parent2": (parents[1].root, {"skill"}),
        "p-native-claude": (claude, {"native"}),
        "p-native-codex": (codex, {"native"}),
        "p-canvas": (canvas.root, {"canvas"}),
        "p-native-claude-bin": (claude_bin, {"native", "bin", "skill"}),
        "p-native-claude-exec": (claude_exec, {"native", "skill", "hook", "lsp"}),
        "p-kit": (kit.root, {"skill", "hook", "canvas", "lsp", "other-file"}),
        "p-pack": (pack.root, {"skill", "hook", "canvas", "lsp", "other-file"}),
    }
    assert tuple(authored) == PACKAGE_NAMES
    return {
        name: AuthoredPackage(name, path, frozenset(kinds))
        for name, (path, kinds) in authored.items()
    }
