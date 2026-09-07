"""Opt-in binary qualification for native Agent Plugin loading (issue #2703).

These tests prove the whole PR #2705 contract against the real, SHA-pinned
GitHub Copilot CLI artifact that qualified the feature:

    ``apm pack --format agent-plugin`` -> ``apm install --target copilot``
    -> plain ``copilot`` loads the plugin live.

They are doubly opt-in (``APM_E2E_TESTS=1`` **and**
``APM_RUN_INTEGRATION_TESTS=1``) because they download an 80+ MB release
artifact and execute a third-party binary. The normal CI run stays hermetic
and deterministic; every claim these tests make is also covered hermetically
by ``tests/unit/copilot_plugins/``.

Isolation contract (this runs on the operator's real machine):

* ``COPILOT_HOME`` always points into the pytest tmp tree, so the operator's
  real ``~/.copilot`` is never written.
* Global-scope APM installs are redirected by pointing ``HOME`` at the tmp
  tree (user scope resolves ``Path.home()/.apm``), so the operator's real
  ``~/.apm`` is never written.
* ``_run_copilot`` hands the downloaded binary a minimal environment
  allowlist -- never the ambient ``dict(os.environ)``. APM's own secrets
  (``GITHUB_APM_PAT`` / ``GITHUB_TOKEN`` / ``ADO_APM_PAT`` and any cloud
  credentials) are deliberately withheld. The one place that needs an
  authenticated Copilot session (the MCP handshake) opts in to reading the
  operator's own Copilot login via ``HOME`` -- read-only -- and passes no
  raw token to the binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
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

COPILOT_RELEASE_TAG = "v1.0.81"
COPILOT_ASSET = "copilot-darwin-arm64.tar.gz"
COPILOT_ASSET_SHA256 = "568b0d6fe88d573c171ab95887d33276802dda2c5ca3cee7d0fe438df2343be4"
COPILOT_ASSET_SIZE = 84675389
COPILOT_ASSET_URL = (
    f"https://github.com/github/copilot-cli/releases/download/{COPILOT_RELEASE_TAG}/{COPILOT_ASSET}"
)

_PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

# A real stdio MCP server. It records every JSON-RPC ``method`` it receives to
# ``$APM_MCP_MARKER`` and answers ``initialize`` + ``tools/list`` so a genuine
# client handshake completes. This proves the plugin's server was actually
# spoken to, not merely listed.
_PROBE_JS = (
    "const fs = require('fs');\n"
    "const marker = process.env.APM_MCP_MARKER;\n"
    "let buffer = '';\n"
    "process.stdin.on('data', (chunk) => {\n"
    "  buffer += chunk;\n"
    "  let index;\n"
    "  while ((index = buffer.indexOf('\\n')) >= 0) {\n"
    "    const line = buffer.slice(0, index);\n"
    "    buffer = buffer.slice(index + 1);\n"
    "    if (!line.trim()) continue;\n"
    "    let message;\n"
    "    try { message = JSON.parse(line); } catch (e) { continue; }\n"
    "    if (marker && message.method) fs.appendFileSync(marker, message.method + '\\n');\n"
    "    if (message.method === 'initialize') {\n"
    "      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id, result: {\n"
    "        protocolVersion: '2024-11-05', capabilities: {tools: {}},\n"
    "        serverInfo: {name: 'apm-probe', version: '1.0.0'}}}) + '\\n');\n"
    "    } else if (message.method === 'tools/list') {\n"
    "      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id,\n"
    "        result: {tools: [{name: 'apm_probe_tool', description: 'probe',\n"
    "        inputSchema: {type: 'object', properties: {}}}]}}) + '\\n');\n"
    "    } else if (message.method === 'tools/call') {\n"
    "      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id,\n"
    "        result: {content: [{type: 'text', text: 'PROBE_OK'}]}}) + '\\n');\n"
    "    }\n"
    "  }\n"
    "});\n"
)


def _supported_host() -> bool:
    """Return ``True`` only where the pinned artifact digest applies."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


# ---------------------------------------------------------------------------
# Environment allowlist + Copilot invocation
# ---------------------------------------------------------------------------


def _run_copilot(
    binary: Path,
    home: Path,
    cwd: Path,
    *args: str,
    marker: Path | None = None,
    use_login: bool = False,
) -> subprocess.CompletedProcess:
    """Run the pinned Copilot CLI with a minimal, isolated environment.

    Only an explicit allowlist is forwarded -- never ``dict(os.environ)`` --
    so APM's own secrets and any ambient cloud credentials are withheld from
    the downloaded third-party binary (defect #5). ``COPILOT_HOME`` is always
    redirected into the tmp tree.

    ``use_login`` is opt-in for the single invocation (the MCP handshake) that
    needs an authenticated Copilot session: it lets the binary read the
    operator's own Copilot login via a real ``HOME`` (read-only). Every other
    invocation gets a throwaway ``HOME`` inside the tmp tree. The real home is
    read from the passwd database, not ``os.environ`` -- the suite's hermetic
    conftest clobbers ``$HOME`` to a tmp dir, which would hide the login. Even
    with a real ``HOME``, ``COPILOT_HOME`` still points at tmp, so every write
    stays isolated and only the read-only auth lookup sees the real home.
    """
    fake_home = home.parent / (home.name + "-fakehome")
    fake_home.mkdir(parents=True, exist_ok=True)
    login_home = pwd.getpwuid(os.getuid()).pw_dir if use_login else str(fake_home)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "COPILOT_HOME": str(home),
        "COPILOT_ALLOW_ALL": "true",
        "HOME": login_home,
    }
    for passthrough in ("LANG", "LC_ALL", "TMPDIR"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    if marker is not None:
        env["APM_MCP_MARKER"] = str(marker)
    return subprocess.run(
        [str(binary), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def copilot_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Download, digest-verify, and extract the pinned Copilot CLI artifact.

    The stable-release digest is verified BEFORE the 84 MB payload is written to the tar or
    executed (defect #5): the bytes are hashed in memory first, and only a
    size + sha256 match unlocks extraction.
    """
    if not _supported_host():
        pytest.skip("The pinned Copilot artifact digest covers darwin-arm64 only")
    root = tmp_path_factory.mktemp("copilot-cli")
    with urllib.request.urlopen(COPILOT_ASSET_URL, timeout=300) as response:  # noqa: S310
        payload = response.read()
    # Verify-before-use: assert the pin holds before any byte is trusted.
    assert len(payload) == COPILOT_ASSET_SIZE, (
        f"artifact size {len(payload)} != pinned {COPILOT_ASSET_SIZE}"
    )
    digest = hashlib.sha256(payload).hexdigest()
    assert digest == COPILOT_ASSET_SHA256, f"artifact sha256 {digest} != pinned"
    archive = root / COPILOT_ASSET
    archive.write_bytes(payload)
    extracted = root / "extracted"
    extracted.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(extracted, filter="data")
    for candidate in sorted(extracted.rglob("copilot")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("Pinned Copilot artifact layout is not recognized")


def _write_author_plugin(project: Path, *, name: str, marker: str) -> Path:
    """Author an Agent Plugins 1.0 source project with a real stdio MCP server.

    The MCP server is declared as a self-defined stdio dependency in
    ``apm.yml`` (``registry: false``). This is the shape the agent-plugin
    exporter projects into the packed ``mcp.json`` -- a hand-written
    ``mcp.json`` is ignored by pack. Returns the absolute path of the probe
    script the packed bundle will point at.
    """
    project.mkdir(parents=True, exist_ok=True)
    probe = project / "probe.js"
    probe.write_text(_PROBE_JS, encoding="ascii")
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "APM native plugin qualification fixture",
                "license": "MIT",
                "dependencies": {
                    "apm": [],
                    "mcp": [
                        {
                            "name": f"{name}-mcp",
                            "registry": False,
                            "transport": "stdio",
                            "command": "node",
                            "args": [str(probe)],
                        }
                    ],
                },
            }
        ),
        encoding="ascii",
    )
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
    # The skill description carries the live-update marker: ``copilot skill
    # list`` reads it straight from the materialized bytes every session.
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: {marker}\n---\n\nqualification skill body\n",
        encoding="ascii",
    )
    return probe


@pytest.fixture(scope="module")
def packed_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Pack the author project once and share the read-only bundle.

    Returns the packed bundle directory plus the plugin name. Building this
    once (module scope) keeps total wall-time sane -- every consumer test
    re-declares this same bundle rather than re-packing.
    """
    root = tmp_path_factory.mktemp("author")
    author = root / "apm-qualification"
    _write_author_plugin(author, name="apm-qualification", marker="MARK_V1")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(author)
        # The author install only needs to resolve the self-defined MCP server
        # into ``apm.lock.yaml`` so pack can project it. ``--target claude``
        # satisfies harness detection and writes project-local (tmp) config;
        # it never touches the operator's real Claude/Copilot homes.
        installed = CliRunner().invoke(
            cli, ["install", "--no-policy", "--target", "claude"], catch_exceptions=False
        )
        assert installed.exit_code == 0, installed.output
        packed = CliRunner().invoke(
            cli,
            ["pack", "--format", "agent-plugin", "-o", str(root / "build")],
            catch_exceptions=False,
        )
        assert packed.exit_code == 0, packed.output
    bundle_dir = next(path.parent for path in sorted((root / "build").rglob("plugin.json")))
    return {"dir": bundle_dir, "name": "apm-qualification"}


# ---------------------------------------------------------------------------
# Shared consumer helpers
# ---------------------------------------------------------------------------


def _make_git_root(project: Path) -> None:
    """Materialize a minimal ``.git`` so ``git rev-parse`` roots here.

    The real Copilot CLI resolves project-scope settings from the git root,
    not the cwd. A tiny inert ``.git`` (no git binary invoked, honoring the
    no-git-writes rule) makes the consumer its own root.
    """
    git = project / ".git"
    (git / "objects").mkdir(parents=True, exist_ok=True)
    (git / "refs").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (git / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="ascii")


def _seed_trust(home: Path, folder: Path) -> None:
    """Pre-seed folder trust so project-scope discovery is non-interactive."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"trustedFolders": [str(folder)]}), encoding="ascii"
    )


def _read_jsonc(path: Path) -> dict:
    """Parse the Copilot ``config.json`` (JSONC with leading ``//`` comments)."""
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    ]
    return json.loads("\n".join(lines))


def _install_consumer(
    consumer: Path,
    dependency: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "qualification-consumer",
) -> CliRunner:
    """Declare a bundle/source under ``dependencies.apm`` and install it."""
    consumer.mkdir(parents=True, exist_ok=True)
    (consumer / "apm.yml").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "consumer",
                "dependencies": {"apm": [str(dependency)]},
            }
        ),
        encoding="ascii",
    )
    monkeypatch.chdir(consumer)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install", "--no-policy", "--target", "copilot"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return runner


def _write_source_plugin(project: Path, *, name: str, marker: str, apm_deps: list[str]) -> None:
    """Author a plain source plugin (used for the transitive dependency case)."""
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": name,
                "license": "MIT",
                "dependencies": {"apm": apm_deps},
            }
        ),
        encoding="ascii",
    )
    (project / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": _PLUGIN_SCHEMA_ID,
                "name": name,
                "version": "1.0.0",
                "description": name,
                "license": "MIT",
            },
            indent=2,
        ),
        encoding="ascii",
    )
    skill = project / "skills" / f"{name}-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: {marker}\n---\n\nbody\n",
        encoding="ascii",
    )


# ---------------------------------------------------------------------------
# Clause (a): the packed bundle is exact Agent Plugins 1.0 canonical shape.
# ---------------------------------------------------------------------------


def test_packed_bundle_is_canonical_agent_plugin(packed_bundle: dict) -> None:
    """``apm pack`` emits the exact canonical Agent Plugins 1.0 layout."""
    bundle = packed_bundle["dir"]
    name = packed_bundle["name"]

    manifest = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == _PLUGIN_SCHEMA_ID
    assert manifest["name"] == name
    # A skills/ directory with a real SKILL.md, at the bundle root.
    skill = bundle / "skills" / f"{name}-skill" / "SKILL.md"
    assert skill.is_file()

    # A ROOT mcp.json with the canonical schema and a stdio server -- NOT a
    # legacy ``.mcp.json`` and NOT ``mcpServers`` embedded in the manifest.
    assert not (bundle / ".mcp.json").exists()
    assert "mcpServers" not in manifest
    mcp = json.loads((bundle / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["$schema"] == _MCP_SCHEMA_ID
    server = mcp["mcpServers"][f"{name}-mcp"]
    assert server["type"] == "stdio"
    assert server["command"] == "node"
    assert server["args"] and server["args"][0].endswith("probe.js")


# ---------------------------------------------------------------------------
# Clauses (b), (d), (f), (h): declarative install -> plain Copilot pickup,
# absence outside the repo, live update, and no private copy.
# ---------------------------------------------------------------------------


def test_project_install_loads_live_in_plain_copilot(
    copilot_binary: Path,
    packed_bundle: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pack -> declare -> install -> plain Copilot loads it live (project scope)."""
    consumer = tmp_path / "consumer"
    _install_consumer(consumer, packed_bundle["dir"], monkeypatch)

    modules = consumer / "apm_modules"
    settings = json.loads(
        (consumer / ".github" / "copilot" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert catalog_path_for(modules).is_file()
    assert settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]["path"] == "apm_modules"
    assert settings[ENABLED_PLUGINS_KEY] == {"apm-qualification@apm": True}

    _make_git_root(consumer)
    home = tmp_path / "copilot-home"
    _seed_trust(home, consumer)

    # (b) The plain, unconfigured Copilot binary picks the plugin up live.
    plugins = _run_copilot(copilot_binary, home, consumer, "plugin", "list")
    skills = _run_copilot(copilot_binary, home, consumer, "skill", "list")
    servers = _run_copilot(copilot_binary, home, consumer, "mcp", "list")
    assert "apm-qualification@apm" in plugins.stdout, plugins.stdout + plugins.stderr
    assert "apm-qualification-skill" in skills.stdout, skills.stdout + skills.stderr
    assert "apm-qualification-mcp" in servers.stdout, servers.stdout + servers.stderr

    # (h) No plugin copy and no private Copilot registration were created.
    installed = home / "installed-plugins"
    assert not installed.exists() or not any(installed.rglob("*"))
    config = home / "config.json"
    if config.is_file():
        assert not _read_jsonc(config).get("installedPlugins")

    # (f) A live edit of the materialized skill reaches a fresh session with no
    # ``copilot plugin update``. The skill description ("MARK_V1") is read
    # straight from the bytes on disk every session.
    live_skill = (
        modules
        / "_local"
        / packed_bundle["dir"].name
        / "skills"
        / "apm-qualification-skill"
        / "SKILL.md"
    )
    before = _run_copilot(copilot_binary, home, consumer, "skill", "list")
    assert "MARK_V1" in before.stdout, before.stdout + before.stderr
    live_skill.write_text(
        live_skill.read_text(encoding="utf-8").replace("MARK_V1", "MARK_V2"), encoding="utf-8"
    )
    refreshed = _run_copilot(copilot_binary, home, consumer, "skill", "list")
    assert "MARK_V2" in refreshed.stdout, refreshed.stdout + refreshed.stderr

    # (d) A project registration is absent outside its repository.
    outside = tmp_path / "outside"
    outside.mkdir()
    away = _run_copilot(copilot_binary, home, outside, "plugin", "list")
    assert "apm-qualification@apm" not in away.stdout


# ---------------------------------------------------------------------------
# Clause (c): the MCP initialize + tools/list handshake actually reaches the
# plugin's stdio server.
# ---------------------------------------------------------------------------


def test_mcp_handshake_reaches_plugin_stdio_server(
    copilot_binary: Path,
    packed_bundle: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Copilot session speaks JSON-RPC to the plugin's MCP server."""
    consumer = tmp_path / "consumer"
    _install_consumer(consumer, packed_bundle["dir"], monkeypatch)
    _make_git_root(consumer)
    home = tmp_path / "copilot-home"
    _seed_trust(home, consumer)
    marker = tmp_path / "mcp-marker.txt"

    # ``copilot -p`` runs one non-interactive agent turn; the MCP client
    # connects to every configured server at session start, which drives the
    # initialize + tools/list handshake regardless of what the model does.
    session = _run_copilot(
        copilot_binary,
        home,
        consumer,
        "-p",
        "List the tools available to you.",
        "--allow-all-tools",
        "--no-color",
        marker=marker,
        use_login=True,
    )
    recorded = marker.read_text(encoding="utf-8") if marker.is_file() else ""
    if not recorded and "authenticat" in (session.stdout + session.stderr).lower():
        pytest.skip(
            "Copilot CLI is not authenticated in this environment; the MCP "
            "handshake requires an authenticated session. stderr:\n" + session.stderr
        )
    methods = set(recorded.split())
    assert "initialize" in methods, recorded or session.stdout + session.stderr
    assert "tools/list" in methods, recorded or session.stdout + session.stderr


# ---------------------------------------------------------------------------
# Clause (e): a global install is visible from an unrelated directory.
# ---------------------------------------------------------------------------


def test_global_install_visible_from_unrelated_directory(
    copilot_binary: Path,
    packed_bundle: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-scope install is loaded by Copilot from an unrelated cwd."""
    # Redirect the user root into tmp: user scope resolves ``Path.home()/.apm``
    # and ``Path.home()`` follows ``HOME``. The operator's real ~/.apm is never
    # touched.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    copilot_home = tmp_path / "cphome"
    copilot_home.mkdir()
    apm_dir = fake_home / ".apm"
    apm_dir.mkdir()
    (apm_dir / "apm.yml").write_text(
        json.dumps(
            {
                "name": "user-scope",
                "version": "1.0.0",
                "description": "user",
                "dependencies": {"apm": [str(packed_bundle["dir"])]},
            }
        ),
        encoding="ascii",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("COPILOT_HOME", str(copilot_home))
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    result = CliRunner().invoke(
        cli, ["install", "-g", "--no-policy", "--target", "copilot"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output

    # The operator's real ~/.apm must be untouched; the install landed in tmp.
    assert (apm_dir / "apm_modules").exists()
    global_settings = json.loads((copilot_home / "settings.json").read_text(encoding="utf-8"))
    # Global marketplace paths are absolute so cwd cannot matter.
    assert global_settings[EXTRA_MARKETPLACES_KEY]["apm"]["source"]["path"] == str(
        apm_dir / "apm_modules"
    )
    assert global_settings[ENABLED_PLUGINS_KEY] == {"apm-qualification@apm": True}

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    plugins = _run_copilot(copilot_binary, copilot_home, unrelated, "plugin", "list")
    assert "apm-qualification@apm" in plugins.stdout, plugins.stdout + plugins.stderr


# ---------------------------------------------------------------------------
# Clause (g): APM list / update-convergence / uninstall / prune, cross-checked
# against the real client.
# ---------------------------------------------------------------------------


def test_apm_lifecycle_and_copilot_uninstall_semantics(
    copilot_binary: Path,
    packed_bundle: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deps list, idempotent re-install, prune, uninstall -> Copilot drops it."""
    consumer = tmp_path / "consumer"
    runner = _install_consumer(consumer, packed_bundle["dir"], monkeypatch)
    catalog = catalog_path_for(consumer / "apm_modules")

    listed = runner.invoke(cli, ["deps", "list"], catch_exceptions=False)
    assert listed.exit_code == 0, listed.output
    assert "apm-qualification" in listed.output

    # Re-running install must converge, not accumulate duplicate catalog rows.
    reinstalled = runner.invoke(
        cli, ["install", "--no-policy", "--target", "copilot"], catch_exceptions=False
    )
    assert reinstalled.exit_code == 0, reinstalled.output
    rows = [entry["name"] for entry in json.loads(catalog.read_text(encoding="utf-8"))["plugins"]]
    assert rows == ["apm-qualification"]

    # prune is convergent here: the plugin is still declared, so prune retires
    # nothing and leaves the live row intact (retire only orphaned rows).
    pruned = runner.invoke(cli, ["prune"], catch_exceptions=False)
    assert pruned.exit_code == 0, pruned.output
    assert catalog.is_file()
    rows_after_prune = [
        entry["name"] for entry in json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
    ]
    assert rows_after_prune == ["apm-qualification"]

    _make_git_root(consumer)
    home = tmp_path / "copilot-home"
    _seed_trust(home, consumer)
    present = _run_copilot(copilot_binary, home, consumer, "plugin", "list")
    assert "apm-qualification@apm" in present.stdout, present.stdout + present.stderr

    # Uninstall retires only APM-owned rows; the source bundle bytes survive.
    removed = runner.invoke(cli, ["uninstall", str(packed_bundle["dir"])], catch_exceptions=False)
    assert removed.exit_code == 0, removed.output
    cleaned = json.loads(
        (consumer / ".github" / "copilot" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert ENABLED_PLUGINS_KEY not in cleaned
    assert (packed_bundle["dir"] / "plugin.json").is_file()

    # The REAL client no longer loads it after uninstall.
    gone = _run_copilot(copilot_binary, home, consumer, "plugin", "list")
    assert "apm-qualification@apm" not in gone.stdout, gone.stdout + gone.stderr


# ---------------------------------------------------------------------------
# Clause (i): direct PLUS transitive dependency both land in one aggregate
# catalog and both are loaded by the real client.
# ---------------------------------------------------------------------------


def test_direct_and_transitive_plugins_load_from_one_catalog(
    copilot_binary: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin B (a transitive apm dep of A) is registered and loaded too."""
    leaf = tmp_path / "plugin-b"
    _write_source_plugin(leaf, name="plugin-b", marker="BMARK", apm_deps=[])
    direct = tmp_path / "plugin-a"
    _write_source_plugin(direct, name="plugin-a", marker="AMARK", apm_deps=[str(leaf)])

    consumer = tmp_path / "consumer"
    _install_consumer(consumer, direct, monkeypatch, name="transitive-consumer")

    catalog = json.loads(catalog_path_for(consumer / "apm_modules").read_text(encoding="utf-8"))
    assert catalog["name"] == "apm"
    names = sorted(entry["name"] for entry in catalog["plugins"])
    assert names == ["plugin-a", "plugin-b"]
    settings = json.loads(
        (consumer / ".github" / "copilot" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert settings[ENABLED_PLUGINS_KEY] == {"plugin-a@apm": True, "plugin-b@apm": True}

    _make_git_root(consumer)
    home = tmp_path / "copilot-home"
    _seed_trust(home, consumer)
    plugins = _run_copilot(copilot_binary, home, consumer, "plugin", "list")
    assert "plugin-a@apm" in plugins.stdout, plugins.stdout + plugins.stderr
    assert "plugin-b@apm" in plugins.stdout, plugins.stdout + plugins.stderr
