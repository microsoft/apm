"""Unit tests for apm_cli.bundle.plugin_exporter."""

import json
import os
import zipfile
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
    _deployed_path_parts,
    _get_dev_dependency_urls,
    _merge_file_map,
    _plugin_rel_for_deployed_path,
    _rename_prompt,
    _update_plugin_json_paths,
    _validate_output_rel,
    export_plugin_bundle,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml
from apm_cli.utils.content_hash import compute_file_hash

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


def _write_deployed_skill(project: Path, name: str, marker: str) -> list[str]:
    """Create a deployed skill under the project and return lockfile entries."""
    skill_dir = project / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(marker, encoding="utf-8")
    (skill_dir / "notes.md").write_text(f"{marker} notes", encoding="utf-8")
    return [
        skill_dir.relative_to(project).as_posix(),
        (skill_dir / "SKILL.md").relative_to(project).as_posix(),
        (skill_dir / "notes.md").relative_to(project).as_posix(),
    ]


def _write_deployed_agent(project: Path, filename: str, marker: str) -> list[str]:
    """Create a deployed agent file under the project and return lockfile entries."""
    agents_dir = project / ".agents" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / filename
    agent_file.write_text(marker, encoding="utf-8")
    return [agent_file.relative_to(project).as_posix()]


def _write_deployed_agent_at(project: Path, target: str, filename: str, content: str) -> list[str]:
    """Deploy an agent under a specific target dir (e.g. ``.github``/``.claude``).

    Different targets map to the same plugin-native ``agents/<filename>`` output,
    which lets two dependencies collide on one bundle path while each has its
    own attested on-disk deployed file.
    """
    agents_dir = project / target / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / filename
    agent_file.write_text(content, encoding="utf-8")
    return [agent_file.relative_to(project).as_posix()]


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


class TestDeployedPathParts:
    def test_normalizes_backslashes_to_posix_parts(self):
        assert _deployed_path_parts(".agents\\skills\\alpha\\SKILL.md") == (
            ".agents",
            "skills",
            "alpha",
            "SKILL.md",
        )

    def test_rejects_absolute_posix_path(self):
        with pytest.raises(ValueError, match=r"absolute deployed file path"):
            _deployed_path_parts("/etc/passwd")

    def test_rejects_absolute_windows_path(self):
        with pytest.raises(ValueError, match=r"absolute deployed file path"):
            _deployed_path_parts("C:\\Windows\\System32\\cmd.exe")

    def test_rejects_traversal_path(self):
        with pytest.raises(ValueError, match=r"unsafe deployed file path"):
            _deployed_path_parts(".agents/skills/../../escape")


class TestPluginRelForDeployedPath:
    def test_top_level_skills_honor_subset(self):
        dep = LockedDependency(
            repo_url="acme/skill-bundle",
            depth=1,
            package_type="skill_bundle",
            skill_subset=["alpha"],
        )
        skill_subset = set(dep.skill_subset)

        assert _plugin_rel_for_deployed_path("skills/alpha/SKILL.md", skill_subset) == (
            "skills/alpha/SKILL.md"
        )
        assert _plugin_rel_for_deployed_path("skills/gamma/SKILL.md", skill_subset) is None

    @pytest.mark.parametrize(
        ("deployed_path", "expected"),
        [
            (".agents/skills/alpha/SKILL.md", "skills/alpha/SKILL.md"),
            (".agents/agents/helper.agent.md", "agents/helper.agent.md"),
            (".agents/prompts/do-thing.prompt.md", "commands/do-thing.md"),
            (".github/instructions/team.instructions.md", "instructions/team.instructions.md"),
            (".claude/hooks/pre-commit.sh", "hooks/pre-commit.sh"),
            (".codex/extensions/acme/manifest.json", "extensions/acme/manifest.json"),
            (".claude/hooks.json", "hooks.json"),
        ],
    )
    def test_target_deployed_paths_map_to_plugin_layout(
        self,
        deployed_path: str,
        expected: str,
    ) -> None:
        assert _plugin_rel_for_deployed_path(deployed_path, {"alpha"}) == expected

    def test_target_deployed_skills_honor_subset(self) -> None:
        assert _plugin_rel_for_deployed_path(".agents/skills/gamma/SKILL.md", {"alpha"}) is None


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
        # No APM source artifacts in output (the bundle now embeds an
        # enriched apm.lock.yaml with the per-file SHA-256 manifest -- see
        # issue #1098 -- so apm.lock.yaml IS expected at bundle root.)
        assert not (result.bundle_path / "apm.yml").exists()
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
        """G2: bare plugin shape (no marketplace block) gets an [i] info,
        no longer a yellow warning, because synthesis from apm.yml is the
        APM-native happy path."""
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        with (
            patch("apm_cli.core.plugin_manifest._rich_info") as mock_info,
            patch("apm_cli.core.plugin_manifest._rich_warning") as mock_warn,
        ):
            result = export_plugin_bundle(project, out)

        pj = json.loads((result.bundle_path / "plugin.json").read_text())
        assert pj["name"] == "test-pkg"
        # Info emitted about synthesis (demoted from warning per #1348 G2)
        assert any("apm.yml" in str(c) for c in mock_info.call_args_list)
        # No misleading "consider running apm init --plugin" warning
        assert not any("apm init --plugin" in str(c) for c in mock_warn.call_args_list)

    def test_synthesis_info_suppressed_when_marketplace_block_present(self, tmp_path):
        """G2: marketplace-publishing project (apm.yml has marketplace:)
        SHOULD NOT emit the synthesis info, because plugin.json is not
        the audience here -- the marketplace artifacts are."""
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        # Append a marketplace block to apm.yml
        apm_yml = project / "apm.yml"
        apm_yml.write_text(
            apm_yml.read_text()
            + "\nmarketplace:\n  name: m\n  description: d\n  version: 0.1.0\n"
            + "  owner: {name: acme}\n  packages: [{name: p, source: acme/p, version: '^1.0.0'}]\n"
        )
        out = tmp_path / "build"

        with patch("apm_cli.core.plugin_manifest._rich_info") as mock_info:
            export_plugin_bundle(project, out)

        assert not any("deriving it from apm.yml" in str(c) for c in mock_info.call_args_list), (
            "marketplace-block project should not see plugin.json synthesis chatter"
        )

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

    def test_archive_dry_run_reports_projected_zip_path(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = export_plugin_bundle(project, out, archive=True, dry_run=True)

        assert result.bundle_path == out / "test-pkg-1.0.0.zip"
        assert not out.exists()

    def test_archive_dry_run_reports_projected_tar_gz_path(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = export_plugin_bundle(
            project,
            out,
            archive=True,
            archive_format="tar.gz",
            dry_run=True,
        )

        assert result.bundle_path == out / "test-pkg-1.0.0.tar.gz"
        assert not out.exists()

    def test_archive(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["a.agent.md"])
        out = tmp_path / "build"

        result = export_plugin_bundle(project, out, archive=True)

        assert result.bundle_path.name == "test-pkg-1.0.0.zip"
        assert result.bundle_path.exists()
        assert not (out / "test-pkg-1.0.0").exists()
        with zipfile.ZipFile(result.bundle_path, "r") as zf:
            names = zf.namelist()
            assert any("agent.md" in n for n in names)

    def test_dependency_components_included(self, tmp_path):
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])

        # Dependency content is packed only from lockfile-attested deployed_files.
        deployed = _write_deployed_agent(project, "dep-agent.agent.md", "dep agent body")
        dep = LockedDependency(repo_url="acme/tools", depth=1, deployed_files=deployed)
        _write_lockfile(project, [dep])

        out = tmp_path / "build"
        result = export_plugin_bundle(project, out)

        assert (result.bundle_path / "agents" / "dep-agent.agent.md").exists()
        assert (result.bundle_path / "agents" / "own.agent.md").exists()

    def test_dependency_hash_mismatch_rejects_pack(self, tmp_path):
        """A deployed file whose SHA-256 diverges from the lockfile must fail loud.

        ``_verify_attested_hash`` is the integrity half of the provenance
        guarantee: a deployed file tampered or corrupted after ``apm install``
        must never enter the bundle silently. This regression trap tampers the
        on-disk copy so its content no longer matches the recorded
        ``deployed_file_hashes`` and asserts pack refuses.
        """
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])

        deployed = _write_deployed_agent(project, "dep-agent.agent.md", "attested body")
        rel = deployed[0]
        # Record the hash of the attested content, then tamper the file on disk
        # so the packed bytes no longer match the lockfile attestation.
        attested_hash = compute_file_hash(project / rel)
        (project / rel).write_text("tampered body", encoding="utf-8")
        dep = LockedDependency(
            repo_url="acme/tools",
            depth=1,
            deployed_files=deployed,
            deployed_file_hashes={rel: attested_hash},
        )
        _write_lockfile(project, [dep])

        with pytest.raises(
            ValueError,
            match=r"does not match the hash recorded in apm\.lock\.yaml",
        ):
            export_plugin_bundle(project, tmp_path / "build")

    def test_dependency_hash_match_packs_successfully(self, tmp_path):
        """A deployed file whose SHA-256 matches the lockfile packs cleanly.

        Complements ``test_dependency_hash_mismatch_rejects_pack``: the same
        verification path that rejects a tampered file must accept an intact
        one, so the guard does not become a false-positive blocker.
        """
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])

        deployed = _write_deployed_agent(project, "dep-agent.agent.md", "attested body")
        rel = deployed[0]
        dep = LockedDependency(
            repo_url="acme/tools",
            depth=1,
            deployed_files=deployed,
            deployed_file_hashes={rel: compute_file_hash(project / rel)},
        )
        _write_lockfile(project, [dep])

        result = export_plugin_bundle(project, tmp_path / "build")

        assert (result.bundle_path / "agents" / "dep-agent.agent.md").exists()

    def test_dependency_unattested_cache_is_not_packed(self, tmp_path):
        """apm_modules cache content with no lockfile attestation must fail loud."""
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])

        dep = LockedDependency(repo_url="acme/tools", depth=1)
        _write_lockfile(project, [dep])
        dep_path = project / "apm_modules" / "acme" / "tools"
        _make_apm_dir(dep_path, agents=["dep-agent.agent.md"])

        with pytest.raises(
            ValueError,
            match=r"installed content that cannot be verified exists in the apm_modules cache",
        ):
            export_plugin_bundle(project, tmp_path / "build")

    def test_dependency_deployed_skill_subset_wins_over_raw_cache(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        deployed_files: list[str] = []
        for skill in ("alpha", "beta", "gamma"):
            deployed_files.extend(_write_deployed_skill(project, skill, f"deployed {skill}"))
        dep = LockedDependency(
            repo_url="acme/skill-bundle",
            depth=1,
            package_type="skill_bundle",
            deployed_files=deployed_files,
            skill_subset=["alpha", "beta"],
        )
        _write_lockfile(project, [dep])

        dep_path = project / "apm_modules" / "acme" / "skill-bundle"
        _make_apm_dir(
            dep_path,
            skills={
                "alpha": ["SKILL.md"],
                "beta": ["SKILL.md"],
                "gamma": ["SKILL.md"],
            },
        )

        result = export_plugin_bundle(project, tmp_path / "build")

        skills_dir = result.bundle_path / "skills"
        assert {path.name for path in skills_dir.iterdir()} == {"alpha", "beta"}
        assert (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "deployed alpha"
        assert (skills_dir / "beta" / "SKILL.md").read_text(encoding="utf-8") == "deployed beta"

    def test_dependency_deployed_skills_survive_without_raw_cache(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        deployed_files: list[str] = []
        for skill in ("alpha", "beta"):
            deployed_files.extend(_write_deployed_skill(project, skill, f"deployed {skill}"))
        dep = LockedDependency(
            repo_url="acme/skill-bundle",
            depth=1,
            package_type="skill_bundle",
            deployed_files=deployed_files,
            skill_subset=["alpha", "beta"],
        )
        _write_lockfile(project, [dep])

        result = export_plugin_bundle(project, tmp_path / "build")

        skills_dir = result.bundle_path / "skills"
        assert {path.name for path in skills_dir.iterdir()} == {"alpha", "beta"}
        assert (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "deployed alpha"
        assert (skills_dir / "beta" / "SKILL.md").read_text(encoding="utf-8") == "deployed beta"

    def test_dependency_skill_subset_empty_resolution_errors(self, tmp_path):
        project = _setup_plugin_project(tmp_path)
        deployed_files = _write_deployed_skill(project, "gamma", "deployed gamma")
        dep = LockedDependency(
            repo_url="acme/skill-bundle",
            depth=1,
            package_type="skill_bundle",
            deployed_files=deployed_files,
            skill_subset=["alpha"],
        )
        _write_lockfile(project, [dep])

        with pytest.raises(
            ValueError,
            match=r"skill_subset: alpha\) were not found among its installed files",
        ):
            export_plugin_bundle(project, tmp_path / "build")

    def test_dependency_without_deployed_files_or_cache_skips_cleanly(self, tmp_path):
        """A dep with no attested files and no cached primitives packs cleanly.

        Such a dependency contributes no plugin primitives (e.g. an MCP-only
        or hooks-config-only package), so pack skips it rather than failing.
        """
        project = _setup_plugin_project(tmp_path, agents=["own.agent.md"])
        dep = LockedDependency(repo_url="acme/missing", depth=1)
        _write_lockfile(project, [dep])

        result = export_plugin_bundle(project, tmp_path / "build")

        assert (result.bundle_path / "agents" / "own.agent.md").exists()
        assert not (result.bundle_path / "skills").exists()

    def test_virtual_skill_dependency_does_not_duplicate_skills_dir(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        deployed_files = _write_deployed_skill(project, "javascript-typescript-jest", "# Jest")
        dep = LockedDependency(
            repo_url="github/awesome-copilot",
            depth=1,
            resolved_commit="abc123",
            virtual_path="skills/javascript-typescript-jest",
            is_virtual=True,
            deployed_files=deployed_files,
        )
        _write_lockfile(project, [dep])

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

        # Two deps whose attested deployed files map to the same bundle path
        deployed1 = _write_deployed_agent_at(project, ".github", "shared.agent.md", "from-first")
        deployed2 = _write_deployed_agent_at(project, ".claude", "shared.agent.md", "from-second")
        dep1 = LockedDependency(repo_url="acme/first", depth=1, deployed_files=deployed1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1, deployed_files=deployed2)
        _write_lockfile(project, [dep1, dep2])

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = export_plugin_bundle(project, out)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert content == "from-first"  # First writer wins

    def test_collision_force_last_wins(self, tmp_path):
        project = _setup_plugin_project(tmp_path)

        deployed1 = _write_deployed_agent_at(project, ".github", "shared.agent.md", "from-first")
        deployed2 = _write_deployed_agent_at(project, ".claude", "shared.agent.md", "from-second")
        dep1 = LockedDependency(repo_url="acme/first", depth=1, deployed_files=deployed1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1, deployed_files=deployed2)
        _write_lockfile(project, [dep1, dep2])

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = export_plugin_bundle(project, out, force=True)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert content == "from-second"

    def test_hooks_merged(self, tmp_path):
        """Root (first-party) hooks are packed; dependency cache hooks are not.

        Dependency hooks live in the unattested apm_modules cache, so they are
        no longer merged into the bundle (provenance guarantee). Only the
        project's own hooks reach hooks.json.
        """
        project = _setup_plugin_project(tmp_path)

        # Root hooks
        root_hooks_dir = project / ".apm" / "hooks"
        root_hooks_dir.mkdir(parents=True, exist_ok=True)
        (root_hooks_dir / "hooks.json").write_text(json.dumps({"preCommit": ["root-lint"]}))

        # Dep hooks planted in the unattested cache -- must be ignored
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
        # Only first-party (root) hooks are packed
        assert hooks["preCommit"] == ["root-lint"]
        # Unattested dependency-only hook keys are NOT packed
        assert "postPush" not in hooks

    def test_mcp_merged(self, tmp_path):
        """Root (first-party) MCP is packed; dependency cache MCP is not.

        Dependency .mcp.json lives in the unattested apm_modules cache and is
        not recorded in deployed_files, so it is no longer merged into the
        bundle. Only the project's own MCP config reaches .mcp.json.
        """
        project = _setup_plugin_project(tmp_path)

        # Root MCP
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"root-db": {"command": "root-server"}}})
        )

        # Dep MCP planted in the unattested cache -- must be ignored
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
        # Only first-party (root) MCP servers are packed
        assert mcp["mcpServers"]["root-db"]["command"] == "root-server"
        # Unattested dependency-only server is NOT packed
        assert "dep-only" not in mcp["mcpServers"]

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
        deployed1 = _write_deployed_agent_at(project, ".github", "shared.agent.md", "from-first")
        deployed2 = _write_deployed_agent_at(project, ".claude", "shared.agent.md", "from-second")
        dep1 = LockedDependency(repo_url="acme/first", depth=1, deployed_files=deployed1)
        dep2 = LockedDependency(repo_url="acme/second", depth=1, deployed_files=deployed2)
        _write_lockfile(project, [dep1, dep2])

        out = tmp_path / "build"
        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            result = pack_bundle(project, out, fmt="plugin", force=True)

        content = (result.bundle_path / "agents" / "shared.agent.md").read_text()
        assert content == "from-second"
