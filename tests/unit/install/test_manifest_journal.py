"""Hardening of the manifest record a killed ``apm install <package>`` leaves (#5454)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.install.errors import InstallFailureAlreadyRendered
from apm_cli.install.manifest_journal import ManifestJournal, replay_abandoned_manifest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]

_PRIOR = b"name: proj\nversion: 1.0.0\n"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "apm.yml").write_bytes(_PRIOR)
    return root


def _killed_add(workspace: Path, packages: list[str]) -> ManifestJournal:
    """A record as a process killed after it wrote apm.yml leaves it."""
    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, packages)
    (workspace / "apm.yml").write_bytes(_PRIOR + b"dependencies:\n  apm: [./x]\n")
    journal.seal()
    return journal


def test_a_record_naming_another_file_is_refused_and_nothing_is_written(
    workspace: Path,
) -> None:
    journal = _killed_add(workspace, ["./x"])
    victim = workspace / "notes.txt"
    victim.write_bytes(b"user data\n")
    record = json.loads(journal.path.read_text(encoding="utf-8"))
    record["manifest"] = str(victim)
    journal.path.write_text(json.dumps(record), encoding="utf-8")
    logger = MagicMock()

    with pytest.raises(InstallFailureAlreadyRendered):
        replay_abandoned_manifest(workspace, logger)

    assert victim.read_bytes() == b"user data\n"
    message = logger.error.call_args.args[0]
    assert "other than the workspace apm.yml" in message
    assert f"delete {journal.path}" in message


def test_control_and_ansi_bytes_in_a_recorded_package_never_reach_the_terminal(
    workspace: Path,
) -> None:
    _killed_add(workspace, ["./x\x1b[31mred\x07"])
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger)

    assert (workspace / "apm.yml").read_bytes() == _PRIOR
    message = logger.warning.call_args.args[0]
    assert all(0x20 <= ord(char) < 0x7F for char in message), repr(message)
    assert "./x?[31mred?" in message
