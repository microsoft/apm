"""Regenerate the 0.36.1 harness-track upgrade fixtures from a tagged APM release.

Usage (from the repository root, with the tagged source checked out at
``<tag-worktree>``, using the repository virtualenv interpreter)::

    .venv/bin/python tests/fixtures/upgrade_0361/t3/generate_fixtures.py \
        --label v0.36.0 --source <tag-worktree>/src --version 0.36.0

The script runs ``apm install`` from that source tree against a temp HOME
(network disabled except a loopback MCP Registry v0.1 endpoint) and copies
the produced manifests, lockfiles and native harness configs into
``tests/fixtures/upgrade_0361/t3/<label>/``. The registry URL is replaced
with ``__REGISTRY_URL__`` so tests can replay against their own endpoint.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO))

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment  # noqa: E402
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory  # noqa: E402

REGISTRY_NAME = "io.github.apm-fixture/heal-registry"
REGISTRY_PLACEHOLDER = "__REGISTRY_URL__"
REGISTRY_DOCUMENT = {
    "name": REGISTRY_NAME,
    "description": "upgrade heal fixture",
    "version": "1.0.0",
    "remotes": [{"type": "sse", "url": "https://registry-sse.example.invalid/sse"}],
}
SELF_DEFINED = [
    {
        "name": "heal-sse",
        "registry": False,
        "transport": "sse",
        "url": "https://sse.example.invalid/sse",
    },
    {
        "name": "heal-http",
        "registry": False,
        "transport": "http",
        "url": "https://http.example.invalid/mcp",
    },
    {
        "name": "heal-streamable",
        "registry": False,
        "transport": "streamable-http",
        "url": "https://streamable.example.invalid/mcp",
    },
    {
        "name": "heal-stdio",
        "registry": False,
        "transport": "stdio",
        "command": "node",
        "args": ["server.js"],
        "cwd": "/srv/heal-fixture",
    },
]
PROJECT_TARGETS = "vscode,copilot,claude,gemini,antigravity,hermes"
USER_TARGETS = "copilot,claude,gemini,antigravity,hermes"
PROJECT_FILES = (
    "project/apm.yml",
    "project/apm.lock.yaml",
    "project/.vscode/mcp.json",
    "project/.github/mcp.json",
    "project/.mcp.json",
    "project/.gemini/settings.json",
    "project/.agents/mcp_config.json",
    "home/.hermes/config.yaml",
)
USER_FILES = (
    "home/.apm/apm.yml",
    "home/.apm/apm.lock.yaml",
    "home/.copilot/mcp-config.json",
    "home/.claude.json",
    "home/.gemini/settings.json",
    "home/.gemini/config/mcp_config.json",
    "home/.hermes/config.yaml",
)
AGENT_FILES = (
    "home/.apm/apm.yml",
    "home/.apm/agents/kit-agent.agent.md",
    "home/.codex/agents/kit-agent.toml",
    "home/.kiro/agents/kit-agent.md",
    "home/.copilot/agents/kit-agent.agent.md",
    "home/.claude/agents/kit-agent.md",
)
AGENT_SOURCE = "---\nname: kit-agent\ndescription: Research helper\n---\n\nAgent body.\n"


def _command(source: Path, version: str) -> list[str]:
    # The lockfile reads importlib.metadata, so pin both version sources.
    code = (
        "import sys, importlib.metadata as md; sys.path.insert(0, sys.argv.pop(1)); "
        "pinned = sys.argv.pop(1); real = md.version; "
        "md.version = lambda name: pinned if name == 'apm-cli' else real(name); "
        "import apm_cli.version as v; v.__BUILD_VERSION__ = pinned; "
        "from apm_cli.cli import main; sys.exit(main())"
    )
    return [sys.executable, "-c", code, str(source), version]


def _run(command: list[str], args: list[str], cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run([*command, *args], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{args} failed:\n{result.stdout}\n{result.stderr}")


def _scene(base: Path, name: str) -> tuple[IsolatedApmEnvironment, Path, dict[str, str]]:
    isolated = IsolatedApmEnvironment.create(base / name, base_env=dict(os.environ))
    project = isolated.work_root / "project"
    for directory in (".vscode", ".github", ".claude", ".gemini", ".agents", ".codex"):
        (project / directory).mkdir(parents=True)
    env = isolated.subprocess_env(
        overrides={"APM_NON_INTERACTIVE": "1", "MCP_REGISTRY_ALLOW_HTTP": "1"}
    )
    for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME"):
        env.pop(variable, None)
    return isolated, project, env


def _export(
    isolated: IsolatedApmEnvironment,
    project: Path,
    files: tuple[str, ...],
    out: Path,
    replacements: dict[str, str],
) -> None:
    roots = {"project": project, "home": isolated.home}
    for relative in files:
        head, _, tail = relative.partition("/")
        source = roots[head] / tail
        if not source.is_file():
            raise SystemExit(f"expected output missing: {relative}")
        text = source.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        if str(isolated.root) in text or str(isolated.root.resolve()) in text:
            raise SystemExit(f"absolute temp path leaked into {relative}")
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _mcp_scene(base: Path, command: list[str], *, user: bool, out: Path) -> None:
    isolated, project, env = _scene(base, "user" if user else "project")
    with LocalMcpRegistryFactory(isolated.root / "registry").start(REGISTRY_DOCUMENT) as registry:
        env["APM_TEST_LOOPBACK_PORTS"] = registry.url.rsplit(":", 1)[1]
        servers = [*SELF_DEFINED, {"name": REGISTRY_NAME, "registry": registry.url}]
        manifest = {
            "name": "heal-consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [], "mcp": servers},
        }
        root = isolated.config_root if user else project
        (root / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        args = ["install", "--no-policy", "--target", USER_TARGETS if user else PROJECT_TARGETS]
        if user:
            args.append("--global")
        _run(command, args, project, env)
        _export(
            isolated,
            project,
            USER_FILES if user else PROJECT_FILES,
            out / ("user-mcp" if user else "project-mcp"),
            {registry.url: REGISTRY_PLACEHOLDER},
        )


def _codex_scene(base: Path, command: list[str], out: Path) -> None:
    """Registry SSE remote for Codex only (self-defined SSE aborted Codex installs)."""
    isolated, project, env = _scene(base, "codex")
    with LocalMcpRegistryFactory(isolated.root / "registry").start(REGISTRY_DOCUMENT) as registry:
        env["APM_TEST_LOOPBACK_PORTS"] = registry.url.rsplit(":", 1)[1]
        manifest = {
            "name": "heal-consumer",
            "version": "1.0.0",
            "dependencies": {
                "apm": [],
                "mcp": [SELF_DEFINED[1], {"name": REGISTRY_NAME, "registry": registry.url}],
            },
        }
        (project / "apm.yml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        _run(command, ["install", "--no-policy", "--target", "codex"], project, env)
        _export(
            isolated,
            project,
            ("project/apm.yml", "project/apm.lock.yaml", "project/.codex/config.toml"),
            out / "project-codex",
            {registry.url: REGISTRY_PLACEHOLDER},
        )


def _agent_scene(base: Path, command: list[str], out: Path) -> None:
    isolated, project, env = _scene(base, "agents")
    (isolated.config_root / "apm.yml").write_text(
        "name: user-profile\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="utf-8"
    )
    (isolated.config_root / "agents").mkdir()
    (isolated.config_root / "agents" / "kit-agent.agent.md").write_text(
        AGENT_SOURCE, encoding="utf-8"
    )
    _run(
        command,
        ["install", "-g", "--no-policy", "--target", "codex,kiro,copilot,claude"],
        project,
        env,
    )
    _export(isolated, project, AGENT_FILES, out / "user-agents", {})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--version", required=True)
    options = parser.parse_args()
    out = Path(__file__).resolve().parent / options.label
    if out.exists():
        shutil.rmtree(out)
    command = _command(options.source.resolve(), options.version)
    with tempfile.TemporaryDirectory(prefix="apm-upgrade-fixture-") as temp:
        base = Path(temp)
        _mcp_scene(base, command, user=False, out=out)
        _mcp_scene(base, command, user=True, out=out)
        _codex_scene(base, command, out)
        _agent_scene(base, command, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
