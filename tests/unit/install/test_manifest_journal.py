"""Hardening of the manifest record a killed ``apm install <package>`` leaves (#5454)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.install.errors import InstallFailureAlreadyRendered
from apm_cli.install.manifest_journal import (
    ManifestJournal,
    replay_abandoned_manifest,
    shell_words,
)

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
        replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")

    assert victim.read_bytes() == b"user data\n"
    message = logger.error.call_args.args[0]
    assert "other than an apm.yml" in message
    assert f"delete {journal.path}" in message


def test_control_and_ansi_bytes_in_a_recorded_package_never_reach_the_terminal(
    workspace: Path,
) -> None:
    _killed_add(workspace, ["./x\x1b[31mred\x07"])
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")

    assert (workspace / "apm.yml").read_bytes() == _PRIOR
    message = logger.warning.call_args.args[0]
    assert all(0x20 <= ord(char) < 0x7F for char in message), repr(message)
    assert "./x?[31mred?" in message


def test_a_kill_between_publication_and_seal_still_restores(workspace: Path) -> None:
    """The writer names its bytes before it publishes them; no seal is needed (#5454)."""
    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"])
    text = "name: proj\nversion: 1.0.0\ndependencies:\n  apm: [./x]\n"
    journal.publish(text)  # killed here, before any seal
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")

    assert (workspace / "apm.yml").read_bytes() == _PRIOR
    assert not journal.path.exists()
    assert "Run 'apm install ./x' to add it again." in logger.warning.call_args.args[0]


def test_a_killed_lockfile_migration_still_restores(workspace: Path) -> None:
    """A legacy-only apm.lock the killed install renamed is the lock it found."""
    (workspace / "apm.lock").write_text("lockfile_version: '1'\ndependencies: []\n", "utf-8")
    _killed_add(workspace, ["./x"])
    (workspace / "apm.lock").rename(workspace / "apm.lock.yaml")
    logger = MagicMock()

    replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")

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
    replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")
    assert (workspace / "apm.yml").read_bytes() == _PRIOR
    assert not journal.path.exists()
    assert f"Run '{retry}' to add it again." in logger.warning.call_args.args[0]

    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"], options)
    (workspace / "apm.yml").write_bytes(b"# hand edit\n")
    replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")
    assert (workspace / "apm.yml").read_bytes() == b"# hand edit\n"
    assert not journal.path.exists()
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

    with pytest.raises(InstallFailureAlreadyRendered):
        replay_abandoned_manifest(deploy, MagicMock(), tmp_path / "other" / "apm.yml")
    assert journal.path.exists(), "another source's command replayed this record"

    replay_abandoned_manifest(deploy, MagicMock(), source / "apm.yml")
    assert (source / "apm.yml").read_bytes() == _PRIOR
    assert not journal.path.exists()


def test_a_record_naming_another_apm_yml_never_writes_it(workspace: Path, tmp_path: Path) -> None:
    """The record's own path is never trusted: only the command's apm.yml is restored."""
    journal = _killed_add(workspace, ["./x"])
    victim = tmp_path / "elsewhere" / "apm.yml"
    victim.parent.mkdir()
    victim.write_bytes(b"name: theirs\n")
    record = json.loads(journal.path.read_text(encoding="utf-8"))
    record["manifest"] = str(victim)
    journal.path.write_text(json.dumps(record), encoding="utf-8")
    ours = (workspace / "apm.yml").read_bytes()

    with pytest.raises(TypeError):  # no caller may replay without naming its manifest
        replay_abandoned_manifest(workspace, MagicMock())  # type: ignore[call-arg]
    with pytest.raises(InstallFailureAlreadyRendered):
        replay_abandoned_manifest(workspace, MagicMock(), workspace / "apm.yml")

    assert victim.read_bytes() == b"name: theirs\n"
    assert (workspace / "apm.yml").read_bytes() == ours


def test_a_record_from_another_source_refuses_and_names_that_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a second source sharing the --root workspace never replaces the record."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    source, deploy = tmp_path / "source-a", tmp_path / "deploy"
    source.mkdir()
    deploy.mkdir()
    (source / "apm.yml").write_bytes(_PRIOR)
    journal = ManifestJournal(
        source / "apm.yml", deploy, _PRIOR, ["./x"], (["--root", str(deploy)], [])
    )
    journal.publish(_PRIOR.decode() + "dependencies:\n  apm: [./x]\n")
    logger = MagicMock()

    with pytest.raises(InstallFailureAlreadyRendered):
        replay_abandoned_manifest(deploy, logger, tmp_path / "source-b" / "apm.yml")

    assert journal.path.exists()
    message = logger.error.call_args.args[0]
    # The command is quoted for this platform's shell (a Windows path keeps its backslashes).
    root = shell_words(["--root", str(deploy)])
    assert f"'apm install {root} ./x' from {source} is still pending" in message
    assert f"Change to {source}, then run 'apm install {root}'" in message


def _other_source_record(tmp_path: Path) -> tuple[ManifestJournal, Path, Path]:
    """Source A's add into the --root workspace *deploy*, killed after it wrote apm.yml."""
    source, deploy = tmp_path / "source-a", tmp_path / "deploy"
    source.mkdir()
    deploy.mkdir()
    (deploy / "apm.lock.yaml").write_text("lockfile_version: '1'\ndependencies: []\n", "utf-8")
    (source / "apm.yml").write_bytes(_PRIOR)
    journal = ManifestJournal(
        source / "apm.yml", deploy, _PRIOR, ["./x"], (["--root", str(deploy)], [])
    )
    journal.publish(_PRIOR.decode() + "dependencies:\n  apm: [./x]\n")
    return journal, source, deploy


def test_a_stale_record_from_another_source_is_dropped_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed lock means nothing is left to restore: no source may be blocked by it."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    journal, source, deploy = _other_source_record(tmp_path)
    (deploy / "apm.lock.yaml").write_text("lockfile_version: '1'\ndependencies: [x]\n", "utf-8")

    replay_abandoned_manifest(deploy, MagicMock(), tmp_path / "source-b" / "apm.yml")

    assert not journal.path.exists()
    assert (source / "apm.yml").read_bytes() != _PRIOR, "a stale record restored apm.yml"


def test_a_record_whose_source_is_gone_is_dropped_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record no command can replay never blocks the workspace forever."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    journal, source, deploy = _other_source_record(tmp_path)
    shutil.rmtree(source)
    logger = MagicMock()

    replay_abandoned_manifest(deploy, logger, tmp_path / "source-b" / "apm.yml")

    assert not journal.path.exists()
    message = logger.warning.call_args.args[0]
    assert f"from {source} can no longer be restored" in message
    assert str(journal.path) in message
    assert message.endswith("Run 'apm install' to continue.")


def test_a_symlinked_apm_yml_is_restored_through_its_link(workspace: Path) -> None:
    """Replay restores the link's file and keeps the link; a moved link is refused."""
    shared = workspace / "shared.yml"
    shared.write_bytes(_PRIOR)
    (workspace / "apm.yml").unlink()
    try:
        (workspace / "apm.yml").symlink_to("shared.yml")
    except OSError:
        pytest.skip("this platform cannot create a symbolic link here")
    added = _PRIOR.decode() + "dependencies:\n  apm: [./x]\n"
    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"])
    journal.publish(added)
    assert (workspace / "apm.yml").is_symlink(), "publishing replaced the link"

    replay_abandoned_manifest(workspace, MagicMock(), workspace / "apm.yml")

    assert (workspace / "apm.yml").is_symlink()
    assert os.readlink(workspace / "apm.yml") == "shared.yml"
    assert shared.read_bytes() == _PRIOR
    assert not journal.path.exists()

    journal = ManifestJournal(workspace / "apm.yml", workspace, _PRIOR, ["./x"])
    journal.publish(added)
    other = workspace / "other.yml"
    other.write_bytes(added.encode())
    (workspace / "apm.yml").unlink()
    (workspace / "apm.yml").symlink_to("other.yml")
    logger = MagicMock()

    with pytest.raises(InstallFailureAlreadyRendered):
        replay_abandoned_manifest(workspace, logger, workspace / "apm.yml")

    assert os.readlink(workspace / "apm.yml") == "other.yml"
    assert other.read_bytes() == added.encode()
    assert journal.path.exists()
    assert f"delete {journal.path}" in logger.error.call_args.args[0]


def test_a_dangling_link_bootstrap_removes_the_created_file_not_the_link(
    workspace: Path,
) -> None:
    """With no prior apm.yml behind a link, replay removes the file the add created."""
    (workspace / "apm.yml").unlink()
    try:
        (workspace / "apm.yml").symlink_to("created.yml")
    except OSError:
        pytest.skip("this platform cannot create a symbolic link here")
    journal = ManifestJournal(workspace / "apm.yml", workspace, None, ["./x"])
    journal.publish("name: proj\nversion: 1.0.0\ndependencies:\n  apm: [./x]\n")
    assert (workspace / "created.yml").is_file()

    replay_abandoned_manifest(workspace, MagicMock(), workspace / "apm.yml")

    assert (workspace / "apm.yml").is_symlink(), "replay removed the link, not the file"
    assert os.readlink(workspace / "apm.yml") == "created.yml"
    assert not (workspace / "created.yml").exists()
    assert not journal.path.exists()
