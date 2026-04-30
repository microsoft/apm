"""Unit tests for apm_cli.bundle.plugin_exporter."""

import json
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from apm_cli.bundle.plugin_exporter import (
    PackResult,  # noqa: F401
    _collect_apm_components,
    _collect_bare_skill,  # noqa: F401
    _collect_hooks_from_apm,
    _collect_hooks_from_root,
    _collect_mcp,
    _collect_root_plugin_components,
    _deep_merge,
    _get_dev_dependency_urls,
    _merge_file_map,
    _rename_prompt,
    _update_plugin_json_paths,
    _validate_output_rel,
    export_plugin_bundle,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_apm_yml(
    project: Path,
    *,
    name: str = "test-pkg",
    version: str = "1.0.0",
    extra: dict | None = None,
) -> Path:
    """Write a minimal apm.yml and return its path."""
    data = {"name": name, "version": version}
    if extra:
        data.update(extra)
    path = project / "apm.yml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _write_lockfile(
    project: Path,
    deps: list[LockedDependency] | None = None,
) -> Path:
    lockfile = LockFile()
    for d in deps or []:
        lockfile.add_dependency(d)
    lockfile.write(project / "apm.lock.yaml")
    return project / "apm.lock.yaml"


def _make_apm_dir(
    base: Path,
    *,
    agents: list[str] | None = None,
    skills: dict[str, list[str]] | None = None,
    prompts: list[str] | None = None,
    instructions: list[str] | None = None,
    commands: list[str] | None = None,
) -> Path:
    """Create a .apm/ directory tree under *base* with given component files."""
    apm = base / ".apm"
    apm.mkdir(parents=True, exist_ok=True)

    def _write_files(subdir, names):
        d = apm / subdir
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_text(f"content of {n}", encoding="utf-8")

    if agents:
        _write_files("agents", agents)
    if skills:
        for skill_name, files in skills.items():
            skill_dir = apm / "skills" / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            for fn in files:
                (skill_dir / fn).write_text(f"content of {fn}", encoding="utf-8")
    if prompts:
        _write_files("prompts", prompts)
    if instructions:
        _write_files("instructions", instructions)
    if commands:
        _write_files("commands", commands)
    return apm


def _setup_plugin_project(
    tmp_path: Path,
    *,
    deps: list[LockedDependency] | None = None,
    agents: list[str] | None = None,
    skills: dict[str, list[str]] | None = None,
    prompts: list[str] | None = None,
    instructions: list[str] | None = None,
    commands: list[str] | None = None,
    apm_yml_extra: dict | None = None,
    plugin_json: dict | None = None,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _write_apm_yml(project, extra=apm_yml_extra)
    _write_lockfile(project, deps)
    _make_apm_dir(
        project,
        agents=agents,
        skills=skills,
        prompts=prompts,
        instructions=instructions,
        commands=commands,
    )
    if plugin_json is not None:
        (project / "plugin.json").write_text(json.dumps(plugin_json), encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestValidateOutputRel:
    def test_valid_paths(self):
        assert _validate_output_rel("agents/a.md") is True
        assert _validate_output_rel("commands/deep/b.md") is True

    def test_rejects_traversal(self):
        assert _validate_output_rel("../escape.md") is False
        assert _validate_output_rel("agents/../../etc/passwd") is False

    def test_rejects_absolute_unix(self):
        assert _validate_output_rel("/etc/passwd") is False

    def test_rejects_absolute_windows(self):
        assert _validate_output_rel("C:\\Windows\\System32") is False


class TestRenamePrompt:
    def test_strips_prompt_infix(self):
        assert _rename_prompt("foo.prompt.md") == "foo.md"

    def test_preserves_plain_md(self):
        assert _rename_prompt("foo.md") == "foo.md"

    def test_preserves_non_md(self):
        assert _rename_prompt("foo.txt") == "foo.txt"


class TestDeepMerge:
    def test_first_wins_by_default(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"a": 99, "c": 3})
        assert base == {"a": 1, "b": 2, "c": 3}

    def test_overwrite_mode(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"a": 99, "c": 3}, overwrite=True)
        assert base == {"a": 99, "b": 2, "c": 3}

    def test_nested_first_wins(self):
        base = {"hooks": {"preCommit": "old"}}
        _deep_merge(base, {"hooks": {"preCommit": "new", "postCommit": "added"}})
        assert base == {"hooks": {"preCommit": "old", "postCommit": "added"}}

    def test_nested_overwrite(self):
        base = {"hooks": {"preCommit": "old"}}
        _deep_merge(
            base,
            {"hooks": {"preCommit": "new", "postCommit": "added"}},
            overwrite=True,
        )
        assert base == {"hooks": {"preCommit": "new", "postCommit": "added"}}

    def test_depth_limit_raises(self):
        """Deeply nested dicts beyond _MAX_MERGE_DEPTH raise ValueError."""
        from apm_cli.bundle.plugin_exporter import _MAX_MERGE_DEPTH

        # Build two dicts nested deeper than the limit with overlapping keys
        # so _deep_merge actually recurses on every level
        def _nested(depth: int) -> dict:
            d = {"leaf": True}
            for _ in range(depth):
                d = {"k": d}
            return d

        base = _nested(_MAX_MERGE_DEPTH + 5)
        overlay = _nested(_MAX_MERGE_DEPTH + 5)

        with pytest.raises(ValueError, match="maximum nesting depth"):
            _deep_merge(base, overlay)


# ---------------------------------------------------------------------------
# Unit tests: component collectors
# ---------------------------------------------------------------------------


class TestCollectApmComponents:
    def test_agents(self, tmp_path):
        _make_apm_dir(tmp_path, agents=["helper.agent.md"])
        comps = _collect_apm_components(tmp_path / ".apm")
        assert any(r == "agents/helper.agent.md" for _, r in comps)

    def test_skills_preserve_structure(self, tmp_path):
        _make_apm_dir(tmp_path, skills={"my-skill": ["SKILL.md", "lib.py"]})
        comps = _collect_apm_components(tmp_path / ".apm")
        rels = {r for _, r in comps}
        assert "skills/my-skill/SKILL.md" in rels
        assert "skills/my-skill/lib.py" in rels

    def test_prompts_rename(self, tmp_path):
        _make_apm_dir(tmp_path, prompts=["task.prompt.md", "plain.md"])
        comps = _collect_apm_components(tmp_path / ".apm")
        rels = {r for _, r in comps}
        assert "commands/task.md" in rels
        assert "commands/plain.md" in rels

    def test_instructions(self, tmp_path):
        _make_apm_dir(tmp_path, instructions=["rules.instructions.md"])
        comps = _collect_apm_components(tmp_path / ".apm")
        assert any(r == "instructions/rules.instructions.md" for _, r in comps)

    def test_commands_passthrough(self, tmp_path):
        _make_apm_dir(tmp_path, commands=["deploy.md"])
        comps = _collect_apm_components(tmp_path / ".apm")
        assert any(r == "commands/deploy.md" for _, r in comps)

    def test_empty_apm_dir(self, tmp_path):
        (tmp_path / ".apm").mkdir()
        comps = _collect_apm_components(tmp_path / ".apm")
        assert comps == []

    def test_missing_apm_dir(self, tmp_path):
        comps = _collect_apm_components(tmp_path / ".apm")
        assert comps == []

    def test_skips_symlinks(self, tmp_path):
        apm = _make_apm_dir(tmp_path, agents=["real.agent.md"])
        link = apm / "agents" / "link.agent.md"
        target = apm / "agents" / "real.agent.md"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlinks not supported")
        comps = _collect_apm_components(tmp_path / ".apm")
        rels = {r for _, r in comps}
        assert "agents/link.agent.md" not in rels
        assert "agents/real.agent.md" in rels


class TestCollectRootPluginComponents:
    def test_root_agents(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "bot.agent.md").write_text("x")
        comps = _collect_root_plugin_components(tmp_path)
        assert any(r == "agents/bot.agent.md" for _, r in comps)

    def test_ignores_nonexistent(self, tmp_path):
        comps = _collect_root_plugin_components(tmp_path)
        assert comps == []


class TestCollectBareSkill:
    """Tests for _collect_bare_skill — bare SKILL.md at dep root."""

    def test_bare_skill_detected(self, tmp_path):
        """A SKILL.md at root with no skills/ subdir is collected."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "SKILL.md").write_text("# My Skill")
        (tmp_path / "LICENSE.txt").write_text("MIT")
        dep = LockedDependency(
            repo_url="owner/my-skill",
            resolved_commit="abc123",
            depth=1,
        )
        out: list = []
        _collect_bare_skill(tmp_path, dep, out)
        rel_paths = [r for _, r in out]
        assert "skills/my-skill/SKILL.md" in rel_paths
        assert "skills/my-skill/LICENSE.txt" in rel_paths

    def test_virtual_path_used_as_slug(self, tmp_path):
        """virtual_path is preferred over repo_url for the skill slug."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "SKILL.md").write_text("# Frontend")
        dep = LockedDependency(
            repo_url="github/awesome-copilot",
            resolved_commit="abc123",
            depth=1,
            virtual_path="frontend-design",
            is_virtual=True,
        )
        out: list = []
        _collect_bare_skill(tmp_path, dep, out)
        assert any(r.startswith("skills/frontend-design/") for _, r in out)

    def test_skills_prefix_stripped_from_virtual_path(self, tmp_path):
        """A skills/ virtual path should not produce skills/skills/ nesting."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "SKILL.md").write_text("# Jest")
        dep = LockedDependency(
            repo_url="github/awesome-copilot",
            resolved_commit="abc123",
            depth=1,
            virtual_path="skills/javascript-typescript-jest",
            is_virtual=True,
        )
        out: list = []
        _collect_bare_skill(tmp_path, dep, out)
        rel_paths = [r for _, r in out]
        assert "skills/javascript-typescript-jest/SKILL.md" in rel_paths
        assert not any(r.startswith("skills/skills/") for r in rel_paths)

    def test_skips_when_no_skill_md(self, tmp_path):
        """No SKILL.md at root means nothing collected."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "README.md").write_text("hello")
        dep = LockedDependency(
            repo_url="owner/pkg",
            resolved_commit="abc",
            depth=1,
        )
        out: list = []
        _collect_bare_skill(tmp_path, dep, out)
        assert out == []

    def test_skips_when_skills_already_collected(self, tmp_path):
        """If skills/ was already collected via normal paths, bare skill is skipped."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "SKILL.md").write_text("# Root skill")
        dep = LockedDependency(
            repo_url="owner/pkg",
            resolved_commit="abc",
            depth=1,
        )
        out = [(tmp_path / "skills" / "sub" / "SKILL.md", "skills/sub/SKILL.md")]
        _collect_bare_skill(tmp_path, dep, out)
        # Should not add another entry
        assert len(out) == 1

    def test_excludes_apm_files(self, tmp_path):
        """apm.yml, apm.lock.yaml, plugin.json are excluded from bare skill output."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill  # noqa: F811

        (tmp_path / "SKILL.md").write_text("# Skill")
        (tmp_path / "apm.yml").write_text("name: x")
        (tmp_path / "plugin.json").write_text("{}")
        (tmp_path / "apm.lock.yaml").write_text("deps: []")
        dep = LockedDependency(
            repo_url="owner/pkg",
            resolved_commit="abc",
            depth=1,
        )
        out: list = []
        _collect_bare_skill(tmp_path, dep, out)
        rel_paths = [r for _, r in out]
        assert "skills/pkg/SKILL.md" in rel_paths
        assert not any("apm.yml" in r for r in rel_paths)
        assert not any("plugin.json" in r for r in rel_paths)
        assert not any("apm.lock.yaml" in r for r in rel_paths)


# ---------------------------------------------------------------------------
# Unit tests: hooks / MCP collection
# ---------------------------------------------------------------------------


class TestCollectHooks:
    def test_from_apm_hooks_dir(self, tmp_path):
        apm = tmp_path / ".apm"
        hooks_dir = apm / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "a.json").write_text(json.dumps({"preCommit": ["lint"]}))
        result = _collect_hooks_from_apm(apm)
        assert result == {"preCommit": ["lint"]}

    def test_from_root_hooks_json(self, tmp_path):
        (tmp_path / "hooks.json").write_text(json.dumps({"postPush": ["deploy"]}))
        result = _collect_hooks_from_root(tmp_path)
        assert result == {"postPush": ["deploy"]}

    def test_invalid_json_skipped(self, tmp_path):
        apm = tmp_path / ".apm"
        hooks_dir = apm / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "bad.json").write_text("not json")
        result = _collect_hooks_from_apm(apm)
        assert result == {}


class TestCollectMcp:
    def test_reads_mcp_servers(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"db": {"command": "db-server"}}})
        )
        result = _collect_mcp(tmp_path)
        assert result == {"db": {"command": "db-server"}}

    def test_missing_file(self, tmp_path):
        assert _collect_mcp(tmp_path) == {}


# ---------------------------------------------------------------------------
# Unit tests: devDependencies filtering
# ---------------------------------------------------------------------------


class TestDevDependencyUrls:
    def test_simple_list(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "1.0.0",
                    "devDependencies": {"apm": ["owner/dev-tool", "other/helper"]},
                }
            )
        )
        urls = _get_dev_dependency_urls(apm_yml)
        assert ("owner/dev-tool", "") in urls
        assert ("other/helper", "") in urls

    def test_virtual_path_preserved(self, tmp_path):
        """Deps from the same repo but different virtual paths are distinct."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "version": "1.0.0",
                    "devDependencies": {"apm": ["owner/repo/sub/dev-tool"]},
                }
            )
        )
        keys = _get_dev_dependency_urls(apm_yml)
        assert ("owner/repo", "sub/dev-tool") in keys
        # The bare repo should NOT match
        assert ("owner/repo", "") not in keys

    def test_no_dev_deps(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.dump({"name": "test", "version": "1.0.0"}))
        assert _get_dev_dependency_urls(apm_yml) == set()

    def test_missing_file(self, tmp_path):
        assert _get_dev_dependency_urls(tmp_path / "missing.yml") == set()


# ---------------------------------------------------------------------------
# Unit tests: collision handling
# ---------------------------------------------------------------------------


class TestMergeFileMap:
    def test_first_writer_wins_by_default(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("first")
        f2.write_text("second")
        file_map: dict = {}
        collisions: list = []
        _merge_file_map(file_map, [(f1, "agents/a.md")], "pkg-a", False, collisions)
        _merge_file_map(file_map, [(f2, "agents/a.md")], "pkg-b", False, collisions)
        assert file_map["agents/a.md"][0] == f1
        assert len(collisions) == 1
        assert "first writer wins" in collisions[0]

    def test_force_last_writer_wins(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("first")
        f2.write_text("second")
        file_map: dict = {}
        collisions: list = []
        _merge_file_map(file_map, [(f1, "agents/a.md")], "pkg-a", True, collisions)
        _merge_file_map(file_map, [(f2, "agents/a.md")], "pkg-b", True, collisions)
        assert file_map["agents/a.md"][0] == f2
        assert len(collisions) == 1
        assert "last writer wins" in collisions[0]


# ---------------------------------------------------------------------------
# Unit tests: plugin.json synthesis
# ---------------------------------------------------------------------------


class TestSynthesizePluginJson:
    def test_basic_synthesis(self, tmp_path):
        _write_apm_yml(tmp_path, extra={"description": "A tool", "author": "Alice"})
        result = synthesize_plugin_json_from_apm_yml(tmp_path / "apm.yml")
        assert result["name"] == "test-pkg"
        assert result["version"] == "1.0.0"
        assert result["description"] == "A tool"
        assert result["author"] == {"name": "Alice"}

    def test_missing_name_raises(self, tmp_path):
        (tmp_path / "apm.yml").write_text(yaml.dump({"version": "1.0.0"}))
        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(tmp_path / "apm.yml")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            synthesize_plugin_json_from_apm_yml(tmp_path / "nope.yml")

    def test_license_included(self, tmp_path):
        _write_apm_yml(tmp_path, extra={"license": "MIT"})
        result = synthesize_plugin_json_from_apm_yml(tmp_path / "apm.yml")
        assert result["license"] == "MIT"


# ---------------------------------------------------------------------------
# Unit tests: plugin.json path updating
# ---------------------------------------------------------------------------


class TestUpdatePluginJsonPaths:
    def test_strips_convention_dir_keys(self):
        """Convention dirs are auto-discovered; keys must be absent for schema validity."""
        pj = {"name": "test"}
        files = ["agents/a.md", "commands/b.md"]
        result = _update_plugin_json_paths(pj, files)
        assert "agents" not in result
        assert "commands" not in result
        assert "skills" not in result

    def test_strips_existing_invalid_keys(self):
        """Pre-existing invalid convention-dir entries are stripped."""
        pj = {"name": "test", "skills": ["skills/"], "agents": ["agents/"]}
        files = ["agents/a.md"]
        result = _update_plugin_json_paths(pj, files)
        assert "skills" not in result
        assert "agents" not in result
        assert result["name"] == "test"

    def test_warns_when_stripping_authored_keys(self):
        """When authored plugin.json has the keys, emit a warning naming what was stripped."""
        import logging

        pj = {"name": "test", "skills": ["skills/"], "agents": ["agents/"]}
        captured = []

        class _StubLogger:
            def warning(self, msg):
                captured.append(msg)

        _update_plugin_json_paths(pj, [], logger=_StubLogger())
        assert len(captured) == 1
        assert "Stripped schema-invalid keys" in captured[0]
        assert "skills" in captured[0]
        assert "agents" in captured[0]
        assert "auto-discovered" in captured[0]
        del logging  # silence unused

    def test_no_warning_when_no_authored_keys(self):
        """Synthesized manifests don't carry the keys; no warning to noise the user."""
        pj = {"name": "test"}
        captured = []

        class _StubLogger:
            def warning(self, msg):
                captured.append(msg)

        _update_plugin_json_paths(pj, [], logger=_StubLogger())
        assert captured == []


# ---------------------------------------------------------------------------
# Integration tests: export_plugin_bundle
# ---------------------------------------------------------------------------


class TestExportPluginBundle:
    def test_basic_export(self, tmp_path):
        project = _setup_plugin_project(
            tmp_path,
            agents=["helper.agent.md"],
            prompts=["task.prompt.md"],
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert result.bundle_path == out / "test-pkg-1.0.0"
        assert result.bundle_path.exists()
        assert (result.bundle_path / "agents" / "helper.agent.md").exists()
        assert (result.bundle_path / "commands" / "task.md").exists()
        assert (result.bundle_path / "plugin.json").exists()
        # No APM artifacts in output
        assert not (result.bundle_path / "apm.yml").exists()
        assert not (result.bundle_path / "apm.lock.yaml").exists()
        assert not (result.bundle_path / ".apm").exists()
        assert not (result.bundle_path / "apm_modules").exists()

    def test_uses_existing_plugin_json(self, tmp_path):
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            plugin_json={"name": "custom-name", "version": "2.0.0"},
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        pj = json.loads((result.bundle_path / "plugin.json").read_text())
        assert pj["name"] == "custom-name"
        assert pj["version"] == "2.0.0"

    def test_synthesizes_plugin_json_when_absent(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = export_plugin_bundle(project, out)

        pj = json.loads((result.bundle_path / "plugin.json").read_text())
        assert pj["name"] == "test-pkg"
        # Warning emitted about synthesis
        assert any("plugin.json" in str(c) for c in mock_warn.call_args_list)

    def test_prompt_md_rename(self, tmp_path):
        project = _setup_plugin_project(
            tmp_path,
            prompts=["do-thing.prompt.md", "plain.md"],
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert (result.bundle_path / "commands" / "do-thing.md").exists()
        assert (result.bundle_path / "commands" / "plain.md").exists()
        # The .prompt.md variant should NOT exist
        assert not (result.bundle_path / "commands" / "do-thing.prompt.md").exists()

    def test_skills_structure_preserved(self, tmp_path):
        project = _setup_plugin_project(
            tmp_path,
            skills={"my-skill": ["SKILL.md"]},
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        assert (result.bundle_path / "skills" / "my-skill" / "SKILL.md").exists()

    def test_dry_run_no_output(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = export_plugin_bundle(project, out, dry_run=True)

        assert not out.exists()
        assert len(result.files) > 0
        assert "plugin.json" in result.files

    def test_archive(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = export_plugin_bundle(project, out, archive=True)

        assert result.bundle_path.name == "test-pkg-1.0.0.tar.gz"
        assert result.bundle_path.exists()
        assert not (out / "test-pkg-1.0.0").exists()
        with tarfile.open(result.bundle_path, "r:gz") as tar:
            names = tar.getnames()
            assert any("agent.md" in n for n in names)

    def test_dependency_components_included(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])

        # Set up a dependency in apm_modules
        dep = LockedDependency(repo_url="acme/tools", depth=1)
        _write_lockfile(project, [dep])
        dep_path = project / "apm_modules" / "acme" / "tools"
        _make_apm_dir(dep_path, agents=["dep-agent.agent.md"])

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert (result.bundle_path / "agents" / "dep-agent.agent.md").exists()
        assert (result.bundle_path / "agents" / "own.agent.md").exists()

    def test_virtual_skill_dependency_does_not_duplicate_skills_dir(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        dep = LockedDependency(
            repo_url="github/awesome-copilot",
            depth=1,
            resolved_commit="abc123",
            virtual_path="skills/javascript-typescript-jest",
            is_virtual=True,
        )
        _write_lockfile(project, [dep])
        dep_path = (
            project
            / "apm_modules"
            / "github"
            / "awesome-copilot"
            / "skills"
            / "javascript-typescript-jest"
        )
        dep_path.mkdir(parents=True)
        (dep_path / "SKILL.md").write_text("# Jest", encoding="utf-8")

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert (result.bundle_path / "skills" / "javascript-typescript-jest" / "SKILL.md").exists()
        assert not (
            result.bundle_path / "skills" / "skills" / "javascript-typescript-jest" / "SKILL.md"
        ).exists()

    def test_dev_dependency_excluded(self, tmp_path):
        project = _setup_plugin_project(
            tmp_path,
            agents=["own.agent.md"],
            apm_yml_extra={"devDependencies": {"apm": ["acme/dev-only"]}},
        )

        dep = LockedDependency(repo_url="acme/dev-only", depth=1)
        _write_lockfile(project, [dep])
        dep_path = project / "apm_modules" / "acme" / "dev-only"
        _make_apm_dir(dep_path, agents=["dev-agent.agent.md"])

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert (result.bundle_path / "agents" / "own.agent.md").exists()
        assert not (result.bundle_path / "agents" / "dev-agent.agent.md").exists()

    def test_collision_first_wins(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        # Two deps with the same agent file
        dep1 = LockedDependency(repo_url="acme/first", depth=1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1)
        _write_lockfile(project, [dep1, dep2])

        dep1_path = project / "apm_modules" / "acme" / "first"
        _make_apm_dir(dep1_path, agents=["shared.agent.md"])
        dep2_path = project / "apm_modules" / "acme" / "second"
        _make_apm_dir(dep2_path, agents=["shared.agent.md"])

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = export_plugin_bundle(project, out)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert "shared.agent.md" in content  # From dep1

    def test_collision_force_last_wins(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        dep1 = LockedDependency(repo_url="acme/first", depth=1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1)
        _write_lockfile(project, [dep1, dep2])

        dep1_path = project / "apm_modules" / "acme" / "first"
        agents1 = dep1_path / ".apm" / "agents"
        agents1.mkdir(parents=True)
        (agents1 / "shared.agent.md").write_text("from-first")

        dep2_path = project / "apm_modules" / "acme" / "second"
        agents2 = dep2_path / ".apm" / "agents"
        agents2.mkdir(parents=True)
        (agents2 / "shared.agent.md").write_text("from-second")

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = export_plugin_bundle(project, out, force=True)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert content == "from-second"

    def test_hooks_merged(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        # Root hooks
        root_hooks_dir = project / ".apm" / "hooks"
        root_hooks_dir.mkdir(parents=True, exist_ok=True)
        (root_hooks_dir / "hooks.json").write_text(json.dumps({"preCommit": ["root-lint"]}))

        # Dep hooks
        dep = LockedDependency(repo_url="acme/hooks-pkg", depth=1)
        _write_lockfile(project, [dep])
        dep_path = project / "apm_modules" / "acme" / "hooks-pkg"
        dep_hooks_dir = dep_path / ".apm" / "hooks"
        dep_hooks_dir.mkdir(parents=True)
        (dep_hooks_dir / "hooks.json").write_text(
            json.dumps({"preCommit": ["dep-lint"], "postPush": ["deploy"]})
        )

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        hooks = json.loads((result.bundle_path / "hooks.json").read_text())
        # Root wins on key collision
        assert hooks["preCommit"] == ["root-lint"]
        # Dep-only key preserved
        assert hooks["postPush"] == ["deploy"]

    def test_mcp_merged(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        # Root MCP
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"root-db": {"command": "root-server"}}})
        )

        # Dep MCP
        dep = LockedDependency(repo_url="acme/mcp-pkg", depth=1)
        _write_lockfile(project, [dep])
        dep_path = project / "apm_modules" / "acme" / "mcp-pkg"
        dep_path.mkdir(parents=True)
        (dep_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "root-db": {"command": "dep-server"},
                        "dep-only": {"command": "extra"},
                    }
                }
            )
        )

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        mcp = json.loads((result.bundle_path / ".mcp.json").read_text())
        # Root wins on name collision
        assert mcp["mcpServers"]["root-db"]["command"] == "root-server"
        # Dep-only server preserved
        assert mcp["mcpServers"]["dep-only"]["command"] == "extra"

    def test_empty_project(self, tmp_path):
        project = _setup_plugin_project(tmp_path)
        out = tmp_path / "build"

        result = export_plugin_bundle(project, out)

        assert result.bundle_path.exists()
        assert (result.bundle_path / "plugin.json").exists()

    def test_no_lockfile_still_exports(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        _write_apm_yml(project)
        (project / ".apm").mkdir()

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert result.bundle_path.exists()
        assert (result.bundle_path / "plugin.json").exists()

    def test_security_scan_warns(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["sneaky.agent.md"])
        # Inject hidden Unicode
        sneaky = project / ".apm" / "agents" / "sneaky.agent.md"
        sneaky.write_text("Hello \U000e0001 world", encoding="utf-8")

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = export_plugin_bundle(project, out)

        assert result.bundle_path.exists()
        assert any("hidden character" in str(c) for c in mock_warn.call_args_list)

    def test_plugin_json_omits_convention_dir_keys(self, tmp_path):
        """plugin.json must NOT include convention-dir keys (schema requires
        ``./*.md`` paths for these arrays; convention dirs are auto-discovered)."""
        project = _setup_plugin_project(
            tmp_path,
            agents=["a.agent.md"],
            skills={"s1": ["SKILL.md"]},
            plugin_json={"name": "custom"},
        )
        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        pj = json.loads((result.bundle_path / "plugin.json").read_text())
        assert "agents" not in pj
        assert "skills" not in pj
        assert "commands" not in pj
        assert "instructions" not in pj
        # Files still land in convention dirs
        assert (result.bundle_path / "agents" / "a.agent.md").exists()
        assert (result.bundle_path / "skills" / "s1" / "SKILL.md").exists()

    def test_root_level_plugin_dirs_collected(self, tmp_path):
        """Root-level agents/ commands/ etc. are picked up for plugin-native repos."""
        project = _setup_plugin_project(tmp_path)
        # Create root-level agents dir (no .apm/)
        root_agents = project / "agents"
        root_agents.mkdir()
        (root_agents / "root-bot.agent.md").write_text("root bot")

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)
        assert (result.bundle_path / "agents" / "root-bot.agent.md").exists()


class TestExportPluginBundleViaPackBundle:
    """Verify pack_bundle(fmt='plugin') delegates correctly."""

    def test_fmt_plugin_delegates(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = pack_bundle(project, out, fmt="plugin")

        assert (result.bundle_path / "plugin.json").exists()
        assert (result.bundle_path / "agents" / "a.agent.md").exists()

    def test_force_flag_passed_through(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        project = _setup_plugin_project(tmp_path)
        dep1 = LockedDependency(repo_url="acme/first", depth=1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1)
        _write_lockfile(project, [dep1, dep2])

        dep1_path = project / "apm_modules" / "acme" / "first"
        a1 = dep1_path / ".apm" / "agents"
        a1.mkdir(parents=True)
        (a1 / "shared.agent.md").write_text("from-first")

        dep2_path = project / "apm_modules" / "acme" / "second"
        a2 = dep2_path / ".apm" / "agents"
        a2.mkdir(parents=True)
        (a2 / "shared.agent.md").write_text("from-second")

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = pack_bundle(project, out, fmt="plugin", force=True)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert content == "from-second"
