"""Regenerate the #3781 user-scope fixtures from a tagged APM release.

Usage (from the repository root, with the tagged ``src/`` tree extracted at
``<tag-src>``, using the repository virtualenv interpreter)::

    .venv/bin/python tests/fixtures/user_global_update_audit/generate_fixtures.py \
        --label v0.35.1 --source <tag-src>/src --version 0.35.1

The script runs ``apm install --global`` from that source tree against a temp
HOME (no network) for a user manifest that declares no APM packages: one
self-defined stdio MCP server plus a root-local ``~/.apm/agents`` agent. It
copies the produced manifest, lockfile, user content and native harness files
into ``tests/fixtures/user_global_update_audit/<label>/``. ``~/.apm/apm_modules``
is never exported: it is disposable install state that dotfile sync and fresh
machines do not carry.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO))

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment  # noqa: E402

MANIFEST = """\
name: user-profile
version: 1.0.0
dependencies:
  apm: []
  mcp:
  - name: local-tool
    registry: false
    transport: stdio
    command: node
    args:
    - server.js
"""
AGENT = "---\nname: kit-agent\ndescription: Research helper\n---\n\nAgent body.\n"
FILES = (
    ".apm/apm.yml",
    ".apm/apm.lock.yaml",
    ".apm/agents/kit-agent.agent.md",
    ".copilot/mcp-config.json",
    ".copilot/agents/kit-agent.agent.md",
    ".claude.json",
    ".claude/agents/kit-agent.md",
)


def _command(source: Path, version: str) -> list[str]:
    # The lockfile stamps importlib.metadata's apm-cli version, so pin it too.
    code = (
        "import sys, importlib.metadata as m; sys.path.insert(0, sys.argv.pop(1)); "
        "pinned = sys.argv.pop(1); real = m.version; "
        "m.version = lambda name: pinned if name == 'apm-cli' else real(name); "
        "import apm_cli.version as v; v.__BUILD_VERSION__ = pinned; "
        "from apm_cli.cli import main; sys.exit(main())"
    )
    return [sys.executable, "-c", code, str(source), version]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--version", required=True)
    options = parser.parse_args()
    out = Path(__file__).resolve().parent / options.label
    if out.exists():
        shutil.rmtree(out)
    with tempfile.TemporaryDirectory(prefix="apm-3781-fixture-") as temp:
        isolated = IsolatedApmEnvironment.create(Path(temp) / "scene", base_env=dict(os.environ))
        env = isolated.subprocess_env(overrides={"APM_NON_INTERACTIVE": "1"})
        for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR"):
            env.pop(variable, None)
        for marker in (".copilot", ".claude"):
            (isolated.home / marker).mkdir()
        (isolated.config_root / "apm.yml").write_text(MANIFEST, encoding="utf-8")
        (isolated.config_root / "agents").mkdir()
        (isolated.config_root / "agents" / "kit-agent.agent.md").write_text(AGENT, encoding="utf-8")
        args = ["install", "--global", "--no-policy", "--target", "copilot,claude"]
        result = subprocess.run(
            [*_command(options.source.resolve(), options.version), *args],
            cwd=isolated.work_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"{args} failed:\n{result.stdout}\n{result.stderr}")
        for relative in FILES:
            source = isolated.home / relative
            if not source.is_file():
                raise SystemExit(f"expected output missing: {relative}")
            text = source.read_text(encoding="utf-8")
            if str(isolated.root) in text or str(isolated.root.resolve()) in text:
                raise SystemExit(f"absolute temp path leaked into {relative}")
            target = out / "home" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
