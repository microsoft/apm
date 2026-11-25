"""Shared hermetic harness-output scenarios driven by the installed ``apm`` binary.

One ``HarnessScene`` owns a temp HOME, a project with harness signal
directories, optional local MCP Registry v0.1 endpoints, and readers for every
harness's native MCP config at project and user scope (0.36.1 harness track).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory

SIGNAL_DIRS = (
    ".vscode",
    ".github",
    ".claude",
    ".gemini",
    ".agents",
    ".codex",
    ".cursor",
    ".opencode",
    ".kiro",
)

# (relative path from root, servers key, format); root is "project" or "home".
_PROJECT_CONFIGS: dict[str, tuple[str, str, str, str]] = {
    # ``vscode`` is a deprecated alias of ``copilot``; both share Claude's file.
    "vscode": ("project", ".mcp.json", "mcpServers", "json"),
    "copilot": ("project", ".mcp.json", "mcpServers", "json"),
    "claude": ("project", ".mcp.json", "mcpServers", "json"),
    "cursor": ("project", ".cursor/mcp.json", "mcpServers", "json"),
    "gemini": ("project", ".gemini/settings.json", "mcpServers", "json"),
    "antigravity": ("project", ".agents/mcp_config.json", "mcpServers", "json"),
    "codex": ("project", ".codex/config.toml", "mcp_servers", "toml"),
    "kiro": ("project", ".kiro/settings/mcp.json", "mcpServers", "json"),
    "opencode": ("project", "opencode.json", "mcp", "json"),
    "windsurf": ("home", ".codeium/windsurf/mcp_config.json", "mcpServers", "json"),
    "hermes": ("home", ".hermes/config.yaml", "mcp_servers", "yaml"),
}
_USER_CONFIGS: dict[str, tuple[str, str, str, str]] = {
    "copilot": ("home", ".copilot/mcp-config.json", "mcpServers", "json"),
    "claude": ("home", ".claude.json", "mcpServers", "json"),
    "cursor": ("home", ".cursor/mcp.json", "mcpServers", "json"),
    "gemini": ("home", ".gemini/settings.json", "mcpServers", "json"),
    "antigravity": ("home", ".gemini/config/mcp_config.json", "mcpServers", "json"),
    "codex": ("home", ".codex/config.toml", "mcp_servers", "toml"),
    "kiro": ("home", ".kiro/settings/mcp.json", "mcpServers", "json"),
    "opencode": ("home", ".config/opencode/opencode.json", "mcp", "json"),
    "windsurf": ("home", ".codeium/windsurf/mcp_config.json", "mcpServers", "json"),
    "hermes": ("home", ".hermes/config.yaml", "mcp_servers", "yaml"),
}


def flat(text: str) -> str:
    """Join terminal-width wrapping back into single spaces."""
    return " ".join(text.split())


def registry_document(
    name: str, url: str, *, transport_key: str = "type", transport: str | None
) -> dict[str, Any]:
    """Return one remote-only MCP Registry v0.1 server document."""
    remote: dict[str, Any] = {"url": url}
    if transport is not None:
        remote[transport_key] = transport
    return {
        "name": name,
        "description": "harness output fixture",
        "version": "1.0.0",
        "remotes": [remote],
    }


@dataclass
class HarnessScene:
    """A hermetic project plus temp HOME driven by the real ``apm`` binary."""

    isolated: IsolatedApmEnvironment
    project: Path
    runner: ApmLifecycleRunner
    env: dict[str, str]
    _stack: ExitStack = field(default_factory=ExitStack)
    _registries: list[LocalMcpRegistry] = field(default_factory=list)

    @classmethod
    def create(
        cls, root: Path, binary: Path, *, signal_dirs: Iterable[str] = SIGNAL_DIRS
    ) -> HarnessScene:
        isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
        project = isolated.work_root / "project"
        project.mkdir(parents=True)
        for directory in signal_dirs:
            (project / directory).mkdir(parents=True, exist_ok=True)
        env = isolated.subprocess_env(
            overrides={"APM_NON_INTERACTIVE": "1", "COLUMNS": "240", "MCP_REGISTRY_ALLOW_HTTP": "1"}
        )
        for name in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME"):
            env.pop(name, None)
        runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
        return cls(isolated=isolated, project=project, runner=runner, env=env)

    @property
    def home(self) -> Path:
        return self.isolated.home

    def close(self) -> None:
        self._stack.close()

    def start_registry(self, document: Mapping[str, Any]) -> str:
        """Start one local registry and allow its loopback port for children."""
        factory = LocalMcpRegistryFactory(self.isolated.root / f"registry-{len(self._registries)}")
        registry = self._stack.enter_context(factory.start(document))
        self._registries.append(registry)
        ports = [str(urlparse(item.url).port) for item in self._registries]
        self.env["APM_TEST_LOOPBACK_PORTS"] = ",".join(ports)
        return registry.url

    def manifest_root(self, user: bool) -> Path:
        return self.isolated.config_root if user else self.project

    def write_manifest(self, mcp: list[dict[str, Any]], *, user: bool, **extra: Any) -> Path:
        manifest: dict[str, Any] = {"name": "harness-output", "version": "1.0.0", **extra}
        manifest.setdefault("dependencies", {})
        manifest["dependencies"].setdefault("apm", [])
        manifest["dependencies"]["mcp"] = mcp
        path = self.manifest_root(user) / "apm.yml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        return path

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> CommandResult:
        return self.runner.run(
            args, cwd=self.project, env=dict(env or self.env), scenario_id=" ".join(args)
        )

    def ok(self, *args: str, env: Mapping[str, str] | None = None) -> CommandResult:
        result = self.run(*args, env=env)
        assert result.returncode == 0, describe(result)
        return result

    def install(self, targets: str, *, user: bool, extra: Iterable[str] = ()) -> CommandResult:
        args = ["install", "--no-policy", "--target", targets]
        if user:
            args.append("--global")
        return self.run(*args, *extra)

    def config_path(self, harness: str, *, user: bool) -> Path:
        table = _USER_CONFIGS if user else _PROJECT_CONFIGS
        base, relative, _key, _fmt = table[harness]
        root = self.home if base == "home" else self.project
        return root / relative

    def servers(self, harness: str, *, user: bool) -> dict[str, Any]:
        table = _USER_CONFIGS if user else _PROJECT_CONFIGS
        _base, _relative, key, fmt = table[harness]
        path = self.config_path(harness, user=user)
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        if fmt == "json":
            document = json.loads(text) if text.strip() else {}
        elif fmt == "toml":
            document = tomllib.loads(text)
        else:
            document = yaml.safe_load(text) or {}
        return dict(document.get(key) or {})

    def lockfile(self, *, user: bool) -> dict[str, Any]:
        path = self.manifest_root(user) / "apm.lock.yaml"
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def claimed_runtimes(self, server: str, *, user: bool) -> set[str]:
        """Runtimes whose MCP deployment for ``server`` the lockfile records."""
        deployments = self.lockfile(user=user).get("deployments") or []
        return {
            str(item.get("runtime"))
            for item in deployments
            if item.get("target") == "mcp" and item.get("value") == server
        }

    def native_bytes(self, harnesses: Iterable[str], *, user: bool) -> dict[str, bytes | None]:
        result: dict[str, bytes | None] = {}
        for harness in harnesses:
            path = self.config_path(harness, user=user)
            result[harness] = path.read_bytes() if path.exists() else None
        return result


def describe(result: CommandResult) -> str:
    return f"command={result.command!r}\nrc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"


def warning_segments(result: CommandResult) -> list[str]:
    """Return each flattened ``[!]`` warning segment of the combined output."""
    text = flat(result.stdout + " " + result.stderr)
    return [segment for segment in re.split(r"(?=\[[!x*i+>]\])", text) if segment.startswith("[!]")]
