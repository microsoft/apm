"""Hardening of the manifest record a killed ``apm install <package>`` leaves (#5454)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.install import manifest_journal
from apm_cli.install.errors import InstallFailureAlreadyRendered
from apm_cli.install.manifest_journal import (
    ManifestJournal,
    expect_manifest_text,
    replay_abandoned_manifest,
)
from apm_cli.utils.atomic_io import write_text_lf

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]

_PRIOR = b"name: proj\nversion: 1.0.0\n"


@pytest.fixture(autouse=True)
def _no_active_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each cell's journals stay its own: the in-process record pointer is reset."""
    monkeypatch.setattr(manifest_journal, "_ACTIVE", None)


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
    assert "other than an apm.yml" in message
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


def test_a_kill_between_publication_and_seal_still_restores(workspace: Path) -> None:
    """The writer names its bytes before it publishes them; no seal is needed (#5454)."""
    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"])
    text = "name: proj\nversion: 1.0.0\ndependencies:\n  apm: [./x]\n"
    expect_manifest_text(workspace / "apm.yml", text)
    write_text_lf(workspace / "apm.yml", text)  # killed here, before any seal
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger)

    assert (workspace / "apm.yml").read_bytes() == _PRIOR
    assert not journal.path.exists()
    assert "Run 'apm install ./x' to add it again." in logger.warning.call_args.args[0]


def test_a_killed_lockfile_migration_still_restores(workspace: Path) -> None:
    """A legacy-only apm.lock the killed install renamed is the lock it found."""
    (workspace / "apm.lock").write_text("lockfile_version: '1'\ndependencies: []\n", "utf-8")
    _killed_add(workspace, ["./x"])
    (workspace / "apm.lock").rename(workspace / "apm.lock.yaml")
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger)

    assert (workspace / "apm.yml").read_bytes() == _PRIOR


@pytest.mark.parametrize(
    ("options", "retry", "kept"),
    [
        ((["--global"], []), "apm install --global ./x", "apm install --global"),
        ((["--dev"], []), "apm install --dev ./x", "apm install"),
    ],
    ids=["global", "dev"],
)
def test_the_advice_repeats_the_interrupted_command_exactly(
    workspace: Path, options: tuple[list[str], list[str]], retry: str, kept: str
) -> None:
    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"], options)
    (workspace / "apm.yml").write_bytes(_PRIOR + b"dependencies:\n  apm: [./x]\n")
    journal.seal()
    logger = MagicMock()
    replay_abandoned_manifest(workspace, logger)
    assert f"Run '{retry}' to add it again." in logger.warning.call_args.args[0]

    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"], options)
    (workspace / "apm.yml").write_bytes(b"# hand edit\n")
    replay_abandoned_manifest(workspace, logger)
    assert f"Run '{kept}' to lock what apm.yml declares." in logger.warning.call_args.args[0]


def test_a_root_record_restores_its_source_manifest_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--root``: the lockfile workspace is the deploy root, the manifest the source."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    source, deploy = tmp_path / "source", tmp_path / "deploy"
    source.mkdir()
    deploy.mkdir()
    (source / "apm.yml").write_bytes(_PRIOR)
    journal = ManifestJournal(source / "apm.yml", deploy, _PRIOR, ["./x"])
    (source / "apm.yml").write_bytes(_PRIOR + b"dependencies:\n  apm: [./x]\n")
    journal.seal()

    replay_abandoned_manifest(deploy, MagicMock(), tmp_path / "other" / "apm.yml")
    assert journal.path.exists(), "another source's command replayed this record"

    replay_abandoned_manifest(deploy, MagicMock(), source / "apm.yml")
    assert (source / "apm.yml").read_bytes() == _PRIOR
    assert not journal.path.exists()
