"""External metadata protocol substitute, never a native hook/runtime witness.

Extracted from the command lifecycle fixture authored by Daniel Meppiel.
"""

NATIVE_CLIENT_STUB = r"""
import json
import os
from pathlib import Path
from apm_cli.claude_plugins.client import ClaudePluginCLI


def _native_run(self, *args):
    state_path = Path(os.environ["APM_NATIVE_TEST_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "marketplaces": [], "plugins": [], "calls": []
    }
    args = list(args)
    state["calls"].append(args)
    output = ""
    if args == ["--version"]:
        output = "2.1.291 (Claude Code)"
    elif args == ["plugin", "marketplace", "list", "--json"]:
        output = json.dumps(state["marketplaces"])
    elif args == ["plugin", "list", "--json"]:
        output = json.dumps(state["plugins"])
    elif args[:3] == ["plugin", "marketplace", "add"]:
        assert args[4:] == ["--scope", "user"], "Marketplace must stay user-scoped"
        root = Path(args[3])
        catalog = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
        state["marketplaces"] = [{
            "name": catalog["name"], "source": "directory",
            "path": str(root), "installLocation": str(root)
        }]
    elif args[:2] in (["plugin", "install"], ["plugin", "update"]):
        assert args[3:] == ["--scope", "user"], "Plugin registration must stay user-scoped"
        name, marketplace = args[2].split("@")
        selected = next(m for m in state["marketplaces"] if m["name"] == marketplace)
        root = Path(selected["path"])
        catalog = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(p for p in catalog["plugins"] if p["name"] == name)
        source = (root / entry["source"]).resolve()
        manifest = json.loads((source / ".claude-plugin" / "plugin.json").read_text())
        existing = next((p for p in state["plugins"] if p["id"] == args[2]), None)
        enabled = existing["enabled"] if existing else entry.get("defaultEnabled", True)
        state["plugins"] = [p for p in state["plugins"] if p["id"] != args[2]]
        state["plugins"].append({
            "id": args[2], "scope": "user", "enabled": enabled,
            "readFromFolder": str(source), "installPath": str(root / "not-the-live-root"),
            "version": manifest["version"]
        })
    elif args[:2] == ["plugin", "uninstall"]:
        assert args[3:] == ["--scope", "user", "--keep-data"], (
            "Native removal must stay user-scoped and retain user plugin data"
        )
        state["plugins"] = [p for p in state["plugins"] if p["id"] != args[2]]
    else:
        raise AssertionError(f"Unexpected native CLI call: {args!r}")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return output


ClaudePluginCLI.run = _native_run
"""
