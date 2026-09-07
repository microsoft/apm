"""Unit tests for plugin_parser.py and find_plugin_json helper."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from apm_cli.deps.plugin_parser import (
    PluginIntegrityError,
    _extract_mcp_servers,
    _generate_apm_yml,
    _holds_skill_dirs,
    _map_plugin_artifacts,
    _mcp_servers_to_apm_deps,
    _union_dep_list,
    has_normalized_plugin_skill_sources_receipt,
    normalize_plugin_directory,
    normalized_plugin_skill_sources,
    parse_plugin_manifest,
    synthesize_apm_yml_from_plugin,
    validate_plugin_package,
)
from apm_cli.utils.helpers import find_plugin_json


def test_union_dep_list_indexes_existing_entries_once() -> None:
    """Large dependency merges must not scan the growing result per append."""

    class NoContainsList(list):
        def __contains__(self, _item: object) -> bool:
            raise AssertionError("linear membership scan used")

    existing = NoContainsList(
        {"name": f"server-{index}", "command": "echo"} for index in range(100)
    )
    merged = {"mcp": existing}
    new_entries = [{"name": f"server-{index}", "command": "echo"} for index in range(100, 1000)]
    new_entries.extend(
        [
            {"name": "server-0", "command": "echo"},
            {"name": "server-0", "command": "different"},
        ]
    )

    _union_dep_list(merged, "mcp", new_entries)

    assert len(existing) == 1001
    assert existing[-1] == {"name": "server-0", "command": "different"}


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


class TestHoldsSkillDirs:
    """Direct coverage for the per-entry classifier behind #2530.

    Fixtures reach it through ``_map_plugin_artifacts``, which exercises the
    two healthy shapes -- a skill, and a container of them. The branches that
    say "neither" are the ones deciding whether unrecognized content merges
    into the shared skills root, and they were only ever reached transitively.
    Pin them here: anything a wrong answer lets through lands in
    ``.apm/skills/`` under no name of its own.
    """

    def test_own_skill_md_wins_over_a_nested_one(self, tmp_path):
        """Carrying ``SKILL.md`` settles it -- children are not consulted.

        Both shapes are present here, so this pins the precedence rather
        than restating either branch: merging such an entry would spill a
        bare ``SKILL.md`` into the shared skills root under no name at all.
        """
        skill = tmp_path / "engineering"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# engineering", encoding="utf-8")
        (skill / "tdd").mkdir()
        (skill / "tdd" / "SKILL.md").write_text("# tdd", encoding="utf-8")

        assert _holds_skill_dirs(skill) is False

    def test_directory_of_skill_directories_is_a_container(self, tmp_path):
        container = tmp_path / "skills"
        (container / "tdd").mkdir(parents=True)
        (container / "tdd" / "SKILL.md").write_text("# tdd", encoding="utf-8")

        assert _holds_skill_dirs(container) is True

    def test_empty_directory_is_not_a_container(self, tmp_path):
        empty = tmp_path / "skills"
        empty.mkdir()

        # ``any()`` over nothing is False, so an empty declared container
        # keeps its own name rather than merging -- there is nothing to
        # merge, and treating it as a container would be a guess.
        assert _holds_skill_dirs(empty) is False

    def test_directory_whose_skills_sit_two_levels_down_is_not_a_container(self, tmp_path):
        container = tmp_path / "skills"
        (container / "engineering" / "tdd").mkdir(parents=True)
        (container / "engineering" / "tdd" / "SKILL.md").write_text("# tdd", encoding="utf-8")

        assert _holds_skill_dirs(container) is False

    def test_missing_directory_is_not_a_container(self, tmp_path):
        # ``iterdir`` raises FileNotFoundError -- an OSError -- rather than
        # yielding nothing. A declared entry that vanished between
        # resolution and mapping must not be read as a merge instruction.
        assert _holds_skill_dirs(tmp_path / "gone") is False

    def test_unreadable_directory_is_not_a_container(self, tmp_path, monkeypatch):
        """A directory APM cannot list must fail closed, not merge blind.

        Permission errors are the shape this guards on POSIX; they are
        raised here directly because chmod is advisory for root and a no-op
        for directory listing on Windows.
        """
        unreadable = tmp_path / "skills"
        unreadable.mkdir()

        def _deny(self):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "iterdir", _deny)

        assert _holds_skill_dirs(unreadable) is False


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

    def test_skill_receipt_reconciles_removed_declarations(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        for name in ("alpha", "beta"):
            skill = plugin_dir / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        _map_plugin_artifacts(plugin_dir, apm_dir, {"skills": ["./skills/"]})
        assert set(normalized_plugin_skill_sources(plugin_dir)[0]) == {"alpha", "beta"}

        _map_plugin_artifacts(plugin_dir, apm_dir, {"skills": []})

        sources, declared = normalized_plugin_skill_sources(plugin_dir)
        assert sources == {}
        assert declared is True
        assert not (apm_dir / "skills" / "alpha").exists()
        assert not (apm_dir / "skills" / "beta").exists()

    def test_skill_receipt_presence_rejects_symlinked_paths(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir(parents=True)
        external = tmp_path / "external-receipt.json"
        external.write_text("{}", encoding="ascii")
        try:
            (apm_dir / ".plugin-skill-sources.json").symlink_to(external)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        assert not has_normalized_plugin_skill_sources_receipt(plugin_dir)

        (apm_dir / ".plugin-skill-sources.json").unlink()
        apm_dir.rmdir()
        external_apm = tmp_path / "external-apm"
        external_apm.mkdir()
        (external_apm / ".plugin-skill-sources.json").write_text("{}", encoding="ascii")
        apm_dir.symlink_to(external_apm, target_is_directory=True)

        assert not has_normalized_plugin_skill_sources_receipt(plugin_dir)

    def test_skill_receipt_removes_prior_skills_for_file_only_declaration(self, tmp_path):
        """A file-only declaration must remove prior parser-owned skill directories."""
        plugin_dir = tmp_path / "plugin"
        for name in ("alpha", "beta"):
            skill = plugin_dir / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        _map_plugin_artifacts(plugin_dir, apm_dir, {"skills": ["./skills/"]})
        (plugin_dir / "loose.md").write_text("# not a skill\n", encoding="utf-8")
        _map_plugin_artifacts(plugin_dir, apm_dir, {"skills": ["./loose.md"]})

        assert normalized_plugin_skill_sources(plugin_dir) == ({}, True)
        assert not (apm_dir / "skills" / "alpha").exists()
        assert not (apm_dir / "skills" / "beta").exists()

    def test_skill_receipt_preserves_nested_declared_source(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        skill = plugin_dir / "skills" / "engineering" / "tdd"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# tdd", encoding="utf-8")

        _map_plugin_artifacts(
            plugin_dir,
            plugin_dir / ".apm",
            {"skills": ["./skills/engineering/tdd"]},
        )

        sources, declared = normalized_plugin_skill_sources(plugin_dir)
        assert declared is True
        assert sources == {"tdd": skill}
        assert (plugin_dir / ".apm" / "skills" / "tdd" / "SKILL.md").is_file()

    def test_duplicate_declared_skill_leaf_names_fail_closed(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        for parent in ("engineering", "operations"):
            skill = plugin_dir / "skills" / parent / "runbook"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {parent}", encoding="utf-8")

        _map_plugin_artifacts(
            plugin_dir,
            plugin_dir / ".apm",
            {"skills": ["./skills/engineering/runbook", "./skills/operations/runbook"]},
        )

        sources, declared = normalized_plugin_skill_sources(plugin_dir)
        assert declared is True
        assert sources == {}

    def test_malformed_skills_declaration_disables_default_discovery(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        skill = plugin_dir / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# alpha", encoding="utf-8")

        _map_plugin_artifacts(plugin_dir, plugin_dir / ".apm", {"skills": {"path": "./skills"}})

        sources, declared = normalized_plugin_skill_sources(plugin_dir)
        assert declared is False
        assert sources == {}

    def test_skill_receipt_ignores_untracked_staged_normalized_skill(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        staged = plugin_dir / ".apm" / "skills" / "untracked"
        staged.mkdir(parents=True)
        (staged / "SKILL.md").write_text("# untracked", encoding="utf-8")

        _map_plugin_artifacts(plugin_dir, plugin_dir / ".apm", {"skills": []})

        assert normalized_plugin_skill_sources(plugin_dir) == ({}, True)

    def test_plugin_artifact_mapping_rejects_symlinked_apm_destination(self, tmp_path):
        """A plugin cannot redirect normalization cleanup through ``.apm``."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        external = tmp_path / "external"
        sentinel = external / "skills" / "sentinel"
        sentinel.mkdir(parents=True)
        (sentinel / "SKILL.md").write_text("# sentinel", encoding="utf-8")
        try:
            (plugin_dir / ".apm").symlink_to(external, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        with pytest.raises(PluginIntegrityError, match="symlinked destination"):
            _map_plugin_artifacts(plugin_dir, plugin_dir / ".apm", {"skills": []})

        assert (sentinel / "SKILL.md").is_file()

    def test_plugin_artifact_mapping_canonicalizes_a_symlinked_plugin_root(self, tmp_path):
        """A safe symlink to the plugin root still yields a valid receipt."""
        plugin_dir = tmp_path / "plugin"
        skill = plugin_dir / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# alpha", encoding="utf-8")
        alias = tmp_path / "plugin-alias"
        try:
            alias.symlink_to(plugin_dir, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        _map_plugin_artifacts(alias, alias / ".apm", {"skills": ["./skills/alpha"]})

        assert normalized_plugin_skill_sources(alias) == ({"alpha": skill}, True)

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

    def test_prepositioned_apm_command_source_is_preserved(self, tmp_path):
        """A declared command source under .apm is input, not generated output."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        apm_dir = plugin_dir / ".apm"
        source = apm_dir / "custom-commands"
        source.mkdir(parents=True)
        (source / "run.md").write_text("# Run")

        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"commands": [".apm/custom-commands"]},
        )

        assert (apm_dir / "prompts" / "run.prompt.md").read_text() == "# Run"

    def test_command_source_skips_fifo(self, tmp_path):
        """Command mapping must not block while opening a named pipe."""
        plugin_dir = tmp_path / "plugin"
        command_dir = plugin_dir / "commands"
        command_dir.mkdir(parents=True)
        fifo = command_dir / "wait"
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):
            pytest.skip("Named pipes are not supported on this platform")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"commands": "commands"})

        assert not (apm_dir / "prompts" / "wait").exists()

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

    def test_declared_skills_container_flattens_to_one_level(self, tmp_path):
        """A declared container merges its skills instead of nesting itself.

        Regression for #2530: ``"skills": ["./skills/"]`` names the
        conventional container, not a skill. Copying it under its own name
        buried every skill at ``.apm/skills/skills/<name>/`` -- one level
        below where deployment, ``--skill`` enumeration, the bin/ security
        scan and primitive counting all look.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        container = plugin_dir / "skills"
        for name in ("csharp-scripts", "dotnet-pinvoke"):
            skill = container / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"skills": ["./skills/"]})

        normalized = apm_dir / "skills"
        assert not (normalized / "skills").exists()
        assert (normalized / "csharp-scripts" / "SKILL.md").read_text() == "# csharp-scripts"
        assert (normalized / "dotnet-pinvoke" / "SKILL.md").read_text() == "# dotnet-pinvoke"

    def test_declared_skills_string_single_skill_keeps_leaf_name(self, tmp_path):
        """The string form classifies per entry too, not only the array form.

        ``"skills": "./skills/engineering/tdd"`` names one skill. Merging its
        contents would spill a bare ``SKILL.md`` into the shared skills root
        under no name, leaving ``--skill`` with nothing to match -- the #2530
        symptom reached through the other manifest shape.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skill = plugin_dir / "skills" / "engineering" / "tdd"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# tdd", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"skills": "./skills/engineering/tdd"},
        )

        normalized = apm_dir / "skills"
        assert (normalized / "tdd" / "SKILL.md").read_text() == "# tdd"
        assert not (normalized / "SKILL.md").exists()

    def test_declared_skills_string_container_flattens(self, tmp_path):
        """The string form of a container merges, same as the array form."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        for name in ("alpha", "beta"):
            skill = plugin_dir / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"skills": "./skills/"})

        normalized = apm_dir / "skills"
        assert not (normalized / "skills").exists()
        assert (normalized / "alpha" / "SKILL.md").read_text() == "# alpha"
        assert (normalized / "beta" / "SKILL.md").read_text() == "# beta"

    def test_declared_nested_skill_path_keeps_leaf_name(self, tmp_path):
        """A declared entry that IS a skill lands under its own leaf name."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skill = plugin_dir / "skills" / "engineering" / "tdd"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# tdd", encoding="utf-8")
        sibling = plugin_dir / "skills" / "engineering" / "pairing"
        sibling.mkdir(parents=True)
        (sibling / "SKILL.md").write_text("# pairing", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(
            plugin_dir,
            apm_dir,
            manifest={"skills": ["./skills/engineering/tdd"]},
        )

        normalized = apm_dir / "skills"
        assert (normalized / "tdd" / "SKILL.md").read_text() == "# tdd"
        # Undeclared siblings stay out: the entry is a requirement, not a hint.
        assert not (normalized / "pairing").exists()

    def test_declared_skills_entry_holding_no_skill_warns(self, tmp_path):
        """An entry that is neither a skill nor a container must say so.

        A container whose skills sit two levels down reaches no deployable
        depth under either mapping. The copy stays put -- the entry keeps its
        own name so unrecognized content stays out of the shared skills root
        -- but the plugin author gets the one line that #2530 lacked instead
        of an install that looks clean and deploys nothing.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        buried = plugin_dir / "skills" / "engineering" / "tdd"
        buried.mkdir(parents=True)
        (buried / "SKILL.md").write_text("# tdd", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        with patch("apm_cli.deps.plugin_parser._rich_warning") as rich_warning:
            _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"skills": ["./skills/"]})

        warning = rich_warning.call_args.args[0]
        assert "skills" in warning
        assert "no SKILL.md" in warning
        assert "--skill" in warning

    def test_declared_skills_container_does_not_warn(self, tmp_path):
        """The healthy shapes stay quiet -- a warning nobody can act on is noise."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skill = plugin_dir / "skills" / "csharp-scripts"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# csharp-scripts", encoding="utf-8")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        with patch("apm_cli.deps.plugin_parser._rich_warning") as rich_warning:
            _map_plugin_artifacts(plugin_dir, apm_dir, manifest={"skills": ["./skills/"]})

        rich_warning.assert_not_called()

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


class TestMapPluginArtifactsPrePositioned:
    """Regression: when plugin.json points to paths already inside .apm/,
    _map_plugin_artifacts must NOT destroy the source before copying.

    This reproduces the bug where APM packages with both apm.yml and
    .claude-plugin/plugin.json had their .apm/agents/ and .apm/skills/
    directories deleted during validate_apm_package -> normalize_plugin_directory.
    """

    def test_agents_inside_apm_are_preserved(self, tmp_path):
        """Manifest agents pointing into .apm/ must not be rmtree'd."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        # Pre-position agents inside .apm/ (APM package layout)
        apm_dir = plugin_dir / ".apm"
        agents_dir = apm_dir / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.agent.md").write_text("# Agent")

        # Manifest points into .apm/
        manifest = {"name": "test", "agents": [".apm/agents/my-agent.agent.md"]}
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        assert (agents_dir / "my-agent.agent.md").exists(), (
            ".apm/agents/ content destroyed by _map_plugin_artifacts"
        )
        assert (agents_dir / "my-agent.agent.md").read_text() == "# Agent"

    def test_skills_inside_apm_are_preserved(self, tmp_path):
        """Manifest skills pointing into .apm/ must not be rmtree'd."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        apm_dir = plugin_dir / ".apm"
        skill_dir = apm_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill")

        manifest = {"name": "test", "skills": [".apm/skills/my-skill"]}
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        assert (skill_dir / "SKILL.md").exists(), (
            ".apm/skills/ content destroyed by _map_plugin_artifacts"
        )
        assert (skill_dir / "SKILL.md").read_text() == "# Skill"

    def test_commands_inside_apm_are_preserved(self, tmp_path):
        """Manifest commands pointing into .apm/ must not be rmtree'd."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        apm_dir = plugin_dir / ".apm"
        prompts_dir = apm_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "run.prompt.md").write_text("# Run")

        manifest = {"name": "test", "commands": [".apm/prompts"]}
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        assert (prompts_dir / "run.prompt.md").exists(), (
            ".apm/prompts/ content destroyed by _map_plugin_artifacts"
        )

    def test_hooks_inside_apm_are_preserved(self, tmp_path):
        """Manifest hooks pointing into .apm/ must not be rmtree'd."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        apm_dir = plugin_dir / ".apm"
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-commit.json").write_text("{}")

        manifest = {"name": "test", "hooks": ".apm/hooks"}
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        assert (hooks_dir / "pre-commit.json").exists(), (
            ".apm/hooks/ content destroyed by _map_plugin_artifacts"
        )

    def test_hooks_config_file_inside_apm_is_preserved(self, tmp_path):
        """Manifest `hooks: ".apm/hooks/hooks.json"` (config-file form)
        must not raise SameFileError when src and dst are the same path."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        apm_dir = plugin_dir / ".apm"
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text('{"on": "pre-commit"}')

        manifest = {"name": "test", "hooks": ".apm/hooks/hooks.json"}
        # Must not raise SameFileError
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        assert (hooks_dir / "hooks.json").exists()
        assert (hooks_dir / "hooks.json").read_text() == '{"on": "pre-commit"}'

    def test_external_agents_still_copied(self, tmp_path):
        """Non-.apm/ agents must still be copied into .apm/ (no regression)."""
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        # Agents at root level (standard plugin layout)
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "helper.agent.md").write_text("# Helper")

        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "agents" / "helper.agent.md").exists()
        assert (apm_dir / "agents" / "helper.agent.md").read_text() == "# Helper"

    def test_mixed_inside_and_external_agents_both_survive(self, tmp_path):
        """Hybrid manifest mixing .apm/ paths and root-level paths:
        the pre-positioned .apm/ agent must NOT be destroyed, AND the
        external root-level agent must still be copied in.

        Regression for the per-source overlap case raised in PR #1416 review.
        """
        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        # Pre-positioned agent inside .apm/
        apm_dir = plugin_dir / ".apm"
        apm_agents = apm_dir / "agents"
        apm_agents.mkdir(parents=True)
        (apm_agents / "pre.agent.md").write_text("# Pre")

        # External agent at root level
        root_agents = plugin_dir / "agents"
        root_agents.mkdir()
        (root_agents / "new.agent.md").write_text("# New")

        manifest = {
            "name": "test",
            "agents": [".apm/agents/pre.agent.md", "agents/new.agent.md"],
        }
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest=manifest)

        # Pre-positioned survives
        assert (apm_agents / "pre.agent.md").exists(), (
            "pre-positioned .apm/ agent was destroyed in mixed-source case"
        )
        assert (apm_agents / "pre.agent.md").read_text() == "# Pre"

        # External got copied in
        assert (apm_agents / "new.agent.md").exists(), (
            "external root-level agent was not copied in the mixed-source case"
        )
        assert (apm_agents / "new.agent.md").read_text() == "# New"

    def test_dst_symlink_in_target_does_not_redirect_copy(self, tmp_path):
        """Defense-in-depth: a malicious package shipping a symlinked
        destination entry inside .apm/agents/ (or any target_*) must not
        let shutil.copytree(..., dirs_exist_ok=True) follow the link and
        write through it to an external sentinel path.

        Regression trap for the dst-symlink-write-anywhere follow-up on
        PR #1416. The pre-fix code dropped the unconditional rmtree but
        left existing dst symlinks unvalidated; copytree(dirs_exist_ok=
        True) would happily walk into a symlinked subdirectory.
        """
        # Sentinel external directory that MUST stay untouched.
        sentinel = tmp_path / "sentinel_external"
        sentinel.mkdir()
        (sentinel / "MARKER.md").write_text("# untouched-sentinel")

        plugin_dir = tmp_path / "pkg"
        plugin_dir.mkdir()

        # Package ships .apm/agents/<name> as a symlink pointing at the
        # sentinel external directory. This is exactly what the panel
        # called out: pre-existing dst symlinks left over from package
        # extraction.
        apm_dir = plugin_dir / ".apm"
        target_agents = apm_dir / "agents"
        target_agents.mkdir(parents=True)
        malicious_link = target_agents / "linked"
        try:
            malicious_link.symlink_to(sentinel, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        # Source agents at the standard layout, sharing the linked name
        # so copytree would naturally descend into the same subdir.
        agent_src = plugin_dir / "agents"
        nested_src = agent_src / "linked"
        nested_src.mkdir(parents=True)
        (nested_src / "evil.md").write_text("# evil-payload")

        with pytest.raises(PluginIntegrityError):
            _map_plugin_artifacts(plugin_dir, apm_dir)

        # The sentinel must be untouched: no evil.md should have been
        # written through the symlink.
        assert (sentinel / "MARKER.md").read_text() == "# untouched-sentinel"
        assert not (sentinel / "evil.md").exists(), (
            "copytree(dirs_exist_ok=True) followed a dst symlink and wrote outside the plugin root"
        )


# ------------------------------------------------------------------
# Issue #1666 -- synthesize_apm_yml_from_plugin must preserve existing
# apm.yml resolution-critical blocks when normalising a dual-format
# package (plugin.json + apm.yml).
# ------------------------------------------------------------------


class TestSynthesizePreservesExistingManifest:
    """Regression tests for #1666: manifest preservation during plugin normalisation."""

    def _write_apm_yml(self, pkg_dir: Path, data: dict) -> Path:
        """Helper: write an apm.yml from a dict."""
        apm_yml = pkg_dir / "apm.yml"
        apm_yml.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        return apm_yml

    def _write_plugin_json(self, pkg_dir: Path, data: dict) -> Path:
        """Helper: write a plugin.json from a dict."""
        pj = pkg_dir / "plugin.json"
        pj.write_text(json.dumps(data))
        return pj

    def _read_apm_yml(self, pkg_dir: Path) -> dict:
        """Helper: read the apm.yml back as a dict."""
        return yaml.safe_load((pkg_dir / "apm.yml").read_text()) or {}

    def test_preserves_existing_apm_dependencies(self, tmp_path):
        """Existing dependencies.apm and dependencies.mcp must survive synthesis."""
        self._write_apm_yml(
            tmp_path,
            {
                "name": "my-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "apm": [
                        "vercel-labs/agent-skills/skills/web-design-guidelines",
                        {"git": "parent", "path": "packages/stack-js"},
                    ],
                    "mcp": ["io.github.ChromeDevTools/chrome-devtools-mcp"],
                },
            },
        )
        self._write_plugin_json(tmp_path, {"name": "my-pkg", "version": "1.0.0"})

        synthesize_apm_yml_from_plugin(tmp_path, {"name": "my-pkg", "version": "1.0.0"})

        result = self._read_apm_yml(tmp_path)
        deps = result.get("dependencies", {})
        assert "apm" in deps, "dependencies.apm was dropped"
        assert len(deps["apm"]) == 2, f"Expected 2 apm deps, got {len(deps['apm'])}"
        assert "mcp" in deps, "dependencies.mcp was dropped"
        assert len(deps["mcp"]) == 1

    def test_preserves_existing_apm_deps_loadable(self, tmp_path):
        """After synthesis the manifest must be parseable by APMPackage."""
        from apm_cli.models.apm_package import APMPackage

        self._write_apm_yml(
            tmp_path,
            {
                "name": "loadable-pkg",
                "version": "2.0.0",
                "dependencies": {
                    "apm": ["owner/repo-a", "owner/repo-b"],
                },
            },
        )
        self._write_plugin_json(tmp_path, {"name": "loadable-pkg"})

        synthesize_apm_yml_from_plugin(tmp_path, {"name": "loadable-pkg"})

        pkg = APMPackage.from_apm_yml(tmp_path / "apm.yml")
        apm_deps = pkg.get_apm_dependencies()
        assert len(apm_deps) == 2, f"Expected 2 parsed apm deps, got {len(apm_deps)}"

    def test_merges_plugin_mcp_with_existing_deps(self, tmp_path):
        """Plugin-derived MCP deps are unioned with existing apm.yml deps."""
        self._write_apm_yml(
            tmp_path,
            {
                "name": "merge-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "apm": ["foo/bar"],
                    "mcp": ["existing-mcp-server"],
                },
            },
        )
        manifest = {"name": "merge-pkg", "_mcp_deps": ["plugin-mcp-server"]}

        synthesize_apm_yml_from_plugin(tmp_path, manifest)

        result = self._read_apm_yml(tmp_path)
        deps = result.get("dependencies", {})
        apm_deps = deps.get("apm", [])
        mcp_deps = deps.get("mcp", [])
        assert "foo/bar" in apm_deps, "Existing apm dep was dropped"
        assert "existing-mcp-server" in mcp_deps, "Existing mcp dep was dropped"
        assert "plugin-mcp-server" in mcp_deps, "Plugin mcp dep was not added"

    def test_no_existing_apm_yml_unchanged_behaviour(self, tmp_path):
        """Without a pre-existing apm.yml, behaviour is unchanged (no regression)."""
        manifest = {
            "name": "new-pkg",
            "version": "0.1.0",
            "description": "Fresh plugin",
            "_mcp_deps": ["some-mcp"],
        }

        synthesize_apm_yml_from_plugin(tmp_path, manifest)

        result = self._read_apm_yml(tmp_path)
        assert result["name"] == "new-pkg"
        assert result.get("dependencies", {}).get("mcp") == ["some-mcp"]

    def test_preserves_dev_dependencies(self, tmp_path):
        """devDependencies from existing apm.yml must survive synthesis."""
        self._write_apm_yml(
            tmp_path,
            {
                "name": "dev-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": ["test-utils/helpers"],
                },
            },
        )

        synthesize_apm_yml_from_plugin(tmp_path, {"name": "dev-pkg"})

        result = self._read_apm_yml(tmp_path)
        assert "devDependencies" in result, "devDependencies was dropped"
        assert result["devDependencies"]["apm"] == ["test-utils/helpers"]

    def test_preserves_registries_targets_scripts(self, tmp_path):
        """Resolution-critical blocks (registries, targets, scripts) survive."""
        self._write_apm_yml(
            tmp_path,
            {
                "name": "full-pkg",
                "version": "1.0.0",
                "registries": {"my-reg": {"url": "https://example.com"}},
                "targets": ["claude", "copilot"],
                "scripts": {"postinstall": "echo done"},
                "includes": {"patterns": ["*.md"]},
            },
        )

        synthesize_apm_yml_from_plugin(tmp_path, {"name": "full-pkg"})

        result = self._read_apm_yml(tmp_path)
        assert "registries" in result, "registries was dropped"
        assert "targets" in result, "targets was dropped"
        assert "scripts" in result, "scripts was dropped"
        assert "includes" in result, "includes was dropped"

    def test_claude_plugin_dir_without_plugin_json_preserves_deps(self, tmp_path):
        """A .claude-plugin/ dir (no plugin.json) must not strip apm.yml deps."""
        self._write_apm_yml(
            tmp_path,
            {
                "name": "claude-dir-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "apm": ["org/dep-a", "org/dep-b"],
                },
            },
        )
        # Create .claude-plugin directory (no plugin.json inside)
        (tmp_path / ".claude-plugin").mkdir()

        # This triggers MARKETPLACE_PLUGIN classification
        normalize_plugin_directory(tmp_path, plugin_json_path=None)

        result = self._read_apm_yml(tmp_path)
        deps = result.get("dependencies", {})
        assert "apm" in deps, "dependencies.apm was dropped with .claude-plugin/ dir"
        assert len(deps["apm"]) == 2

    def test_validate_apm_package_dual_format_preserves_deps(self, tmp_path):
        """Full chain: validate_apm_package on a dual-format package keeps deps."""
        from apm_cli.models.validation import validate_apm_package

        self._write_apm_yml(
            tmp_path,
            {
                "name": "dual-pkg",
                "version": "1.0.0",
                "description": "Dual-format package",
                "dependencies": {
                    "apm": ["owner/transitive-dep"],
                    "mcp": ["some-mcp-server"],
                },
            },
        )
        self._write_plugin_json(
            tmp_path,
            {"name": "dual-pkg", "version": "1.0.0", "description": "Dual-format"},
        )

        result = validate_apm_package(tmp_path)
        assert result.is_valid, f"Validation failed: {result.errors}"
        assert result.package is not None

        apm_deps = result.package.get_apm_dependencies()
        assert len(apm_deps) >= 1, (
            f"Transitive deps lost after validation; got {len(apm_deps)} apm deps"
        )

    def test_malformed_apm_yml_fallback_surfaces_warning(self, tmp_path):
        """A malformed existing apm.yml must not fail silently (#1666 trap).

        When the existing apm.yml cannot be parsed, synthesis falls back to
        plugin-only metadata -- which drops any transitive deps the file may
        have declared. That data loss must be surfaced to the user via
        ``_surface_warning`` rather than swallowed, otherwise the malformed
        file re-creates the exact #1666 symptom with zero diagnostic output.
        """
        from unittest.mock import patch

        # Write syntactically invalid YAML (unbalanced bracket triggers a
        # yaml.YAMLError inside load_yaml).
        (tmp_path / "apm.yml").write_text("name: bad\ndependencies: [unterminated\n")
        self._write_plugin_json(tmp_path, {"name": "bad-pkg", "version": "1.0.0"})

        with patch("apm_cli.deps.plugin_parser._surface_warning") as mock_warn:
            synthesize_apm_yml_from_plugin(tmp_path, {"name": "bad-pkg", "version": "1.0.0"})

        assert mock_warn.called, "Malformed apm.yml fallback did not surface a warning"
        warning_text = " ".join(str(call.args[0]) for call in mock_warn.call_args_list)
        assert "apm.yml" in warning_text
        assert "transitive" in warning_text.lower()

        # Fallback still produces a usable apm.yml from plugin metadata.
        result = self._read_apm_yml(tmp_path)
        assert result["name"] == "bad-pkg"


class TestRootDeclaredComponents:
    """Root declarations must copy only source content into the component tree."""

    @staticmethod
    def _plugin(tmp_path, component: str) -> tuple[Path, bool]:
        plugin_dir = tmp_path / "plug"
        plugin_dir.mkdir()
        (plugin_dir / ".claude-plugin").mkdir()
        (plugin_dir / "SKILL.md").write_text("# hello\n", encoding="utf-8")
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "plug", component: ["./"]}),
            encoding="utf-8",
        )
        (plugin_dir / ".apm-pin").write_text("internal cache marker\n", encoding="utf-8")
        linked = plugin_dir / "linked.md"
        try:
            linked.symlink_to(plugin_dir / "SKILL.md")
        except OSError:
            return plugin_dir, False
        return plugin_dir, True

    @pytest.mark.parametrize(
        ("component", "expected_files"),
        [
            ("agents", {"SKILL.md", ".claude-plugin/plugin.json"}),
            ("skills", {"SKILL.md", ".claude-plugin/plugin.json"}),
            ("commands", {"SKILL.prompt.md", ".claude-plugin/plugin.json"}),
            ("hooks", {"SKILL.md", ".claude-plugin/plugin.json"}),
        ],
    )
    def test_root_component_excludes_internal_and_symlinked_content(
        self,
        tmp_path,
        component: str,
        expected_files: set[str],
    ) -> None:
        """A root declaration has a stable, finite deployment tree."""
        plugin_dir, has_symlink = self._plugin(tmp_path, component)
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()

        _map_plugin_artifacts(plugin_dir, apm_dir, {"name": "plug", component: ["./"]})

        component_root = (
            apm_dir
            / {
                "agents": "agents",
                "skills": "skills/plug",
                "commands": "prompts",
                "hooks": "hooks",
            }[component]
        )
        deployed_files = {
            path.relative_to(component_root).as_posix()
            for path in component_root.rglob("*")
            if path.is_file()
        }
        assert deployed_files == expected_files
        assert not list(component_root.rglob(".apm"))
        assert not list(component_root.rglob(".apm-pin"))
        if has_symlink:
            assert "linked.md" not in deployed_files

    def test_root_declared_skills_are_idempotent(self, tmp_path) -> None:
        """Re-materializing a root declaration preserves an identical tree."""
        plugin_dir, _ = self._plugin(tmp_path, "skills")
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        manifest = {"name": "plug", "skills": ["./"]}

        _map_plugin_artifacts(plugin_dir, apm_dir, manifest)
        first_tree = {
            path.relative_to(apm_dir).as_posix(): path.read_bytes()
            for path in apm_dir.rglob("*")
            if path.is_file()
        }
        _map_plugin_artifacts(plugin_dir, apm_dir, manifest)
        second_tree = {
            path.relative_to(apm_dir).as_posix(): path.read_bytes()
            for path in apm_dir.rglob("*")
            if path.is_file()
        }

        assert second_tree == first_tree


@pytest.mark.windows_compat
class TestSyntheticManifestLineEndings:
    """apm#2619: synthetic apm.yml bytes must be platform-invariant (LF).

    The synthesized manifest is written by APM itself (not checked out by
    git) into a package tree that ``compute_package_hash`` hashes raw. A
    platform-native text-mode write (CRLF on Windows) made the lockfile
    ``content_hash`` diverge across OSes for byte-identical upstream
    content. Same bug class as #2187 / PR #2223, which only covered
    ``download_virtual_file_package``.
    """

    def test_synthesized_apm_yml_is_lf_only(self, tmp_path):
        plugin = tmp_path / "plug"
        skill = plugin / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(
            b"---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n"
        )

        apm_yml_path = synthesize_apm_yml_from_plugin(plugin, {"name": "plug"})

        raw = apm_yml_path.read_bytes()
        assert b"\r" not in raw
        assert raw.endswith(b"\n")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
    def test_synthesized_apm_yml_preserves_existing_mode(self, tmp_path):
        """Rewriting a dual-format manifest must not replace its POSIX mode."""
        plugin = tmp_path / "plug"
        plugin.mkdir()
        apm_yml = plugin / "apm.yml"
        apm_yml.write_text("name: plug\nversion: 0.0.0\n", encoding="utf-8")
        apm_yml.chmod(0o644)

        synthesize_apm_yml_from_plugin(plugin, {"name": "plug"})

        assert apm_yml.stat().st_mode & 0o777 == 0o644

    def test_inline_hooks_json_is_lf_only(self, tmp_path):
        """Inline plugin.json hooks are serialized into the hashed tree as
        .apm/hooks/hooks.json -- the bytes must be LF-only and UTF-8."""
        plugin = tmp_path / "plug"
        plugin.mkdir()
        apm_dir = plugin / ".apm"
        apm_dir.mkdir()
        hooks = {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
            ]
        }

        _map_plugin_artifacts(plugin, apm_dir, {"name": "plug", "hooks": hooks})

        raw = (apm_dir / "hooks" / "hooks.json").read_bytes()
        assert b"\r" not in raw
        assert json.loads(raw.decode("utf-8")) == hooks
