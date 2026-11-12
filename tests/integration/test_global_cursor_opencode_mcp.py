"""Global Cursor and OpenCode MCP config lifecycle from one APM package."""

import json

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component

_MANIFEST = """name: {name}
version: 1.0.0
dependencies:
  mcp:
    - name: {server}
      registry: false
      transport: stdio
      command: npx
      args: ["--yes", "example-server@1.0.0"]
"""


def _write_package(package, name, server, skill):
    """Write a package with one skill and one self-defined stdio MCP server."""
    package.mkdir()
    (package / "apm.yml").write_text(_MANIFEST.format(name=name, server=server), encoding="utf-8")
    skill_dir = package / ".apm" / "skills" / skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Example\n---\nUse {server}.\n",
        encoding="utf-8",
    )


def test_global_package_install_and_uninstall_preserve_foreign_servers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "unmarked-project"
    package = tmp_path / "package"
    home.mkdir()
    project.mkdir()
    _write_package(package, "global-mcp-test", "test-server", "hello")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    cursor_config = home / ".cursor" / "mcp.json"
    opencode_config = home / ".config" / "opencode" / "opencode.json"
    for config, key in ((cursor_config, "mcpServers"), (opencode_config, "mcp")):
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"unrelated": True, key: {"foreign": {"enabled": True}}}),
            encoding="utf-8",
        )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["install", "--global", str(package), "--target", "cursor,opencode"],
    )
    assert result.exit_code == 0, result.output
    cursor = json.loads(cursor_config.read_text(encoding="utf-8"))
    opencode = json.loads(opencode_config.read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["test-server"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["--yes", "example-server@1.0.0"],
    }
    assert opencode["mcp"]["test-server"] == {
        "type": "local",
        "enabled": True,
        "command": ["npx", "--yes", "example-server@1.0.0"],
    }
    assert cursor["unrelated"] is True
    assert opencode["unrelated"] is True
    assert not (project / ".cursor").exists()
    assert not (project / "opencode.json").exists()

    result = runner.invoke(cli, ["uninstall", "--global", str(package)])
    assert result.exit_code == 0, result.output
    for config, key in ((cursor_config, "mcpServers"), (opencode_config, "mcp")):
        contents = json.loads(config.read_text(encoding="utf-8"))
        assert contents["unrelated"] is True
        assert "foreign" in contents[key]
        assert "test-server" not in contents[key]


def test_global_install_and_uninstall_keep_same_name_foreign_servers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "unmarked-project"
    package = tmp_path / "package"
    home.mkdir()
    project.mkdir()
    _write_package(package, "same-name-mcp-test", "test-server", "hello")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    # The user configured a server under the name the package declares.
    configs = {
        home / ".cursor" / "mcp.json": {"mcpServers": {"test-server": {"command": "mine"}}},
        home / ".config" / "opencode" / "opencode.json": {
            "mcp": {"test-server": {"type": "local", "command": ["mine"]}},
        },
    }
    for config, contents in configs.items():
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(contents), encoding="utf-8")

    runner = CliRunner()
    for args in (
        ["install", "--global", str(package), "--target", "cursor,opencode"],
        ["uninstall", "--global", str(package)],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        for config, contents in configs.items():
            assert json.loads(config.read_text(encoding="utf-8")) == contents


def test_packed_bundle_installs_global_skills_and_mcp_from_any_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    package = tmp_path / "package"
    consumer = tmp_path / "consumer-without-apm-project"
    dist = tmp_path / "dist"
    for directory in (home, consumer, dist):
        directory.mkdir()
    _write_package(package, "portable-mcp-test", "portable-server", "portable-skill")
    monkeypatch.setenv("HOME", str(home))
    args = ["--yes", "example-server@1.0.0"]
    (package / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"portable-server": {"type": "stdio", "command": "npx", "args": args}}}
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    monkeypatch.chdir(package)
    prepared = runner.invoke(cli, ["lock"])
    assert prepared.exit_code == 0, prepared.output
    assert (package / "apm.lock.yaml").is_file()
    packed = runner.invoke(cli, ["pack", "--archive", "--target", "all", "-o", str(dist)])
    assert packed.exit_code == 0, packed.output
    archive = dist / "portable-mcp-test-1.0.0.zip"
    assert archive.is_file()

    monkeypatch.chdir(consumer)
    installed = runner.invoke(
        cli, ["install", "--global", str(archive), "--target", "cursor,opencode"]
    )
    assert installed.exit_code == 0, installed.output
    assert "Wired 1 MCP server" in installed.output
    assert (home / ".agents" / "skills" / "portable-skill" / "SKILL.md").is_file()
    assert (home / ".config" / "opencode" / "skills" / "portable-skill" / "SKILL.md").is_file()
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    opencode_path = home / ".config" / "opencode" / "opencode.json"
    opencode = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["portable-server"]["args"] == args
    assert opencode["mcp"]["portable-server"]["command"] == ["npx", *args]
    for marker in (".agents", ".cursor", ".opencode", "opencode.json"):
        assert not (consumer / marker).exists()


def test_global_install_leaves_unparseable_opencode_config_intact(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    package = tmp_path / "package"
    home.mkdir()
    project.mkdir()
    _write_package(package, "jsonc-mcp-test", "jsonc-server", "hello")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    # OpenCode accepts JSONC; APM cannot rewrite it without losing content.
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    jsonc = (
        "{\n"
        "  // personal settings\n"
        '  "model": "example/model",\n'
        '  "mcp": {"mine": {"type": "local", "command": ["mine"]}},\n'
        "}\n"
    )
    config.write_text(jsonc, encoding="utf-8")

    result = CliRunner().invoke(cli, ["install", "--global", str(package), "--target", "opencode"])

    assert result.exit_code != 0, result.output
    assert config.read_text(encoding="utf-8") == jsonc
