"""Unit tests for plugin_parser.py and find_plugin_json helper."""

import json
import os  # noqa: F401
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.plugin_parser import (
    _extract_mcp_servers,
    _generate_apm_yml,
    _map_plugin_artifacts,
    _mcp_servers_to_apm_deps,
    normalize_plugin_directory,
    parse_plugin_manifest,
    synthesize_apm_yml_from_plugin,
    validate_plugin_package,
)
from apm_cli.utils.helpers import find_plugin_json


class TestFindPluginJson:
    def test_find_plugin_json_root(self, tmp_path):
        pj = tmp_path / "plugin.json"
        pj.write_text('{"name": "root-plugin"}')

        result = find_plugin_json(tmp_path)
        assert result == pj

    def test_find_plugin_json_github_format(self, tmp_path):
        gh_dir = tmp_path / ".github" / "plugin"
        gh_dir.mkdir(parents=True)
        pj = gh_dir / "plugin.json"
        pj.write_text('{"name": "gh-plugin"}')

        result = find_plugin_json(tmp_path)
        assert result == pj

    def test_find_plugin_json_claude_format(self, tmp_path):
        claude_dir = tmp_path / ".claude-plugin"
        claude_dir.mkdir()
        pj = claude_dir / "plugin.json"
        pj.write_text('{"name": "claude-plugin"}')

        result = find_plugin_json(tmp_path)
        assert result == pj

    def test_find_plugin_json_priority_root_wins(self, tmp_path):
        root_pj = tmp_path / "plugin.json"
        root_pj.write_text('{"name": "root"}')

        gh_dir = tmp_path / ".github" / "plugin"
        gh_dir.mkdir(parents=True)
        (gh_dir / "plugin.json").write_text('{"name": "gh"}')

        result = find_plugin_json(tmp_path)
        assert result == root_pj

    def test_find_plugin_json_not_found(self, tmp_path):
        result = find_plugin_json(tmp_path)
        assert result is None

    def test_find_plugin_json_ignores_deep_nested(self, tmp_path):
        deep = tmp_path / "node_modules" / "some-pkg"
        deep.mkdir(parents=True)
        (deep / "plugin.json").write_text('{"name": "deep"}')

        result = find_plugin_json(tmp_path)
        assert result is None


class TestParsePluginManifest:
    def test_parse_valid_manifest(self, tmp_path):
        pj = tmp_path / "plugin.json"
        manifest = {
            "name": "test-plugin",
            "version": "1.2.3",
            "description": "A test plugin",
            "author": {"name": "Alice", "email": "a@b.c"},
            "license": "MIT",
            "tags": ["test", "demo"],
            "dependencies": {"dep-a": "^1.0.0"},
        }
        pj.write_text(json.dumps(manifest))

        result = parse_plugin_manifest(pj)
        assert result["name"] == "test-plugin"
        assert result["version"] == "1.2.3"
        assert result["author"]["name"] == "Alice"
        assert result["tags"] == ["test", "demo"]

    def test_parse_minimal_manifest(self, tmp_path):
        pj = tmp_path / "plugin.json"
        pj.write_text('{"name": "minimal"}')

        result = parse_plugin_manifest(pj)
        assert result == {"name": "minimal"}

    def test_parse_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_plugin_manifest(tmp_path / "nonexistent.json")

    def test_parse_invalid_json(self, tmp_path):
        pj = tmp_path / "plugin.json"
        pj.write_text("{ not valid json }")

        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_plugin_manifest(pj)


class TestMapPluginArtifacts:
    def test_map_agents_directory(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents = plugin_dir / "agents"
        agents.mkdir()
        (agents / "helper.agent.md").write_text("# Helper")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "agents" / "helper.agent.md").exists()
        assert (apm_dir / "agents" / "helper.agent.md").read_text() == "# Helper"

    def test_map_skills_directory(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skills = plugin_dir / "skills"
        skills.mkdir()
        skill_dir = skills / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Skill")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "skills" / "my-skill" / "SKILL.md").exists()

    def test_map_commands_to_prompts(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        commands = plugin_dir / "commands"
        commands.mkdir()
        (commands / "run.md").write_text("# Run")
        (commands / "already.prompt.md").write_text("# Already")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        prompts = apm_dir / "prompts"
        assert prompts.exists()
        # .md → .prompt.md rename
        assert (prompts / "run.prompt.md").exists()
        assert (prompts / "run.prompt.md").read_text() == "# Run"
        # Already .prompt.md stays unchanged
        assert (prompts / "already.prompt.md").exists()

    def test_map_hooks_directory(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        hooks = plugin_dir / "hooks"
        hooks.mkdir()
        (hooks / "pre-install.sh").write_text("#!/bin/sh\necho hi")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "hooks" / "pre-install.sh").exists()

    def test_map_mcp_json_passthrough(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        mcp_data = {"mcpServers": {"s": {"command": "node"}}}
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_data))

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        target = apm_dir / ".mcp.json"
        assert target.exists()
        assert json.loads(target.read_text()) == mcp_data

    def test_no_symlink_follow(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents = plugin_dir / "agents"
        agents.mkdir()
        (agents / "real.md").write_text("# Real")

        # Create a symlink inside agents/
        external = tmp_path / "external"
        external.mkdir()
        (external / "secret.md").write_text("# Secret")
        symlink_target = agents / "linked"
        try:
            symlink_target.symlink_to(external)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        # Real file is copied
        assert (apm_dir / "agents" / "real.md").exists()
        # _ignore_symlinks callback causes copytree to skip symlinks entirely
        copied_linked = apm_dir / "agents" / "linked"
        assert not copied_linked.exists(), (
            "Symlinked directory should be skipped entirely by _ignore_symlinks"
        )

    # ---- Custom component paths from plugin.json ----

    def test_custom_agents_path_string(self, tmp_path):
        """Manifest agents field as a string redirects agent discovery."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        custom = plugin_dir / "src" / "my-agents"
        custom.mkdir(parents=True)
        (custom / "bot.agent.md").write_text("# Bot")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"agents": "src/my-agents"})

        assert (apm_dir / "agents" / "bot.agent.md").exists()

    def test_custom_skills_path_array(self, tmp_path):
        """Manifest skills array preserves each directory as named component."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        s1 = plugin_dir / "skills"
        s1.mkdir()
        (s1 / "SKILL.md").write_text("# A")
        s2 = plugin_dir / "extra-skills"
        s2.mkdir()
        (s2 / "SKILL.md").write_text("# B")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"skills": ["skills/", "extra-skills/"]},
        )

        # Each array entry becomes a named subdirectory
        assert (apm_dir / "skills" / "skills" / "SKILL.md").read_text() == "# A"
        assert (apm_dir / "skills" / "extra-skills" / "SKILL.md").read_text() == "# B"

    def test_custom_commands_path(self, tmp_path):
        """Manifest commands field redirects command discovery."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        cmds = plugin_dir / "my-cmds"
        cmds.mkdir()
        (cmds / "deploy.md").write_text("# Deploy")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"commands": "my-cmds"})

        assert (apm_dir / "prompts" / "deploy.prompt.md").exists()

    def test_hooks_file_path(self, tmp_path):
        """Manifest hooks as a file path copies it to .apm/hooks/hooks.json."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        hooks_data = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "bash", "hooks": [{"type": "command", "command": "echo ok"}]}
                ]
            }
        }
        (plugin_dir / "my-hooks.json").write_text(json.dumps(hooks_data))

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"hooks": "my-hooks.json"})

        target = apm_dir / "hooks" / "hooks.json"
        assert target.exists()
        assert json.loads(target.read_text()) == hooks_data

    def test_hooks_inline_object(self, tmp_path):
        """Manifest hooks as an inline object writes .apm/hooks/hooks.json."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        hooks_obj = {
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo done"}]}]
            }
        }

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"hooks": hooks_obj})

        target = apm_dir / "hooks" / "hooks.json"
        assert target.exists()
        assert json.loads(target.read_text()) == hooks_obj

    def test_hooks_directory_path(self, tmp_path):
        """Manifest hooks as a custom directory path copies the directory."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        custom_hooks = plugin_dir / "my-hooks"
        custom_hooks.mkdir()
        (custom_hooks / "hooks.json").write_text('{"hooks": {}}')
        scripts = custom_hooks / "scripts"
        scripts.mkdir()
        (scripts / "lint.sh").write_text("#!/bin/sh\necho lint")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"hooks": "my-hooks"})

        assert (apm_dir / "hooks" / "hooks.json").exists()
        assert (apm_dir / "hooks" / "scripts" / "lint.sh").exists()

    def test_nonexistent_custom_path_ignored(self, tmp_path):
        """Custom paths that don't exist are silently ignored."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"agents": "does-not-exist/", "skills": ["also-missing/"]},
        )

        assert not (apm_dir / "agents").exists()
        assert not (apm_dir / "skills").exists()

    # ---- Individual file paths (not just directories) ----

    def test_agents_individual_file_paths(self, tmp_path):
        """Manifest agents as individual file paths copies each file."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "planner.md").write_text("# Planner")
        (agents_dir / "coder.md").write_text("# Coder")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"agents": ["./agents/planner.md", "./agents/coder.md"]},
        )

        assert (apm_dir / "agents" / "planner.md").read_text() == "# Planner"
        assert (apm_dir / "agents" / "coder.md").read_text() == "# Coder"

    def test_skills_individual_file_paths(self, tmp_path):
        """Manifest skills as individual file paths copies each file."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skill = plugin_dir / "my-skill.md"
        skill.write_text("# Skill")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"skills": ["my-skill.md"]},
        )

        assert (apm_dir / "skills" / "my-skill.md").read_text() == "# Skill"

    def test_commands_individual_file_paths(self, tmp_path):
        """Manifest commands as individual file paths; .md normalized to .prompt.md."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "deploy.md").write_text("# Deploy")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"commands": ["deploy.md"]},
        )

        assert (apm_dir / "prompts" / "deploy.prompt.md").read_text() == "# Deploy"

    def test_mixed_files_and_dirs(self, tmp_path):
        """Manifest mixing file and directory paths for same component."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "a.md").write_text("# A")
        (plugin_dir / "extra-agent.md").write_text("# Extra")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"agents": ["./agents", "extra-agent.md"]},
        )

        # Directory contents are flattened into .apm/agents/; file entry also flat
        assert (apm_dir / "agents" / "a.md").read_text() == "# A"
        assert (apm_dir / "agents" / "extra-agent.md").read_text() == "# Extra"

    def test_custom_agents_dir_list_flattens_contents(self, tmp_path):
        """Manifest agents as ["./agents"] must not produce .apm/agents/agents/ nesting.

        Regression test for the context-engineering plugin pattern where
        plugin.json declares: "agents": ["./agents"] and the directory contains
        plain .md files (not .agent.md).
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents = plugin_dir / "agents"
        agents.mkdir()
        (agents / "context-architect.md").write_text("# Context Architect")
        (agents / "planner.md").write_text("# Planner")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"agents": ["./agents"]},
        )

        # Files should be directly in .apm/agents/, NOT .apm/agents/agents/
        assert (apm_dir / "agents" / "context-architect.md").read_text() == "# Context Architect"
        assert (apm_dir / "agents" / "planner.md").read_text() == "# Planner"
        assert not (apm_dir / "agents" / "agents").exists(), (
            "Should not create nested agents/agents/ directory"
        )


class TestGenerateApmYml:
    def test_generate_full_metadata(self):
        manifest = {
            "name": "full-plugin",
            "version": "2.0.0",
            "description": "Full featured",
            "author": "Bob",
            "license": "Apache-2.0",
            "repository": "https://github.com/org/repo",
            "homepage": "https://example.com",
            "tags": ["ai", "copilot"],
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["name"] == "full-plugin"
        assert parsed["version"] == "2.0.0"
        assert parsed["description"] == "Full featured"
        assert parsed["author"] == "Bob"
        assert parsed["license"] == "Apache-2.0"
        assert parsed["tags"] == ["ai", "copilot"]
        assert parsed["type"] == "hybrid"

    def test_generate_minimal_metadata(self):
        manifest = {"name": "minimal"}

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["name"] == "minimal"
        assert parsed["version"] == "0.0.0"
        assert parsed["description"] == ""
        assert parsed["type"] == "hybrid"

    def test_generate_author_as_dict(self):
        manifest = {
            "name": "dict-author",
            "author": {"name": "Foo Bar", "email": "foo@bar.com"},
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["author"] == "Foo Bar"

    def test_generate_with_dependencies(self):
        manifest = {
            "name": "with-deps",
            "dependencies": {"dep-a": "^1.0", "dep-b": "~2.0"},
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["dependencies"] == {"apm": {"dep-a": "^1.0", "dep-b": "~2.0"}}


class TestNormalizePluginDirectory:
    def test_normalize_with_manifest(self, tmp_path):
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        pj = plugin_dir / "plugin.json"
        pj.write_text(json.dumps({"name": "My Plugin", "version": "1.0.0"}))
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "bot.md").write_text("# Bot")

        result = normalize_plugin_directory(plugin_dir, pj)

        assert result == plugin_dir / "apm.yml"
        assert result.exists()
        parsed = yaml.safe_load(result.read_text())
        assert parsed["name"] == "My Plugin"
        assert (plugin_dir / ".apm" / "agents" / "bot.md").exists()

    def test_normalize_without_manifest(self, tmp_path):
        plugin_dir = tmp_path / "dir-name-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "go.md").write_text("# Go")

        result = normalize_plugin_directory(plugin_dir, plugin_json_path=None)

        assert result.exists()
        parsed = yaml.safe_load(result.read_text())
        assert parsed["name"] == "dir-name-plugin"
        assert (plugin_dir / ".apm" / "prompts" / "go.prompt.md").exists()


class TestValidatePluginPackage:
    def test_validate_with_plugin_json(self, tmp_path):
        plugin_dir = tmp_path / "valid"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text('{"name": "valid-plugin"}')

        assert validate_plugin_package(plugin_dir) is True

    def test_validate_with_component_dirs_only(self, tmp_path):
        plugin_dir = tmp_path / "components"
        plugin_dir.mkdir()
        (plugin_dir / "agents").mkdir()

        assert validate_plugin_package(plugin_dir) is True

    def test_validate_empty_directory(self, tmp_path):
        plugin_dir = tmp_path / "empty"
        plugin_dir.mkdir()

        assert validate_plugin_package(plugin_dir) is False

    def test_validate_readme_only(self, tmp_path):
        plugin_dir = tmp_path / "readme-only"
        plugin_dir.mkdir()
        (plugin_dir / "README.md").write_text("# Hello")

        assert validate_plugin_package(plugin_dir) is False


class TestExtractMCPServers:
    """Tests for _extract_mcp_servers() — Phase 1, Step 1."""

    def test_mcpservers_inline_object(self, tmp_path):
        """Dict in manifest → extracted directly."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        manifest = {
            "name": "test",
            "mcpServers": {
                "my-server": {"command": "npx", "args": ["-y", "my-server"]},
            },
        }
        result = _extract_mcp_servers(plugin_dir, manifest)
        assert "my-server" in result
        assert result["my-server"]["command"] == "npx"

    def test_mcpservers_string_path(self, tmp_path):
        """File path → reads file, extracts servers."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        mcp_data = {"mcpServers": {"file-srv": {"command": "node", "args": ["index.js"]}}}
        (plugin_dir / "mcp-config.json").write_text(json.dumps(mcp_data))
        manifest = {"name": "test", "mcpServers": "mcp-config.json"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert "file-srv" in result
        assert result["file-srv"]["command"] == "node"

    def test_mcpservers_array_paths(self, tmp_path):
        """Multiple file paths → merges, last-wins."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        file1 = {"mcpServers": {"srv-a": {"command": "a"}, "srv-b": {"command": "b1"}}}
        file2 = {"mcpServers": {"srv-b": {"command": "b2"}, "srv-c": {"command": "c"}}}
        (plugin_dir / "mcp1.json").write_text(json.dumps(file1))
        (plugin_dir / "mcp2.json").write_text(json.dumps(file2))
        manifest = {"name": "test", "mcpServers": ["mcp1.json", "mcp2.json"]}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert result["srv-a"]["command"] == "a"
        assert result["srv-b"]["command"] == "b2"  # last-wins
        assert result["srv-c"]["command"] == "c"

    def test_default_mcp_json(self, tmp_path):
        """No mcpServers field, but .mcp.json exists → auto-discovered."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        mcp_data = {"mcpServers": {"default-srv": {"command": "echo"}}}
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_data))
        manifest = {"name": "test"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert "default-srv" in result

    def test_github_mcp_json_fallback(self, tmp_path):
        """No .mcp.json but .github/.mcp.json → discovered."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        gh_dir = plugin_dir / ".github"
        gh_dir.mkdir()
        mcp_data = {"mcpServers": {"gh-srv": {"url": "https://example.com"}}}
        (gh_dir / ".mcp.json").write_text(json.dumps(mcp_data))
        manifest = {"name": "test"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert "gh-srv" in result

    def test_manifest_wins_over_default(self, tmp_path):
        """mcpServers field takes precedence over .mcp.json file."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        # .mcp.json has different server
        mcp_data = {"mcpServers": {"file-srv": {"command": "from-file"}}}
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_data))
        manifest = {
            "name": "test",
            "mcpServers": {"inline-srv": {"command": "from-manifest"}},
        }

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert "inline-srv" in result
        assert "file-srv" not in result

    def test_missing_file_graceful(self, tmp_path):
        """String path pointing to nonexistent file → empty dict, warning."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        manifest = {"name": "test", "mcpServers": "does-not-exist.json"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert result == {}

    def test_symlink_skipped(self, tmp_path):
        """Symlinked file → skipped."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        external = tmp_path / "external.json"
        external.write_text(json.dumps({"mcpServers": {"evil": {"command": "evil"}}}))
        link = plugin_dir / "mcp.json"
        try:
            link.symlink_to(external)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")
        manifest = {"name": "test", "mcpServers": "mcp.json"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert result == {}

    def test_empty_manifest(self, tmp_path):
        """No mcpServers and no .mcp.json → empty dict."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        manifest = {"name": "test"}

        result = _extract_mcp_servers(plugin_dir, manifest)
        assert result == {}

    def test_plugin_root_substitution(self, tmp_path):
        """${CLAUDE_PLUGIN_ROOT} replaced with absolute plugin path."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        manifest = {
            "name": "test",
            "mcpServers": {
                "local-srv": {
                    "command": "node",
                    "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"],
                },
            },
        }

        result = _extract_mcp_servers(plugin_dir, manifest)
        abs_root = str(plugin_dir.resolve())
        assert result["local-srv"]["args"] == [f"{abs_root}/server.js"]


class TestMCPServersToDeps:
    """Tests for _mcp_servers_to_apm_deps() — Phase 1, Step 2."""

    def test_stdio_server(self, tmp_path):
        """command present → transport=stdio, registry=false."""
        servers = {"my-srv": {"command": "npx", "args": ["-y", "my-server"]}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["name"] == "my-srv"
        assert deps[0]["transport"] == "stdio"
        assert deps[0]["registry"] is False
        assert deps[0]["command"] == "npx"
        assert deps[0]["args"] == ["-y", "my-server"]

    def test_http_server(self, tmp_path):
        """url present → transport=http, registry=false."""
        servers = {"web-srv": {"url": "https://example.com/mcp"}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["name"] == "web-srv"
        assert deps[0]["transport"] == "http"
        assert deps[0]["registry"] is False
        assert deps[0]["url"] == "https://example.com/mcp"

    def test_mixed_servers(self, tmp_path):
        """Both stdio and http in one config."""
        servers = {
            "stdio-srv": {"command": "node", "args": ["index.js"]},
            "http-srv": {"url": "https://example.com"},
        }
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 2
        names = {d["name"] for d in deps}
        assert names == {"stdio-srv", "http-srv"}

    def test_env_and_args_passthrough(self, tmp_path):
        """env and args are passed through."""
        servers = {
            "srv": {
                "command": "cmd",
                "args": ["--flag"],
                "env": {"KEY": "VAL"},
            }
        }
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["env"] == {"KEY": "VAL"}
        assert deps[0]["args"] == ["--flag"]

    def test_invalid_server_skipped(self, tmp_path):
        """No command or url → skipped."""
        servers = {"bad-srv": {"env": {"KEY": "VAL"}}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 0

    def test_sse_type_preserved(self, tmp_path):
        """type field with valid transport is used."""
        servers = {"sse-srv": {"url": "https://sse.example.com", "type": "sse"}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["transport"] == "sse"

    def test_tools_passthrough(self, tmp_path):
        """tools field is passed through."""
        servers = {"srv": {"command": "cmd", "tools": ["tool1", "tool2"]}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["tools"] == ["tool1", "tool2"]

    def test_headers_passthrough(self, tmp_path):
        """headers field is passed through for http servers."""
        servers = {
            "srv": {
                "url": "https://example.com",
                "headers": {"Authorization": "Bearer token"},
            }
        }
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["headers"] == {"Authorization": "Bearer token"}


class TestGenerateApmYmlMCPDeps:
    """Test _mcp_deps injection in generated apm.yml."""

    def test_mcp_deps_in_generated_yml(self):
        """_mcp_deps in manifest → dependencies.mcp in output."""
        manifest = {
            "name": "mcp-plugin",
            "_mcp_deps": [
                {"name": "my-srv", "registry": False, "transport": "stdio", "command": "echo"},
            ],
        }
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "mcp" in parsed["dependencies"]
        assert len(parsed["dependencies"]["mcp"]) == 1
        assert parsed["dependencies"]["mcp"][0]["name"] == "my-srv"

    def test_mcp_deps_with_apm_deps(self):
        """Both apm and mcp deps coexist."""
        manifest = {
            "name": "both-plugin",
            "dependencies": {"dep-a": "^1.0"},
            "_mcp_deps": [
                {"name": "srv", "registry": False, "transport": "http", "url": "https://x"},
            ],
        }
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "apm" in parsed["dependencies"]
        assert "mcp" in parsed["dependencies"]

    def test_no_mcp_deps_no_section(self):
        """No _mcp_deps → no mcp key in dependencies."""
        manifest = {"name": "no-mcp"}
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "dependencies" not in parsed


class TestSynthesizeMCPIntegration:
    """End-to-end test: synthesize_apm_yml_from_plugin with MCP servers."""

    def test_synthesize_with_mcp_json(self, tmp_path):
        """Plugin with .mcp.json produces apm.yml with dependencies.mcp."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        mcp_data = {"mcpServers": {"test-srv": {"command": "echo", "args": ["hello"]}}}
        (plugin_dir / ".mcp.json").write_text(json.dumps(mcp_data))

        apm_yml = synthesize_apm_yml_from_plugin(plugin_dir, {"name": "test-plugin"})
        parsed = yaml.safe_load(apm_yml.read_text())

        assert "dependencies" in parsed
        assert "mcp" in parsed["dependencies"]
        mcp_deps = parsed["dependencies"]["mcp"]
        assert len(mcp_deps) == 1
        assert mcp_deps[0]["name"] == "test-srv"
        assert mcp_deps[0]["transport"] == "stdio"
        assert mcp_deps[0]["registry"] is False

    def test_synthesize_with_inline_mcpservers(self, tmp_path):
        """Plugin with inline mcpServers in manifest."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        manifest = {
            "name": "inline-mcp",
            "mcpServers": {
                "web-srv": {"url": "https://api.example.com"},
            },
        }

        apm_yml = synthesize_apm_yml_from_plugin(plugin_dir, manifest)
        parsed = yaml.safe_load(apm_yml.read_text())

        mcp_deps = parsed["dependencies"]["mcp"]
        assert len(mcp_deps) == 1
        assert mcp_deps[0]["name"] == "web-srv"
        assert mcp_deps[0]["transport"] == "http"


class TestPathTraversalProtection:
    """Regression tests for GHSA path-traversal advisory.

    A malicious plugin must not be able to use absolute paths or ``..``
    traversal in manifest fields (agents/skills/commands/hooks) to copy
    arbitrary host files into ``.apm/``.
    """

    def _make_outside_secret(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("# STOLEN VIA APM INSTALL\n")
        return outside

    def _make_plugin(self, tmp_path: Path) -> tuple[Path, Path]:
        plugin = tmp_path / "evil-plugin"
        plugin.mkdir()
        apm_dir = tmp_path / "victim" / ".apm"
        apm_dir.mkdir(parents=True)
        return plugin, apm_dir

    def test_commands_absolute_path_rejected(self, tmp_path):
        secret = self._make_outside_secret(tmp_path)
        plugin, apm_dir = self._make_plugin(tmp_path)
        manifest = {"name": "evil", "commands": str(secret)}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        prompts_dir = apm_dir / "prompts"
        assert not prompts_dir.exists() or not list(prompts_dir.iterdir()), (
            "Absolute commands path must not produce any prompts files"
        )

    def test_commands_traversal_path_rejected(self, tmp_path):
        self._make_outside_secret(tmp_path)
        plugin, apm_dir = self._make_plugin(tmp_path)
        manifest = {"name": "evil", "commands": "../outside/secret.md"}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        prompts_dir = apm_dir / "prompts"
        assert not prompts_dir.exists() or not list(prompts_dir.iterdir())

    def test_agents_traversal_in_list_rejected(self, tmp_path):
        outside_dir = tmp_path / "outside_agents"
        outside_dir.mkdir()
        (outside_dir / "evil.md").write_text("# evil")
        plugin, apm_dir = self._make_plugin(tmp_path)
        manifest = {"name": "evil", "agents": ["../outside_agents"]}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        agents_dir = apm_dir / "agents"
        assert not agents_dir.exists() or not list(agents_dir.iterdir())

    def test_skills_absolute_path_in_list_rejected(self, tmp_path):
        outside_skill = tmp_path / "outside_skills" / "leak"
        outside_skill.mkdir(parents=True)
        (outside_skill / "SKILL.md").write_text("# leak")
        plugin, apm_dir = self._make_plugin(tmp_path)
        manifest = {"name": "evil", "skills": [str(outside_skill)]}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        skills_dir = apm_dir / "skills"
        assert not skills_dir.exists() or not list(skills_dir.iterdir())

    def test_hooks_string_traversal_rejected(self, tmp_path):
        outside_hook = tmp_path / "outside" / "hooks.json"
        outside_hook.parent.mkdir(parents=True, exist_ok=True)
        outside_hook.write_text('{"hooks": {}}')
        plugin, apm_dir = self._make_plugin(tmp_path)
        manifest = {"name": "evil", "hooks": "../outside/hooks.json"}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        hooks_dir = apm_dir / "hooks"
        assert not hooks_dir.exists() or not list(hooks_dir.iterdir())

    def test_in_root_paths_still_accepted(self, tmp_path):
        """Sanity check: legitimate manifest paths must still work."""
        plugin, apm_dir = self._make_plugin(tmp_path)
        custom = plugin / "custom_cmds"
        custom.mkdir()
        (custom / "hello.md").write_text("# hello")
        manifest = {"name": "good", "commands": "custom_cmds"}

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        assert (apm_dir / "prompts" / "hello.prompt.md").read_text() == "# hello"

    def test_default_component_dir_as_symlink_rejected(self, tmp_path):
        """Default 'agents'/'skills'/etc dirs must be rejected if they're symlinks
        pointing outside the plugin root (no manifest override needed)."""
        outside = tmp_path / "outside_target"
        outside.mkdir()
        (outside / "leak.md").write_text("# leak")
        plugin, apm_dir = self._make_plugin(tmp_path)
        (plugin / "agents").symlink_to(outside, target_is_directory=True)
        manifest = {"name": "evil"}  # no custom paths -> default branch is taken

        _map_plugin_artifacts(plugin, apm_dir, manifest)

        agents_dir = apm_dir / "agents"
        assert not agents_dir.exists() or not list(agents_dir.iterdir()), (
            "Symlinked default component dir must not be copied"
        )
