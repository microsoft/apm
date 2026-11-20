"""Integration tests: ``apm install --target opencode`` with ``dependencies.lsp`` (#3727).

A package that declares LSP servers used to make the install fail for the
opencode target. LSP servers are now written to the ``lsp`` key of the
project ``opencode.json``. The tests drive the real CLI entry point
in-process with a local path dependency, so no network access is needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockFile

pytestmark = [pytest.mark.integration, pytest.mark.component]

_PACKAGE_MANIFEST = """\
name: lsp-pkg
version: 1.0.0
dependencies:
  lsp:
    - name: gopls
      command: gopls
      args: []
      extensionToLanguage:
        ".go": go
      startupTimeout: 60000
"""


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """``pkg/`` (declares an LSP server) and an empty sibling ``proj/``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("APM_E2E_TESTS", "1")
    monkeypatch.setenv("NO_COLOR", "1")

    pkg = tmp_path / "pkg"
    skill = pkg / ".apm" / "skills" / "demo"
    skill.mkdir(parents=True)
    (pkg / "apm.yml").write_text(_PACKAGE_MANIFEST, encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nBody.\n", encoding="utf-8"
    )
    proj = tmp_path / "proj"
    proj.mkdir()

    orig_cwd = os.getcwd()
    os.chdir(proj)
    try:
        yield proj
    finally:
        os.chdir(orig_cwd)


_MIXED_TRANSPORT_MANIFEST = """name: lsp-pkg
version: 1.0.0
dependencies:
  lsp:
    - name: gopls
      command: gopls
      extensionToLanguage:
        ".go": go
    - name: sock
      command: sock-server
      transport: socket
      extensionToLanguage:
        ".s": s
"""


def _install(target: str, *extra: str):
    from apm_cli.cli import cli

    return CliRunner().invoke(cli, ["install", "--target", target, *extra])


class TestInstallOpenCodeLsp:
    def test_fresh_project_writes_lsp_to_opencode_json(self, world: Path):
        result = _install("opencode", "../pkg")

        assert result.exit_code == 0, result.output
        assert "Configured 1 LSP server in opencode.json" in result.output

        config = json.loads((world / "opencode.json").read_text(encoding="utf-8"))
        assert config == {
            "$schema": "https://opencode.ai/config.json",
            "lsp": {"gopls": {"command": ["gopls"], "extensions": [".go"]}},
        }

        manifest = yaml.safe_load((world / "apm.yml").read_text(encoding="utf-8"))
        assert "../pkg" in manifest["dependencies"]["apm"]

        lock = LockFile.read(world / "apm.lock.yaml")
        assert lock is not None
        assert lock.lsp_servers == ["gopls"]
        assert lock.lsp_target_servers == {"opencode": ["gopls"]}

    def test_existing_opencode_json_is_preserved(self, world: Path):
        (world / "opencode.json").write_text(
            json.dumps(
                {
                    "mcp": {"other": {"type": "local", "command": ["other-mcp"]}},
                    "theme": "dark",
                }
            ),
            encoding="utf-8",
        )

        result = _install("opencode", "../pkg")

        assert result.exit_code == 0, result.output
        config = json.loads((world / "opencode.json").read_text(encoding="utf-8"))
        assert config["mcp"] == {"other": {"type": "local", "command": ["other-mcp"]}}
        assert config["theme"] == "dark"
        assert config["lsp"] == {"gopls": {"command": ["gopls"], "extensions": [".go"]}}
        assert "$schema" not in config

    def test_target_without_lsp_support_still_fails(self, world: Path):
        result = _install("cursor", "../pkg")

        assert result.exit_code != 0
        assert "--target opencode" in result.output

    def test_unparseable_opencode_json_is_not_overwritten(self, world: Path):
        content = '{\n  // user comment\n  "mcp": {"other": {"type": "local"}}\n}\n'
        (world / "opencode.json").write_text(content, encoding="utf-8")

        result = _install("opencode", "../pkg")

        assert result.exit_code != 0
        assert "opencode.json" in result.output
        assert (world / "opencode.json").read_text(encoding="utf-8") == content

    def test_frozen_replays_mixed_transports(self, world: Path):
        (world.parent / "pkg" / "apm.yml").write_text(_MIXED_TRANSPORT_MANIFEST, encoding="utf-8")

        first = _install("copilot,opencode", "../pkg")
        assert first.exit_code == 0, first.output
        lock = LockFile.read(world / "apm.lock.yaml")
        assert lock is not None
        assert lock.lsp_target_servers == {"copilot": ["gopls", "sock"], "opencode": ["gopls"]}

        replay = _install("copilot,opencode", "--frozen")

        assert replay.exit_code == 0, replay.output
        config = json.loads((world / "opencode.json").read_text(encoding="utf-8"))
        assert list(config["lsp"]) == ["gopls"]
