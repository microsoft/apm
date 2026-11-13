"""Obstructed deployment paths are reported once, by path, with a remedy (#3678)."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationResult
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import materialize_project_target_profiles
from apm_cli.utils.file_ops import describe_path_obstruction
from apm_cli.utils.path_security import PathTraversalError

_REMEDY = "Move it aside, then run the command again."


def test_directory_in_place_of_deployed_file_is_named(tmp_path: Path) -> None:
    blocked = tmp_path / ".claude/rules/alpha.md"
    blocked.mkdir(parents=True)
    with pytest.raises(OSError) as raised:
        blocked.read_bytes()

    message = describe_path_obstruction(raised.value, tmp_path)

    assert (
        message
        == f".claude/rules/alpha.md is a directory that blocks an APM-managed path. {_REMEDY}"
    )


def test_file_in_place_of_parent_directory_is_named(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/rules").write_text("user notes\n", encoding="ascii")
    with pytest.raises(OSError) as raised:
        (tmp_path / ".claude/rules/alpha.md").write_text("x", encoding="ascii")

    message = describe_path_obstruction(raised.value, tmp_path)

    assert message == f".claude/rules is a file that blocks an APM-managed path. {_REMEDY}"


def test_other_errors_keep_their_own_text(tmp_path: Path) -> None:
    assert (
        describe_path_obstruction(OSError(errno.EACCES, "denied", str(tmp_path)), tmp_path) is None
    )
    assert describe_path_obstruction(ValueError("boom"), tmp_path) is None


def test_skill_member_obstruction_carries_the_blocked_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: s\n---\n", encoding="ascii")
    destination = tmp_path / ".claude/skills/s"
    (destination / "SKILL.md").mkdir(parents=True)

    with pytest.raises(IsADirectoryError) as raised:
        SkillIntegrator().copy_directory_files(
            source,
            destination,
            ignore=lambda _directory, _names: [],
            path_entry=lambda path: path.relative_to(tmp_path).as_posix(),
            is_managed=lambda _entry: True,
            recorded_hashes={},
        )

    assert describe_path_obstruction(raised.value, tmp_path) == (
        f".claude/skills/s/SKILL.md is a directory that blocks an APM-managed path. {_REMEDY}"
    )


def test_target_root_replaced_by_file_is_not_a_resolution_failure(tmp_path: Path) -> None:
    (tmp_path / ".cursor").write_text("user notes\n", encoding="ascii")

    with pytest.raises(PathTraversalError) as raised:
        materialize_project_target_profiles(tmp_path, ["cursor"])

    assert str(raised.value) == f".cursor is a file that blocks an APM-managed path. {_REMEDY}"
    assert (tmp_path / ".cursor").read_text(encoding="ascii") == "user notes\n"


def test_merged_compile_results_report_a_shared_error_once() -> None:
    def result(path: str) -> CompilationResult:
        return CompilationResult(
            success=False,
            output_path=path,
            content="",
            warnings=[],
            errors=["Failed to write distributed output batch: denied"],
            stats={},
        )

    merged = AgentsCompiler()._merge_results([result("AGENTS.md"), result("CLAUDE.md")])

    assert merged.errors == ["Failed to write distributed output batch: denied"]
