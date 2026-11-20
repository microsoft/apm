"""Unit tests for apm_cli.bundle.plugin_exporter (pure logic cases)."""

import pytest

from apm_cli.bundle.plugin_exporter import (
    PackResult,  # noqa: F401
    _collect_bare_skill,  # noqa: F401
    _deep_merge,
    _deployed_path_parts,
    _plugin_rel_for_deployed_path,
    _rename_prompt,
    _update_plugin_json_paths,
    _validate_output_rel,
)
from apm_cli.deps.lockfile import LockedDependency

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Unit tests: hooks / MCP collection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests: devDependencies filtering
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests: collision handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit tests: plugin.json synthesis
# ---------------------------------------------------------------------------


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
