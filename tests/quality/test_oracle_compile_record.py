"""I5: compile's APM-home record is the only home write a project compile may make (#4805).

Compile records which routed instructions each Claude AGENTS.md it wrote
carries, in one APM-home file keyed by its output root. The I5 allowance names
that exact file for a real compile only; these negative controls keep it from
widening to a dry run, another root's record or another command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle import oracles
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.lifecycle_state import LifecycleStateSnapshot

RATCHET_TEST_SCOPE = "repository"

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I5")]


def _observe(project: Path, home: Path) -> oracles.Observation:
    return oracles.Observation(
        LifecycleStateSnapshot.capture(project),
        ArtifactSnapshotSet.capture({"home": home}),
        frozenset(),
        project,
        None,
    )


def _written(tmp_path: Path, record: str) -> Path:
    """The home file the step left behind, which I5 judges."""
    return tmp_path / "home" / record


def _i5(tmp_path: Path, args: tuple[str, ...], record: str) -> list[str]:
    """I5 messages after *args* wrote the home file *record*."""
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    before = _observe(project, home)
    path = home / record
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "files": {}}\n', encoding="ascii")
    after = _observe(project, home)
    op = oracles.operation_for(args, forbidden={"home": ("*",)}, mutating=False)
    found = oracles._CHECKS["I5"](before, after, None, None, op)
    return [violation.message for violation in found or ()]


def test_a_compile_may_write_its_own_roots_record(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")

    messages = _i5(tmp_path, ("compile",), record)

    assert _written(tmp_path, record).exists(), record
    assert messages == [], messages


def test_a_dry_run_compile_may_not_write_the_record(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")

    messages = _i5(tmp_path, ("compile", "--dry-run"), record)

    assert _written(tmp_path, record).exists(), record
    assert any(m.startswith("compile wrote home:") and record in m for m in messages), messages


def test_a_compile_may_not_write_another_roots_record(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "elsewhere")

    messages = _i5(tmp_path, ("compile",), record)

    assert _written(tmp_path, record).exists(), record
    assert len(messages) == 1 and messages[0].startswith("compile wrote home:"), messages
    assert record in messages[0] and "/compiled-agents-md'" not in messages[0], messages


def test_no_other_command_may_write_the_record(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")

    messages = _i5(tmp_path, ("install",), record)

    assert _written(tmp_path, record).exists(), record
    assert len(messages) == 1 and messages[0].startswith("install wrote home:"), messages
    assert record in messages[0], messages


def test_a_compile_may_not_write_below_its_record_path(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")
    below = f"{record}/extra.json"

    messages = _i5(tmp_path, ("compile",), below)

    assert _written(tmp_path, below).exists(), below
    assert len(messages) == 1 and messages[0].startswith("compile wrote home:"), messages
    assert below in messages[0], messages


def _i5_compile(tmp_path: Path, prepare, write) -> list[str]:
    """I5 messages of a compile: *prepare* shapes HOME first, then *write* runs."""
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    prepare(home)
    before = _observe(project, home)
    write(home)
    after = _observe(project, home)
    op = oracles.operation_for(("compile",), forbidden={"home": ("*",)}, mutating=False)
    found = oracles._CHECKS["I5"](before, after, None, None, op)
    return [violation.message for violation in found or ()]


def test_a_compile_may_not_write_its_record_as_a_symlink(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")
    target = tmp_path / "elsewhere.json"
    target.write_text("{}\n", encoding="ascii")

    def write(home: Path) -> None:
        path = home / record
        path.parent.mkdir(parents=True)
        path.symlink_to(target)

    messages = _i5_compile(tmp_path, lambda home: None, write)

    assert _written(tmp_path, record).exists() and _written(tmp_path, record).is_symlink(), record
    assert len(messages) == 1 and record in messages[0], messages


def test_a_compile_may_not_replace_an_ancestor_file_of_its_record(tmp_path: Path) -> None:
    record = oracles.compile_home_record(tmp_path / "project")
    ancestor = record.rsplit("/", 1)[0]

    def prepare(home: Path) -> None:
        (home / ancestor).parent.mkdir(parents=True)
        (home / ancestor).write_text("the user's file\n", encoding="ascii")

    def write(home: Path) -> None:
        (home / ancestor).unlink()
        (home / ancestor).mkdir()
        (home / record).write_text('{"version": 1, "files": {}}\n', encoding="ascii")

    messages = _i5_compile(tmp_path, prepare, write)

    assert _written(tmp_path, record).exists(), record
    assert len(messages) == 1 and f"'{ancestor}'" in messages[0], messages
    assert f"'{record}'" not in messages[0], messages
