"""A hermetic Codex CLI stand-in for installed-CLI lifecycle tests.

Answers ``codex --version`` and the public ``app-server --stdio`` JSON-RPC
methods APM's native registrar calls (``initialize``, ``config/read``,
``config/batchWrite``, ``marketplace/upgrade``, ``plugin/read``,
``plugin/uninstall``, ``marketplace/remove``). It keeps the user layer in
``$HOME/.codex/config.toml`` with a compare-and-swap version and installs
marketplace and plugin copies with local ``git`` clones, so APM verifies a
real installed copy. No network, no agent and no MCP process.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT = r"""
import json, os, shutil, subprocess, sys
from pathlib import Path
from urllib.parse import urlparse

import tomlkit

HOME = Path(os.environ["HOME"])
ROOT = HOME / ".codex"
CONFIG = ROOT / "config.toml"
STATE = ROOT / ".stand-in-version"
INSTALLS = ROOT / ".stand-in"


def _version():
    return STATE.read_text() if STATE.exists() else "0"


def _config():
    return json.loads(json.dumps(tomlkit.parse(CONFIG.read_text()))) if CONFIG.exists() else {}


def _save(config):
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(tomlkit.dumps(config))
    STATE.write_text(str(int(_version()) + 1))


def _clone(url, ref, dest):
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_ALLOW_PROTOCOL="file")
    subprocess.run(["git", "clone", "-q", url, str(dest)], check=True, env=env)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True, env=env)
    return dest


def _path(uri):
    return Path(urlparse(uri).path)


def handle(method, params):
    config = _config()
    if method == "initialize":
        return {}
    if method == "config/read":
        name = {"type": "user", "profile": None, "file": str(CONFIG)}
        return {"layers": [{"name": name, "version": _version(), "config": config}]}
    if method == "config/batchWrite":
        if params["expectedVersion"] != _version():
            raise ValueError("version conflict")
        for edit in params["edits"]:
            table, _, key = edit["keyPath"].partition(".")
            config.setdefault(table, {})[json.loads(key)] = edit["value"]
        _save(config)
        return {"status": "ok"}
    if method == "marketplace/upgrade":
        name = params["marketplaceName"]
        source = config["marketplaces"][name]
        dest = _clone(source["source"], source["ref"], INSTALLS / "marketplaces" / name)
        return {"selectedMarketplaces": [name], "upgradedRoots": [str(dest)], "errors": []}
    if method == "plugin/read":
        manifest = Path(params["marketplacePath"])
        catalog = json.loads(manifest.read_text())
        entry = next(p for p in catalog["plugins"] if p["name"] == params["pluginName"])
        src = entry["source"]
        clone = _clone(src["url"], src["sha"], INSTALLS / "plugins" / entry["name"])
        root = clone / src["path"]
        plugin = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
        mcp = plugin.get("mcpServers")
        servers = sorted(json.loads((root / mcp).read_text())["mcpServers"]) if mcp else []
        plugin_id = f'{entry["name"]}@{catalog["name"]}'
        summary = {
            "id": plugin_id,
            "installed": plugin_id in config.get("plugins", {}),
            "source": {"type": "git", "url": src["url"], "sha": src["sha"]},
        }
        skills = [{"path": str(p)} for p in sorted(root.rglob("SKILL.md"))]
        return {"plugin": {"summary": summary, "skills": skills, "mcpServers": servers}}
    if method == "plugin/uninstall":
        config.get("plugins", {}).pop(params["pluginId"], None)
        _save(config)
        return {}
    if method == "marketplace/remove":
        config.get("marketplaces", {}).pop(params["marketplaceName"], None)
        _save(config)
        return {}
    raise ValueError(method)


if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.164.0")
    sys.exit(0)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    try:
        reply = {"id": message["id"], "result": handle(message["method"], message["params"])}
    except Exception as exc:  # the stand-in answers every request
        reply = {"id": message["id"], "error": {"message": str(exc)}}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


def write_codex_stand_in(bin_dir: Path) -> Path:
    """Write an executable ``codex`` into *bin_dir* and return its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    codex = bin_dir / "codex"
    codex.write_text(f"#!{sys.executable}\n{_SCRIPT}", encoding="ascii")
    codex.chmod(0o755)
    return codex
