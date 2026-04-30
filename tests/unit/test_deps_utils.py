"""Tests for ``apm_cli.commands.deps._utils`` utility functions.

Covers the pure helpers that scan, count, and describe installed packages.
"""

from pathlib import Path

import pytest  # noqa: F401

from apm_cli.commands.deps._utils import (
    _count_package_files,
    _count_primitives,
    _count_workflows,
    _get_detailed_context_counts,
    _get_detailed_package_info,
    _get_package_display_info,
    _is_nested_under_package,
    _scan_installed_packages,
)
from apm_cli.constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

# ------------------------------------------------------------------
# Helpers to build fixture package directories
# ------------------------------------------------------------------


def _make_apm_yml(pkg_dir: Path, name: str = "myrepo", **kwargs) -> None:
    """Write a minimal ``apm.yml`` in *pkg_dir*."""
    lines = [f"name: {name}"]
    for key in ("version", "description", "author", "source"):
        if key in kwargs:
            lines.append(f"{key}: {kwargs[key]}")
    (pkg_dir / APM_YML_FILENAME).write_text("\n".join(lines) + "\n")


def _make_apm_dir(pkg_dir: Path) -> Path:
    """Create and return ``<pkg_dir>/.apm/``."""
    apm = pkg_dir / APM_DIR
    apm.mkdir(parents=True, exist_ok=True)
    return apm


# Intentionally unparsable YAML used to exercise error-handling paths.
_MALFORMED_YML = ":\n  - :\n    ::: bad"


# ==================================================================
# _is_nested_under_package
# ==================================================================


class TestIsNestedUnderPackage:
    """Tests for _is_nested_under_package."""

    def test_direct_child_of_package(self, tmp_path):
        """A subdirectory under a package with apm.yml is nested."""
        modules = tmp_path / "apm_modules"
        pkg = modules / "org" / "repo"
        pkg.mkdir(parents=True)
        _make_apm_yml(pkg, "repo")
        child = pkg / "sub" / "deep"
        child.mkdir(parents=True)
        assert _is_nested_under_package(child, modules) is True

    def test_not_nested_when_no_parent_yml(self, tmp_path):
        """Candidate directly under apm_modules is not nested."""
        modules = tmp_path / "apm_modules"
        candidate = modules / "org" / "repo"
        candidate.mkdir(parents=True)
        assert _is_nested_under_package(candidate, modules) is False

    def test_not_nested_at_boundary(self, tmp_path):
        """Candidate whose parent IS apm_modules_path is not nested."""
        modules = tmp_path / "apm_modules"
        candidate = modules / "standalone"
        candidate.mkdir(parents=True)
        assert _is_nested_under_package(candidate, modules) is False

    def test_nested_multiple_levels_deep(self, tmp_path):
        """Even deeply nested sub-dirs are detected."""
        modules = tmp_path / "apm_modules"
        pkg = modules / "org" / "repo"
        pkg.mkdir(parents=True)
        _make_apm_yml(pkg, "repo")
        deep = pkg / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert _is_nested_under_package(deep, modules) is True


# ==================================================================
# _count_primitives
# ==================================================================


class TestCountPrimitives:
    """Tests for _count_primitives."""

    def test_empty_package(self, tmp_path):
        """Package with no .apm dir returns all zeros."""
        counts = _count_primitives(tmp_path)
        assert counts == {"prompts": 0, "instructions": 0, "agents": 0, "skills": 0, "hooks": 0}

    def test_prompts_in_apm_dir(self, tmp_path):
        """Counts .prompt.md files in .apm/prompts/."""
        apm = _make_apm_dir(tmp_path)
        (apm / "prompts").mkdir()
        (apm / "prompts" / "a.prompt.md").write_text("# prompt")
        (apm / "prompts" / "b.prompt.md").write_text("# prompt")
        counts = _count_primitives(tmp_path)
        assert counts["prompts"] == 2

    def test_instructions_in_apm_dir(self, tmp_path):
        """Counts .md files in .apm/instructions/."""
        apm = _make_apm_dir(tmp_path)
        (apm / "instructions").mkdir()
        (apm / "instructions" / "setup.md").write_text("# setup")
        counts = _count_primitives(tmp_path)
        assert counts["instructions"] == 1

    def test_agents_in_apm_dir(self, tmp_path):
        """Counts .md files in .apm/agents/."""
        apm = _make_apm_dir(tmp_path)
        (apm / "agents").mkdir()
        (apm / "agents" / "agent1.md").write_text("# agent")
        (apm / "agents" / "agent2.md").write_text("# agent")
        (apm / "agents" / "agent3.md").write_text("# agent")
        counts = _count_primitives(tmp_path)
        assert counts["agents"] == 3

    def test_skills_in_apm_dir(self, tmp_path):
        """Counts skill dirs with SKILL.md in .apm/skills/."""
        apm = _make_apm_dir(tmp_path)
        skill1 = apm / "skills" / "skill-a"
        skill1.mkdir(parents=True)
        (skill1 / SKILL_MD_FILENAME).write_text("# skill a")
        skill2 = apm / "skills" / "skill-b"
        skill2.mkdir(parents=True)
        (skill2 / SKILL_MD_FILENAME).write_text("# skill b")
        # dir without SKILL.md should not count
        (apm / "skills" / "empty-dir").mkdir()
        counts = _count_primitives(tmp_path)
        assert counts["skills"] == 2

    def test_root_level_prompt_md(self, tmp_path):
        """Root-level .prompt.md files are counted as prompts."""
        (tmp_path / "run.prompt.md").write_text("# run")
        counts = _count_primitives(tmp_path)
        assert counts["prompts"] == 1

    def test_root_level_skill_md(self, tmp_path):
        """Root-level SKILL.md is counted as a skill."""
        (tmp_path / SKILL_MD_FILENAME).write_text("# skill")
        counts = _count_primitives(tmp_path)
        assert counts["skills"] == 1

    def test_hooks_in_root_hooks_dir(self, tmp_path):
        """Counts .json files in hooks/."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit.json").write_text("{}")
        (hooks_dir / "post-merge.json").write_text("{}")
        counts = _count_primitives(tmp_path)
        assert counts["hooks"] == 2

    def test_hooks_in_apm_hooks_dir(self, tmp_path):
        """Counts .json files in .apm/hooks/."""
        apm = _make_apm_dir(tmp_path)
        hooks_dir = apm / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hook.json").write_text("{}")
        counts = _count_primitives(tmp_path)
        assert counts["hooks"] == 1

    def test_combined_counts(self, tmp_path):
        """Multiple primitive types are counted together correctly."""
        apm = _make_apm_dir(tmp_path)
        # prompts inside .apm
        (apm / "prompts").mkdir()
        (apm / "prompts" / "a.prompt.md").write_text("# p")
        # root-level prompt
        (tmp_path / "root.prompt.md").write_text("# p")
        # instructions
        (apm / "instructions").mkdir()
        (apm / "instructions" / "i.md").write_text("# i")
        # root-level SKILL.md
        (tmp_path / SKILL_MD_FILENAME).write_text("# s")
        counts = _count_primitives(tmp_path)
        assert counts["prompts"] == 2  # 1 apm + 1 root
        assert counts["instructions"] == 1
        assert counts["skills"] == 1


# ==================================================================
# _count_package_files
# ==================================================================


class TestCountPackageFiles:
    """Tests for _count_package_files."""

    def test_no_apm_dir_no_prompts(self, tmp_path):
        """No .apm dir and no root prompts -> (0, 0)."""
        ctx, wf = _count_package_files(tmp_path)
        assert ctx == 0
        assert wf == 0

    def test_no_apm_dir_with_root_prompts(self, tmp_path):
        """No .apm dir but root-level .prompt.md counted as workflows."""
        (tmp_path / "run.prompt.md").write_text("# p")
        ctx, wf = _count_package_files(tmp_path)
        assert ctx == 0
        assert wf == 1

    def test_instructions_counted_as_context(self, tmp_path):
        """Files in .apm/instructions/ are counted as context."""
        apm = _make_apm_dir(tmp_path)
        (apm / "instructions").mkdir()
        (apm / "instructions" / "a.md").write_text("# a")
        (apm / "instructions" / "b.md").write_text("# b")
        ctx, wf = _count_package_files(tmp_path)
        assert ctx == 2
        assert wf == 0

    def test_chatmodes_counted_as_context(self, tmp_path):
        """Files in .apm/chatmodes/ are counted as context."""
        apm = _make_apm_dir(tmp_path)
        (apm / "chatmodes").mkdir()
        (apm / "chatmodes" / "mode.md").write_text("# m")
        ctx, _ = _count_package_files(tmp_path)
        assert ctx == 1

    def test_contexts_dir_counted(self, tmp_path):
        """Files in .apm/context/ (singular) are counted as context."""
        apm = _make_apm_dir(tmp_path)
        (apm / "context").mkdir()
        (apm / "context" / "c.md").write_text("# c")
        ctx, _ = _count_package_files(tmp_path)
        assert ctx == 1

    def test_workflows_in_apm_prompts(self, tmp_path):
        """Workflows in .apm/prompts/ are counted."""
        apm = _make_apm_dir(tmp_path)
        (apm / "prompts").mkdir()
        (apm / "prompts" / "w.prompt.md").write_text("# w")
        _, wf = _count_package_files(tmp_path)
        assert wf == 1

    def test_root_and_apm_prompts_combined(self, tmp_path):
        """Root-level + .apm/prompts/ workflows are summed."""
        apm = _make_apm_dir(tmp_path)
        (apm / "prompts").mkdir()
        (apm / "prompts" / "inner.prompt.md").write_text("# in")
        (tmp_path / "outer.prompt.md").write_text("# out")
        _, wf = _count_package_files(tmp_path)
        assert wf == 2


# ==================================================================
# _count_workflows
# ==================================================================


class TestCountWorkflows:
    """Tests for _count_workflows (thin wrapper)."""

    def test_delegates_to_count_package_files(self, tmp_path):
        """Returns the workflow count from _count_package_files."""
        apm = _make_apm_dir(tmp_path)
        (apm / "prompts").mkdir()
        (apm / "prompts" / "a.prompt.md").write_text("# a")
        assert _count_workflows(tmp_path) == 1

    def test_empty_package(self, tmp_path):
        """Empty package has zero workflows."""
        assert _count_workflows(tmp_path) == 0


# ==================================================================
# _get_detailed_context_counts
# ==================================================================


class TestGetDetailedContextCounts:
    """Tests for _get_detailed_context_counts."""

    def test_no_apm_dir(self, tmp_path):
        """Returns zeros when .apm/ does not exist."""
        result = _get_detailed_context_counts(tmp_path)
        assert result == {"instructions": 0, "chatmodes": 0, "contexts": 0}

    def test_instructions_counted(self, tmp_path):
        """Counts files in .apm/instructions/."""
        apm = _make_apm_dir(tmp_path)
        (apm / "instructions").mkdir()
        (apm / "instructions" / "a.md").write_text("# a")
        result = _get_detailed_context_counts(tmp_path)
        assert result["instructions"] == 1

    def test_chatmodes_counted(self, tmp_path):
        """Counts files in .apm/chatmodes/."""
        apm = _make_apm_dir(tmp_path)
        (apm / "chatmodes").mkdir()
        (apm / "chatmodes" / "debug.md").write_text("# debug")
        (apm / "chatmodes" / "review.md").write_text("# review")
        result = _get_detailed_context_counts(tmp_path)
        assert result["chatmodes"] == 2

    def test_contexts_uses_context_dir(self, tmp_path):
        """'contexts' key maps to .apm/context/ directory (singular name)."""
        apm = _make_apm_dir(tmp_path)
        (apm / "context").mkdir()  # note: singular
        (apm / "context" / "ctx.md").write_text("# ctx")
        result = _get_detailed_context_counts(tmp_path)
        assert result["contexts"] == 1

    def test_all_types_combined(self, tmp_path):
        """Multiple context types are counted independently."""
        apm = _make_apm_dir(tmp_path)
        for d, files in [
            ("instructions", ["i.md"]),
            ("chatmodes", ["m1.md", "m2.md"]),
            ("context", ["c.md"]),
        ]:
            (apm / d).mkdir()
            for f in files:
                (apm / d / f).write_text(f"# {f}")
        result = _get_detailed_context_counts(tmp_path)
        assert result == {"instructions": 1, "chatmodes": 2, "contexts": 1}


# ==================================================================
# _get_package_display_info
# ==================================================================


class TestGetPackageDisplayInfo:
    """Tests for _get_package_display_info."""

    def test_with_apm_yml(self, tmp_path):
        """Extracts name, version, display_name from apm.yml."""
        _make_apm_yml(tmp_path, "testrepo", version="1.2.3")
        info = _get_package_display_info(tmp_path)
        assert info["name"] == "testrepo"
        assert info["version"] == "1.2.3"
        assert info["display_name"] == "testrepo@1.2.3"

    def test_with_apm_yml_empty_version(self, tmp_path):
        """Version defaults to 'unknown' when apm.yml has empty version."""
        _make_apm_yml(tmp_path, "nover", version="")
        info = _get_package_display_info(tmp_path)
        assert info["version"] == "unknown"
        assert "@unknown" in info["display_name"]

    def test_without_apm_yml(self, tmp_path):
        """Falls back to directory name when apm.yml is missing."""
        info = _get_package_display_info(tmp_path)
        assert info["name"] == tmp_path.name
        assert info["version"] == "unknown"
        assert f"{tmp_path.name}@unknown" == info["display_name"]

    def test_malformed_apm_yml(self, tmp_path):
        """Returns error info for unparsable apm.yml."""
        (tmp_path / APM_YML_FILENAME).write_text(_MALFORMED_YML)
        info = _get_package_display_info(tmp_path)
        assert info["version"] == "error"
        assert f"{tmp_path.name}@error" == info["display_name"]


# ==================================================================
# _get_detailed_package_info
# ==================================================================


class TestGetDetailedPackageInfo:
    """Tests for _get_detailed_package_info."""

    def test_full_apm_yml(self, tmp_path):
        """All metadata fields are extracted from apm.yml."""
        _make_apm_yml(
            tmp_path,
            "fullpkg",
            version="3.0.0",
            description="Full package",
            author="Alice",
        )
        info = _get_detailed_package_info(tmp_path)
        assert info["name"] == "fullpkg"
        assert info["version"] == "3.0.0"
        assert info["description"] == "Full package"
        assert info["author"] == "Alice"
        assert info["source"] == "local"  # source not in apm.yml -> default
        assert info["install_path"] == str(tmp_path.resolve())

    def test_no_apm_yml(self, tmp_path):
        """Falls back gracefully when apm.yml is absent."""
        info = _get_detailed_package_info(tmp_path)
        assert info["name"] == tmp_path.name
        assert info["version"] == "unknown"
        assert info["description"] == "No apm.yml found"

    def test_hooks_counted(self, tmp_path):
        """Hook .json files are reflected in the result."""
        _make_apm_yml(tmp_path, "hookpkg", version="1.0.0")
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "h.json").write_text("{}")
        info = _get_detailed_package_info(tmp_path)
        assert info["hooks"] == 1

    def test_workflows_counted(self, tmp_path):
        """Workflow .prompt.md files are reflected in the result."""
        _make_apm_yml(tmp_path, "wfpkg", version="1.0.0")
        apm = _make_apm_dir(tmp_path)
        (apm / "prompts").mkdir()
        (apm / "prompts" / "run.prompt.md").write_text("# run")
        info = _get_detailed_package_info(tmp_path)
        assert info["workflows"] == 1

    def test_context_files_included(self, tmp_path):
        """Context file counts are included."""
        _make_apm_yml(tmp_path, "ctxpkg", version="1.0.0")
        apm = _make_apm_dir(tmp_path)
        (apm / "instructions").mkdir()
        (apm / "instructions" / "setup.md").write_text("# s")
        info = _get_detailed_package_info(tmp_path)
        assert info["context_files"]["instructions"] == 1

    def test_error_handling(self, tmp_path):
        """Error path returns safe fallback dict."""
        # Write an apm.yml that will cause APMPackage.from_apm_yml to fail
        (tmp_path / APM_YML_FILENAME).write_text(_MALFORMED_YML)
        info = _get_detailed_package_info(tmp_path)
        assert info["version"] == "error"
        assert "Error loading package" in info["description"]
        assert info["hooks"] == 0
        assert info["workflows"] == 0

    def test_defaults_for_missing_optional_fields(self, tmp_path):
        """Optional apm.yml fields default gracefully."""
        _make_apm_yml(tmp_path, "minpkg", version="0.0.0")
        info = _get_detailed_package_info(tmp_path)
        assert info["version"] == "0.0.0"
        assert info["description"] == "No description"
        assert info["author"] == "Unknown"
        assert info["source"] == "local"


# ==================================================================
# _scan_installed_packages (additional edge cases beyond test_command_helpers)
# ==================================================================


class TestScanInstalledPackages:
    """Additional edge cases for _scan_installed_packages."""

    def test_three_level_ado_packages(self, tmp_path):
        """ADO-style org/project/repo packages are found."""
        pkg = tmp_path / "org" / "project" / "repo"
        pkg.mkdir(parents=True)
        _make_apm_yml(pkg, "repo")
        result = _scan_installed_packages(tmp_path)
        assert "org/project/repo" in result

    def test_hidden_dirs_skipped(self, tmp_path):
        """Directories starting with '.' are skipped."""
        hidden = tmp_path / "org" / ".hidden"
        hidden.mkdir(parents=True)
        _make_apm_yml(hidden, "hidden")
        result = _scan_installed_packages(tmp_path)
        assert result == []

    def test_packages_with_dot_apm_dir(self, tmp_path):
        """Packages identified by .apm/ directory (no apm.yml)."""
        pkg = tmp_path / "org" / "repo"
        pkg.mkdir(parents=True)
        (pkg / APM_DIR).mkdir()
        result = _scan_installed_packages(tmp_path)
        assert "org/repo" in result

    def test_single_level_dirs_excluded(self, tmp_path):
        """Single-level paths (just 'org') are not included."""
        org = tmp_path / "justorg"
        org.mkdir()
        _make_apm_yml(org, "justorg")
        result = _scan_installed_packages(tmp_path)
        assert result == []
