"""Shared hermetic MCP project chain driven by the installed ``apm`` binary (#3649)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

SECRET = "mcp-chain-secret-sentinel"
JSON_CONFIGS = {
    "claude": (".mcp.json", "mcpServers"),
    "vscode": (".vscode/mcp.json", "servers"),
    "cursor": (".cursor/mcp.json", "mcpServers"),
    "copilot": (".github/mcp.json", "mcpServers"),
    "opencode": ("opencode.json", "mcp"),
}


class Chain:
    """One hermetic project driven by the real ``apm`` binary."""

    def __init__(self, root: Path, binary: Path, *, dirs: tuple[str, ...] = ()) -> None:
        self.isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        self.project = self.isolated.work_root / "project"
        self.project.mkdir(parents=True)
        for directory in dirs:
            (self.project / directory).mkdir(parents=True, exist_ok=True)
        self.env = self.isolated.subprocess_env()
        self.env["APM_NON_INTERACTIVE"] = "1"
        self.env["MCP_CHAIN_TOKEN"] = SECRET
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)

    def run(self, *args: str) -> CommandResult:
        return self.runner.run(args, cwd=self.project, env=self.env, scenario_id=" ".join(args))

    def ok(self, *args: str) -> CommandResult:
        result = self.run(*args)
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
        return result

    def write_manifest(self, mcp: list[dict], apm: list | None = None, **extra) -> None:
        manifest = {"name": "mcp-chain", "version": "1.0.0", **extra}
        manifest["dependencies"] = {"apm": apm or [], "mcp": mcp}
        (self.project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def servers(self, target: str) -> dict:
        relative, key = JSON_CONFIGS[target]
        path = self.project / relative
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get(key, {})

    def codex_servers(self) -> list[str]:
        path = self.project / ".codex" / "config.toml"
        if not path.exists():
            return []
        found = re.findall(r"^\[mcp_servers\.([^\]]+)\]$", path.read_text("utf-8"), re.M)
        return [name.strip('"') for name in found if not name.endswith(".env")]

    def lock(self) -> dict:
        path = self.project / "apm.lock.yaml"
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def seed_org_policy(self, policy_yaml: str) -> None:
        """Cache an org policy for ``github.com/acme`` so discovery stays offline."""
        subprocess.run(["git", "init", "-q"], cwd=self.project, env=self.env, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/project.git"],
            cwd=self.project,
            env=self.env,
            check=True,
        )
        policy_path = self.isolated.root / "policy.yml"
        policy_path.write_text(policy_yaml, encoding="utf-8")
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from apm_cli.policy.discovery import _write_cache\n"
            "from apm_cli.policy.parser import load_policy\n"
            "policy, _ = load_policy(Path(sys.argv[2]).read_text())\n"
            "_write_cache('acme/.github-private', policy, Path(sys.argv[1]))\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(self.project), str(policy_path)],
            env=self.env,
            check=True,
        )


def stdio_server(name: str, *args: str, env: dict | None = None) -> dict:
    server = {
        "name": name,
        "registry": False,
        "transport": "stdio",
        "command": "npx",
        "args": list(args),
    }
    if env:
        server["env"] = env
    return server


def http_server(name: str, url: str, headers: dict | None = None) -> dict:
    server = {"name": name, "registry": False, "transport": "http", "url": url}
    if headers:
        server["headers"] = headers
    return server
