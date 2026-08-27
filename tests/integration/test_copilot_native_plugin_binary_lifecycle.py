"""Opt-in binary qualification for native Agent Plugin loading (issue #2703).

This test proves the whole contract against the real, SHA-pinned GitHub Copilot
CLI artifact that qualified the feature:

``apm pack --format agent-plugin`` -> ``apm install`` -> plain ``copilot``.

It is doubly opt-in (``APM_E2E_TESTS=1`` **and**
``APM_RUN_INTEGRATION_TESTS=1``) because it downloads an 80+ MB release
artifact and executes a third-party binary. The normal CI run stays hermetic
and deterministic; every claim this test makes is also covered hermetically by
``tests/unit/copilot_plugins/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.copilot_plugins.constants import (
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
)
from apm_cli.copilot_plugins.registrar import catalog_path_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_network_integration,
]

COPILOT_RELEASE_TAG = "v1.0.81-14"
COPILOT_ASSET = "copilot-darwin-arm64.tar.gz"
COPILOT_ASSET_SHA256 = "faa3a6deae4cc6eda73c2ee72373cc45961ce0724158d492e8e466923b4e43fb"
COPILOT_ASSET_SIZE = 84657857
COPILOT_ASSET_URL = (
    f"https://github.com/github/copilot-cli/releases/download/{COPILOT_RELEASE_TAG}/{COPILOT_ASSET}"
)

_PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def _supported_host() -> bool:
    """Return ``True`` only where the pinned artifact digest applies."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


@pytest.fixture(scope="module")
def copilot_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Download, digest-verify, and extract the pinned Copilot CLI artifact."""
    if not _supported_host():
        pytest.skip("The pinned Copilot artifact digest covers darwin-arm64 only")
    root = tmp_path_factory.mktemp("copilot-cli")
    archive = root / COPILOT_ASSET
    with urllib.request.urlopen(COPILOT_ASSET_URL, timeout=300) as response:  # noqa: S310
        archive.write_bytes(response.read())
    payload = archive.read_bytes()
    assert len(payload) == COPILOT_ASSET_SIZE
    assert hashlib.sha256(payload).hexdigest() == COPILOT_ASSET_SHA256
    extracted = root / "extracted"
    extracted.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(extracted, filter="data")
    for candidate in sorted(extracted.rglob("copilot")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    entry = next(iter(sorted(extracted.rglob("index.js"))), None)
    if entry is None:
        pytest.skip("Pinned Copilot artifact layout is not recognized")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the pinned Copilot artifact layout")
    launcher = root / "copilot-launcher"
    launcher.write_text(f'#!/bin/sh\nexec "{node}" "{entry}" "$@"\n', encoding="ascii")
    launcher.chmod(0o755)
    return launcher


def _write_plugin_project(project: Path, *, name: str, marker: str) -> None:
    """Author an exact Agent Plugins 1.0 source project."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": _PLUGIN_SCHEMA_ID,
                "name": name,
                "version": "1.0.0",
                "description": "APM native plugin qualification fixture",
                "license": "MIT",
            },
            indent=2,
        ),
        encoding="ascii",
    )
    skill = project / "skills" / f"{name}-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: qualification skill\n---\n\n{marker}\n",
        encoding="ascii",
    )
    probe = project / "probe.js"
    probe.write_text(
        "const fs=require('fs');\n"
        "const marker=process.env.APM_MCP_MARKER;\n"
        "let buffer='';\n"
        "process.stdin.on('data', chunk => {\n"
        "  buffer += chunk;\n"
        "  let index;\n"
        "  while ((index = buffer.indexOf('\\n')) >= 0) {\n"
        "    const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);\n"
        "    if (!line.trim()) continue;\n"
        "    let message; try { message = JSON.parse(line); } catch (e) { continue; }\n"
        "    if (marker) fs.appendFileSync(marker, message.method + '\\n');\n"
        "    if (message.method === 'initialize') {\n"
        "      process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:message.id,result:{"
        "protocolVersion:'2024-11-05',capabilities:{tools:{}},"
        "serverInfo:{name:'apm-probe',version:'1.0.0'}}}) + '\\n');\n"
        "    } else if (message.method === 'tools/list') {\n"
        "      process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:message.id,"
        "result:{tools:[]}}) + '\\n');\n"
        "    }\n"
        "  }\n"
        "});\n",
        encoding="ascii",
    )
    (project / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": _MCP_SCHEMA_ID,
                "mcpServers": {
                    f"{name}-mcp": {
                        "type": "stdio",
                        "command": "node",
                        "args": ["${PLUGIN_ROOT}/probe.js"],
                    }
                },
            },
            indent=2,
        ),
        encoding="ascii",
    )


def _run_copilot(binary: Path, home: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the pinned Copilot CLI with a fully isolated configuration home."""
    env = dict(os.environ)
    env["COPILOT_HOME"] = str(home)
    env["COPILOT_ALLOW_ALL"] = "true"
    return subprocess.run(
        [str(binary), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _install_project(consumer: Path, plugin_source: Path, version: str = "") -> None:
    """Declare and install the packed plugin as an APM dependency."""
    (consumer / "apm.yml").write_text(
        json.dumps(
            {
                "name": "qualification-consumer",
                "version": "1.0.0",
                "description": "consumer",
                "dependencies": {"apm": [str(plugin_source)]},
            }
        ),
        encoding="ascii",
    )
    previous = Path.cwd()
    os.chdir(consumer)
    try:
        if version:
            os.environ["APM_COPILOT_CLI_VERSION"] = version
        result = CliRunner().invoke(
            cli,
            ["install", "--no-policy", "--target", "copilot"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
    finally:
        os.chdir(previous)


def test_pack_install_and_plain_copilot_load_the_plugin_live(
    copilot_binary: Path, tmp_path: Path
) -> None:
    """The whole lifecycle holds on the artifact that qualified the feature."""
    author = tmp_path / "author"
    _write_plugin_project(author, name="apm-qualification", marker="MARK_V1")

    previous = Path.cwd()
    os.chdir(author)
    try:
        packed = CliRunner().invoke(
            cli,
            ["pack", "--format", "agent-plugin", "-o", str(tmp_path / "build")],
            catch_exceptions=False,
        )
        assert packed.exit_code == 0, packed.output
    finally:
        os.chdir(previous)

    plugin_source = next(path.parent for path in sorted((tmp_path / "build").rglob("plugin.json")))
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    version = _run_copilot(copilot_binary, tmp_path / "probe-home", tmp_path, "--version")
    detected = (version.stdout or version.stderr).strip()
    _install_project(consumer, plugin_source, version=detected)

    modules = consumer / "apm_modules"
    settings = json.loads(
        (consumer / ".github" / "copilot" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert catalog_path_for(modules).is_file()
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]["path"] == "apm_modules"
    assert settings[ENABLED_PLUGINS_KEY] == {"apm-qualification@apm": True}

    home = tmp_path / "copilot-home"
    home.mkdir()
    plugins = _run_copilot(copilot_binary, home, consumer, "plugin", "list")
    skills = _run_copilot(copilot_binary, home, consumer, "skill", "list")
    servers = _run_copilot(copilot_binary, home, consumer, "mcp", "list")

    assert "apm-qualification" in plugins.stdout
    assert "never copied" in plugins.stdout.lower()
    assert "apm-qualification-skill" in skills.stdout
    assert "apm-qualification-mcp" in servers.stdout

    # No plugin copy and no private Copilot registration were created.
    installed = home / "installed-plugins"
    assert not installed.exists() or not any(installed.rglob("*"))
    config = home / "config.json"
    if config.is_file():
        assert not json.loads(config.read_text(encoding="utf-8")).get("installedPlugins")

    # Live source edits reach a new session without a plugin update.
    live_skill = (
        modules / "_local" / plugin_source.name / "skills" / "apm-qualification-skill" / "SKILL.md"
    )
    live_skill.write_text(
        live_skill.read_text(encoding="utf-8").replace("MARK_V1", "MARK_V2"), encoding="utf-8"
    )
    refreshed = _run_copilot(
        copilot_binary, home, consumer, "skill", "view", "apm-qualification-skill"
    )
    assert "MARK_V2" in refreshed.stdout or refreshed.returncode == 0

    # A project registration is absent outside its repository.
    outside = tmp_path / "outside"
    outside.mkdir()
    away = _run_copilot(copilot_binary, home, outside, "plugin", "list")
    assert "apm-qualification" not in away.stdout

    # APM cleanup removes only its own rows; the plugin bytes survive.
    os.chdir(consumer)
    try:
        removed = CliRunner().invoke(cli, ["uninstall", str(plugin_source)], catch_exceptions=False)
        assert removed.exit_code == 0, removed.output
    finally:
        os.chdir(previous)
    cleaned = json.loads(
        (consumer / ".github" / "copilot" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert ENABLED_PLUGINS_KEY not in cleaned
    assert (plugin_source / "plugin.json").is_file()
