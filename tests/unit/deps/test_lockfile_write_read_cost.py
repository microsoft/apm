"""Lockfile read cost across a real write: a command reads its own bytes back unparsed."""

from __future__ import annotations

import json
import uuid

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils import yaml_io
from tests.unit.deps.test_lockfile_read_cost import _count_calls, _rich_lock, _yaml_frames

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]


class TestLockFileWrittenTextIsNotReparsed:
    """A command that writes the lockfile reads its own bytes back without parsing them."""

    @pytest.mark.parametrize("staged", [True, False], ids=["staged", "on-disk"])
    def test_read_after_write_needs_no_parse(self, tmp_path, staged):
        from apm_cli.deps.lockfile_write_authority import LockfileWriter, lockfile_write_scope

        path = tmp_path / "apm.lock.yaml"
        lock = _rich_lock(uuid.uuid4().hex)
        reads: list[LockFile | None] = []

        if staged:
            with lockfile_write_scope(LockfileWriter.INSTALL):
                lock.write(path)
                parses = _yaml_frames(lambda: reads.append(LockFile.read(path)))
        else:
            lock.write(path)
            parses = _yaml_frames(lambda: reads.append(LockFile.read(path)))

        assert parses == 0
        assert reads[0] is not None
        assert reads[0].to_yaml() == path.read_text(encoding="utf-8")

    def test_recorded_document_equals_a_fresh_parse_in_key_order(self, tmp_path):
        from apm_cli.deps.lockfile_container import parse_lockfile_text

        path = tmp_path / "apm.lock.yaml"
        _rich_lock(uuid.uuid4().hex).write(path)
        text = path.read_text(encoding="utf-8")

        recorded = parse_lockfile_text(text)
        fresh = yaml_io.load_machine_yaml_str(text)

        assert recorded == fresh
        assert json.dumps(recorded) == json.dumps(fresh)

    def test_generator_gate_composes_each_text_once(self, tmp_path):
        from apm_cli.deps.lockfile_generator import assert_writable

        path = tmp_path / "apm.lock.yaml"
        _rich_lock(uuid.uuid4().hex).write(path)

        composes = _count_calls(
            yaml_io._top_level_scalar_values.__code__,
            lambda: [assert_writable(path) for _ in range(3)],
        )

        assert composes == 1


def test_a_non_ascii_lockfile_needs_no_parse_and_reads_back_equal(tmp_path):
    path = tmp_path / "apm.lock.yaml"
    _rich_lock(uuid.uuid4().hex, unicode=True).write(path)

    parses = _yaml_frames(lambda: LockFile.read(path))
    read = LockFile.read(path)

    assert parses == 0
    assert read is not None
    assert read.to_yaml() == path.read_text(encoding="utf-8")
