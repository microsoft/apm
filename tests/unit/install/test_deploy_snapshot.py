"""Lazy deploy journal: only touched paths are kept, and rollback restores them (#4282)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile_write_authority import (
    LockfileWriter,
    lockfile_write_scope,
    stage_lockfile,
)
from apm_cli.install.transaction import InstallTransaction
from apm_cli.integration import write_seam

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]


def _tree(root: Path) -> dict[str, bytes | str]:
    state: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[rel] = f"-> {os.readlink(path)}"
        elif path.is_dir():
            state[rel] = "<dir>"
        else:
            state[rel] = path.read_bytes()
    return state


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "apm-home"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


def _receipts(apm_home: Path) -> Path:
    return apm_home / "deployment-receipts"


def _transaction(project: Path, *, acquire_lock: bool = False) -> InstallTransaction:
    project.mkdir(parents=True, exist_ok=True)
    return InstallTransaction(
        manifest_path=project / "apm.yml",
        apm_modules_dir=project / "apm_modules",
        validation=None,
        logger=None,
        acquire_lock=acquire_lock,
    )


def test_only_the_paths_an_install_writes_are_copied(tmp_path: Path, apm_home: Path) -> None:
    project = tmp_path / "project"
    agents = project / ".github" / "agents"
    agents.mkdir(parents=True)
    (agents / "touched.md").write_bytes(b"v1\n")
    (agents / "untouched.md").write_bytes(b"other\n")
    transaction = _transaction(project)

    transaction.journal_deploy_writes()
    assert not (apm_home / "install-snapshots").exists()
    write_seam.write_text_lf(agents / "touched.md", "v2\n")
    write_seam.write_text_lf(agents / "touched.md", "v3\n")

    [store] = (apm_home / "install-snapshots").iterdir()
    copies = [p.read_bytes() for p in store.iterdir() if p.name != "journal.jsonl"]
    assert copies == [b"v1\n"]
    transaction.rollback()
    assert (agents / "touched.md").read_bytes() == b"v1\n"
    assert not (apm_home / "install-snapshots").exists()


def test_rollback_returns_every_written_path_to_its_prior_state(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "kept.md").write_bytes(b"v1\n")
    (agents / "link.md").symlink_to("kept.md")
    (project / ".gitignore").write_bytes(b"node_modules/\n")
    skill = project / ".claude" / "skills" / "old"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"old skill\n")
    before = _tree(project)
    transaction = _transaction(project)
    transaction.journal_deploy_writes()

    write_seam.write_text_lf(agents / "kept.md", "v2 \u202e\n")
    write_seam.write_text_lf(agents / "new.md", "new\n")
    write_seam.unlink(agents / "link.md")
    with write_seam.open_for_write(project / ".gitignore", "a") as handle:
        handle.write("apm_modules/\n")
    write_seam.rmtree(skill)
    created = project / ".agents" / "skills" / "s"
    write_seam.mkdir(created, parents=True)
    write_seam.write_text(created / "SKILL.md", "skill\n")

    transaction.rollback()

    assert _tree(project) == before
    assert not (apm_home / "install-snapshots").exists()


def test_a_rollback_discards_the_staged_lockfile(tmp_path: Path, apm_home: Path) -> None:
    project = tmp_path / "project"
    lock = project / "apm.lock.yaml"
    deployed = project / ".github" / "agents" / "a.md"
    transaction = _transaction(project)

    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        transaction.journal_deploy_writes()
        write_seam.mkdir(deployed.parent, parents=True, exist_ok=True)
        write_seam.write_text_lf(deployed, "a\n")
        assert stage_lockfile(lock, "lockfile_version: '1'\n")
        transaction.rollback()
        raise RuntimeError("failed after deploy")

    assert not lock.exists()
    assert not (project / ".github").exists()


@pytest.mark.parametrize("lock_commits", [True, False])
def test_the_journal_settles_only_when_the_staged_lockfile_reaches_disk(
    tmp_path: Path, apm_home: Path, lock_commits: bool
) -> None:
    project = tmp_path / "project"
    lock = project / "apm.lock.yaml"
    deployed = project / "a.md"
    transaction = _transaction(project)

    with pytest.raises(KeyboardInterrupt) if not lock_commits else _no_error():
        with lockfile_write_scope(LockfileWriter.INSTALL):
            transaction.journal_deploy_writes()
            write_seam.write_text_lf(deployed, "a\n")
            stage_lockfile(lock, "lockfile_version: '1'\n")
            transaction.release_deploy_targets()
            if not lock_commits:
                raise KeyboardInterrupt
    transaction.rollback()

    assert lock.exists() is lock_commits
    assert deployed.exists() is lock_commits
    assert not (apm_home / "install-snapshots").exists()


class _no_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_the_next_install_replays_only_its_own_abandoned_journals(
    tmp_path: Path, apm_home: Path
) -> None:
    """#4863: a killed install's journal is replayed for its root; others stay."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    store_parent = apm_home / "install-snapshots"
    project, other = tmp_path / "project", tmp_path / "other"
    for root in (project, other):
        root.mkdir()
    created, foreign = project / ".kiro" / "a.md", other / "b.md"
    for root, path in ((project, created), (other, foreign)):
        journal = DeployJournal(store_parent, root=root)
        journal.record(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"deployed by a killed install\n")
        journal.after_write(path)
    retained = store_parent / "retained-needed"
    retained.mkdir()
    (retained / "0").write_bytes(b"prior\n")
    transaction = _transaction(project, acquire_lock=True)

    transaction.rollback()

    assert not created.exists() and not created.parent.exists()
    assert foreign.read_bytes() == b"deployed by a killed install\n"
    assert sorted(p.name.split("-")[0] for p in store_parent.iterdir()) == ["deploy", "retained"]
    assert (retained / "0").read_bytes() == b"prior\n"


def test_receipts_under_apm_home_replay_without_a_ledger_row(
    tmp_path: Path, apm_home: Path
) -> None:
    """APM-home metadata restores when it re-hashes as journaled; root paths need the ledger."""
    from apm_cli.install.deploy_snapshot import (
        DeployJournal,
        JournalReplayError,
        replay_abandoned_journals,
    )

    project = tmp_path / "project"
    receipt, deployed = apm_home / "deployment-receipts" / "r.json", project / "a.md"
    for path in (receipt, deployed):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"prior\n")
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    for path in (receipt, deployed):
        journal.record(path)
        path.write_bytes(b"killed\n")

    with pytest.raises(JournalReplayError, match="kept") as kept:
        replay_abandoned_journals(
            apm_home / "install-snapshots", project, dict, receipts=_receipts(apm_home)
        )

    assert kept.value.issues == [("a.md", "the deployment ledger does not record these bytes")]
    assert receipt.read_bytes() == deployed.read_bytes() == b"killed\n"


def _journaled_receipt(apm_home: Path, project: Path) -> tuple[Path, Path]:
    """A killed install's journal holding one receipt it rewrote; return (receipt, store)."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    receipt = apm_home / "deployment-receipts" / "r.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes(b"prior\n")
    project.mkdir(exist_ok=True)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(receipt)
    receipt.write_bytes(b"killed\n")
    journal.after_write(receipt)
    assert journal.store is not None
    return receipt, journal.store


def _retarget(store: Path, path: str) -> None:
    """Point the journal's one unit at *path* (relative to APM home)."""
    import json

    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text().splitlines()]
    lines[1]["path"] = path
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_a_receipt_is_restored_from_its_journal_copy(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import replay_abandoned_journals

    project = tmp_path / "project"
    receipt, _store = _journaled_receipt(apm_home, project)

    outcome = replay_abandoned_journals(
        apm_home / "install-snapshots", project, dict, receipts=_receipts(apm_home)
    )

    assert (outcome.restored, outcome.kept) == (1, ())
    assert receipt.read_bytes() == b"prior\n"


def test_a_receipt_whose_copy_does_not_match_its_hash_is_not_restored(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError, replay_abandoned_journals

    project = tmp_path / "project"
    receipt, store = _journaled_receipt(apm_home, project)
    (store / "0").write_bytes(b"forged\n")

    with pytest.raises(JournalReplayError) as kept:
        replay_abandoned_journals(
            apm_home / "install-snapshots", project, dict, receipts=_receipts(apm_home)
        )

    assert kept.value.issues == [
        ("deployment-receipts/r.json", "journal copy does not match its recorded hash")
    ]
    assert receipt.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    "escape",
    [
        "dotdot",
        "symlinked-dir",
        "outside-receipts",
        "another-store",
        "symlinked-receipt",
        "absolute",
        "drive",
    ],
)
def test_a_receipt_path_leaving_the_receipts_directory_is_refused(
    tmp_path: Path, apm_home: Path, escape: str
) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError, replay_abandoned_journals

    project = tmp_path / "project"
    receipt, store = _journaled_receipt(apm_home, project)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "r.json"
    victim.write_bytes(b"not apm's\n")
    config = apm_home / "config.json"
    config.write_bytes(b"user config\n")
    path = {
        "dotdot": "deployment-receipts/../../outside/r.json",
        "symlinked-dir": "deployment-receipts/linked/r.json",
        "outside-receipts": "config.json",
        "another-store": f"install-snapshots/{store.name}/0",
        "symlinked-receipt": "deployment-receipts/link.json",
        "absolute": str(victim),
        "drive": "C:/outside/r.json",
    }[escape]
    (apm_home / "deployment-receipts" / "linked").symlink_to(outside, target_is_directory=True)
    (apm_home / "deployment-receipts" / "link.json").symlink_to(victim)
    _retarget(store, path)

    with pytest.raises(JournalReplayError) as kept:
        replay_abandoned_journals(
            apm_home / "install-snapshots", project, dict, receipts=_receipts(apm_home)
        )

    assert [p for p, _reason in kept.value.issues] == [path]
    assert not kept.value.applied
    assert victim.read_bytes() == b"not apm's\n"
    assert config.read_bytes() == b"user config\n"
    assert receipt.read_bytes() == b"killed\n"


@pytest.mark.parametrize(
    "path", ["../outside.md", "/tmp/outside.md", "C:/outside.md", "C:outside.md", "\\\\host\\share"]
)
def test_a_deploy_root_path_that_is_absolute_or_escapes_is_refused(
    tmp_path: Path, apm_home: Path, path: str
) -> None:
    import json

    from apm_cli.install.deploy_snapshot import (
        DeployJournal,
        JournalReplayError,
        replay_abandoned_journals,
    )

    project = tmp_path / "project"
    deployed = project / "a.md"
    project.mkdir()
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(deployed)
    deployed.write_bytes(b"killed\n")
    assert journal.store is not None
    manifest = journal.store / "journal.jsonl"
    header, unit = (json.loads(line) for line in manifest.read_text().splitlines())
    unit["path"] = path
    manifest.write_text(f"{json.dumps(header)}\n{json.dumps(unit)}\n", encoding="utf-8")

    with pytest.raises(JournalReplayError) as kept:
        replay_abandoned_journals(
            apm_home / "install-snapshots", project, dict, receipts=_receipts(apm_home)
        )

    assert kept.value.issues == [(path, "journal path escapes the deploy root")]
    assert deployed.read_bytes() == b"killed\n"


def _replay(apm_home: Path, project: Path, ledger=dict):
    from apm_cli.install.deploy_snapshot import replay_abandoned_journals

    return replay_abandoned_journals(
        apm_home / "install-snapshots", project, ledger, receipts=_receipts(apm_home)
    )


def _killed_journal(apm_home: Path, project: Path, path: Path, *, seal: bool = True):
    """Journal *path*, then overwrite it as a killed install would."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    project.mkdir(parents=True, exist_ok=True)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"killed\n")
    if seal:
        journal.after_write(path)
    assert journal.store is not None
    return journal.store


def test_shared_config_the_ledger_does_not_claim_keeps_the_journal(
    tmp_path: Path, apm_home: Path
) -> None:
    """Shared settings outside the ledger, with no config file record: kept and reported (#5009)."""
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": {}}\n')
    _killed_journal(apm_home, project, settings)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    assert kept.value.issues == [
        (".claude/settings.json", "the deployment ledger does not record these bytes")
    ]
    assert settings.read_bytes() == b"killed\n"
    assert kept.value.store.name.startswith("retained-")


def test_a_receipts_directory_the_killed_install_created_is_removed(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    project.mkdir()
    receipts = _receipts(apm_home)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(receipts, contents=False)
    receipts.mkdir(parents=True)
    journal.after_write(receipts)
    journal.record(receipts / "r.json")
    (receipts / "r.json").write_bytes(b"killed\n")
    journal.after_write(receipts / "r.json")

    outcome = _replay(apm_home, project)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert not receipts.exists()


def test_a_path_journaled_before_its_write_restores_without_a_gate_seal(
    tmp_path: Path, apm_home: Path
) -> None:
    """Killed before any gate seal: the journal line itself seals the path."""
    project = tmp_path / "project"
    created = project / "a.md"
    _killed_journal(apm_home, project, created, seal=False)

    outcome = _replay(apm_home, project)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert not created.exists()


def test_an_unreadable_ledger_keeps_the_journal(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError, LedgerUnavailable

    project = tmp_path / "project"
    created = project / "a.md"
    _killed_journal(apm_home, project, created)

    def unreadable():
        raise LedgerUnavailable(project / "apm.lock.yaml", "the lockfile cannot be read: bad")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, unreadable)

    assert kept.value.issues == [
        (str(project / "apm.lock.yaml"), "the lockfile cannot be read: bad")
    ]
    assert created.read_bytes() == b"killed\n"


def test_a_directory_restores_only_when_the_ledger_records_all_its_files(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal, JournalReplayError
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    skill = project / ".kiro" / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"prior\n")
    digest = compute_file_hash(skill / "SKILL.md")
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    (skill / "SKILL.md").write_bytes(b"killed\n")
    journal.after_write(skill / "SKILL.md")
    ledger = {".kiro/skills/s/SKILL.md": digest, ".kiro/skills/s/extra.md": digest}

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert kept.value.issues == [
        (".kiro/skills/s", "the deployment ledger records other files under this directory")
    ]
    assert (skill / "SKILL.md").read_bytes() == b"killed\n"


@pytest.mark.parametrize(
    "mutation",
    [
        {"path": "./a.md"},
        {"path": "."},
        {"path": "x//a.md"},
        {"ancestors": 1},
        {"ancestors": [3]},
        {"kind": "file", "copy": ["0"], "hash": "sha256:0"},
        {"path": "x\\a.md"},
        {"ancestors": ["other"]},
        {"expected": []},
    ],
    ids=[
        "dot-slash",
        "dot",
        "double-slash",
        "ancestors-int",
        "ancestor-int",
        "copy-list",
        "backslash",
        "unrelated-ancestor",
        "expected-list",
    ],
)
def test_a_malformed_unit_keeps_the_journal(tmp_path: Path, apm_home: Path, mutation: dict) -> None:
    import json

    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    created = project / "a.md"
    store = _killed_journal(apm_home, project, created)
    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text().splitlines()]
    lines[1].update(mutation)
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    [(_path, reason)] = kept.value.issues
    assert "ledger" not in reason, reason
    assert not kept.value.applied
    assert created.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_receipts_directory_is_refused(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    receipt, _store = _journaled_receipt(apm_home, project)
    real = receipt.parent.rename(tmp_path / "moved")
    for item in real.iterdir():
        item.rename(outside / item.name)
    _receipts(apm_home).symlink_to(outside, target_is_directory=True)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    assert kept.value.issues == [
        ("deployment-receipts/r.json", "journal path escapes the deploy root")
    ]
    assert (outside / "r.json").read_bytes() == b"killed\n"


def test_a_user_edit_to_a_json_config_killed_before_its_seal_is_kept(
    tmp_path: Path, apm_home: Path
) -> None:
    """A JSON write names the bytes it will leave: other bytes there are a user's (#4863)."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.integration import write_seam

    project = tmp_path / "project"
    config = project / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    # Killed after the write and before its post-write seal: no after-listener.
    write_seam.listen(record)
    try:
        write_seam.write_json_config(
            config, {"mcpServers": {"apm": {}}}, default_trailing_newline=True
        )
    finally:
        write_seam.stop_listening(record)
    config.write_text('{"mine": true}\n', encoding="utf-8")

    outcome = _replay(apm_home, project)

    assert outcome.kept == ((".cursor/mcp.json", "it changed after the interrupted install"),)
    assert config.read_text(encoding="utf-8") == '{"mine": true}\n'


def test_a_user_edit_to_hook_settings_killed_before_their_seal_is_kept(
    tmp_path: Path, apm_home: Path
) -> None:
    """The hook settings write names its bytes, so a user's later edit is kept (#4863)."""
    import json

    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.integration import write_seam
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    hooks = {"evt": [{"command": "x", "_apm_source": "pkg"}, {"command": "y"}]}
    settings.write_text(json.dumps({"theme": "dark", "hooks": hooks}), encoding="utf-8")
    # The ledger vouches for the prior bytes, so only the seal decides.
    ledger = {".claude/settings.json": compute_file_hash(settings)}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    # Killed after the settings write and before its post-write seal.
    write_seam.listen(record)
    try:
        HookIntegrator._clean_apm_entries_from_json(settings, {"files_removed": 0})
    finally:
        write_seam.stop_listening(record)
    settings.write_text('{"theme": "light"}\n', encoding="utf-8")

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == ((".claude/settings.json", "it changed after the interrupted install"),)
    assert settings.read_text(encoding="utf-8") == '{"theme": "light"}\n'


@pytest.mark.parametrize("sealed", [True, False], ids=["after-its-seal", "before-its-seal"])
def test_a_line_ending_only_edit_after_the_kill_is_kept(
    tmp_path: Path, apm_home: Path, sealed: bool
) -> None:
    """Seals and expected hashes are byte-exact: CRLF over APM's LF is a user's edit."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    written = project / "a.md"
    project.mkdir()
    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    write_seam.listen(record, journal.after_write if sealed else None)
    try:
        write_seam.write_text_lf(written, "one\ntwo\n")
    finally:
        write_seam.stop_listening(record)
    written.write_bytes(b"one\r\ntwo\r\n")

    outcome = _replay(apm_home, project)

    assert outcome.kept == (("a.md", "it changed after the interrupted install"),)
    assert written.read_bytes() == b"one\r\ntwo\r\n"


@pytest.mark.parametrize(
    "write",
    [
        lambda p: write_seam.write_text(p, "a\nb\r\n"),
        lambda p: write_seam.write_text(p, "a\nb\r\n", newline=""),
        lambda p: write_seam.write_text(p, "a\nb\n", newline="\r\n"),
        lambda p: write_seam.write_text_lf(p, "a\r\nb\n"),
        lambda p: write_seam.atomic_write_text(p, "a\r\nb\n"),
        lambda p: write_seam.atomic_write_text(p, "a\r\nb\n", normalize_line_endings=False),
        lambda p: write_seam.write_json_config(p, {"a": 1}, default_trailing_newline=True),
    ],
    ids=["text", "text-as-is", "text-crlf", "text-lf", "atomic", "atomic-as-is", "json"],
)
def test_each_seam_writer_expects_the_exact_bytes_it_leaves(tmp_path: Path, write) -> None:
    """On every platform, a write's expected hash is the hash of the bytes on disk."""
    from apm_cli.utils.content_hash import exact_hash_of_bytes

    target = tmp_path / "f.txt"
    seen: list[str | None] = []

    def record(path: Path, contents: bool, expected: str | None) -> None:
        seen.append(expected)

    write_seam.listen(record)
    try:
        write(target)
    finally:
        write_seam.stop_listening(record)

    assert seen == [exact_hash_of_bytes(target.read_bytes())]


def test_a_kill_before_a_repeated_write_starts_restores_the_sealed_bytes(
    tmp_path: Path, apm_home: Path
) -> None:
    """A pending write keeps the last seal: APM's earlier bytes are still APM's."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    created = project / "a.md"
    project.mkdir()
    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    write_seam.listen(record, journal.after_write)
    try:
        write_seam.write_text_lf(created, "first\n")
    finally:
        write_seam.stop_listening(record)
    # The repeated write is journaled, then the kill lands before any byte changes.
    journal.record(created, expected="sha256:" + "0" * 64)

    outcome = _replay(apm_home, project)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert not created.exists()


def test_a_deeply_nested_journal_header_is_kept_and_named(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    created = project / "a.md"
    store = _killed_journal(apm_home, project, created)
    lines = (store / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    nested = "[" * 100000 + "]" * 100000
    (store / "journal.jsonl").write_text("\n".join([nested, *lines[1:]]) + "\n", encoding="utf-8")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    assert kept.value.issues == [(str(store), "journal manifest is nested too deeply")]
    assert kept.value.store.name.startswith("retained-")
    assert created.read_bytes() == b"killed\n"


def test_a_journal_of_another_workspace_sharing_the_root_is_left_alone(
    tmp_path: Path, apm_home: Path
) -> None:
    """User scope and a project at HOME share a deploy root, not a ledger."""
    from apm_cli.install.deploy_snapshot import DeployJournal, replay_abandoned_journals

    project = tmp_path / "home"
    created = project / "a.md"
    project.mkdir()
    journal = DeployJournal(
        apm_home / "install-snapshots", root=project, workspace=tmp_path / "home" / ".apm"
    )
    journal.record(created)
    created.write_bytes(b"killed\n")
    journal.after_write(created)

    outcome = replay_abandoned_journals(
        apm_home / "install-snapshots",
        project,
        dict,
        receipts=_receipts(apm_home),
        workspace=project,
    )

    assert outcome.restored == 0
    assert created.read_bytes() == b"killed\n"
    assert journal.store is not None and journal.store.is_dir()


def test_a_lock_only_transaction_never_replays_a_journal(tmp_path: Path, apm_home: Path) -> None:
    """``apm lock`` changes no deployed file, not even to replay (#4863)."""
    from apm_cli.install.transaction import InstallTransaction

    project = tmp_path / "project"
    created = project / "a.md"
    store = _killed_journal(apm_home, project, created)

    transaction = InstallTransaction(
        manifest_path=project / "apm.yml",
        apm_modules_dir=project / "apm_modules",
        validation=None,
        logger=None,
        recover_deploys=False,
    )
    transaction.rollback()

    assert created.read_bytes() == b"killed\n"
    assert store.is_dir()


@pytest.mark.parametrize("flag", ["dry_run", "lockfile_only"])
def test_a_preview_or_lock_only_pipeline_never_replays_a_journal(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """A direct preview or lock-only pipeline call changes no deployed file (#4863)."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.transaction import transactional_pipeline

    project = tmp_path / "project"
    created = project / "a.md"
    store = _killed_journal(apm_home, project, created)
    monkeypatch.chdir(project)

    transactional_pipeline(lambda **_kwargs: None)(
        scope=InstallScope.PROJECT, logger=None, **{flag: True}
    )

    assert created.read_bytes() == b"killed\n"
    assert store.is_dir()


def test_a_journal_without_a_manifest_is_reclaimed(tmp_path: Path, apm_home: Path) -> None:
    """A fast apply's in-memory journal records nothing to replay: reclaimed, as before."""
    from apm_cli.install.deploy_snapshot import has_abandoned_journals

    store = apm_home / "install-snapshots" / "deploy-fast"
    store.mkdir(parents=True)
    (store / "0").write_bytes(b"prior lockfile\n")
    assert has_abandoned_journals(store.parent)

    outcome = _replay(apm_home, tmp_path)

    assert outcome.restored == 0
    assert not store.parent.exists()


@pytest.mark.parametrize(
    ("store", "counts"),
    [("own", True), ("another-workspace", False), ("unreadable", True), ("no-manifest", True)],
)
def test_an_abandoned_journal_counts_only_where_replay_would_handle_it(
    tmp_path: Path, apm_home: Path, store: str, counts: bool
) -> None:
    """The fast path asks the replay owner's question: would this install replay it? (#5064)"""
    from apm_cli.install.deploy_snapshot import has_abandoned_journals

    project, other = tmp_path / "project", tmp_path / "other"
    root = other if store == "another-workspace" else project
    kept = _killed_journal(apm_home, root, root / "a.md")
    if store == "unreadable":
        (kept / "journal.jsonl").write_text("not json\n", encoding="utf-8")
    if store == "no-manifest":
        (kept / "journal.jsonl").unlink()

    assert has_abandoned_journals(kept.parent, project, workspace=project) is counts
    assert has_abandoned_journals(kept.parent)
    assert kept.is_dir(), "asking never touches the journal"
    assert (root / "a.md").read_bytes() == b"killed\n"


def test_a_malformed_seal_keeps_the_journal(tmp_path: Path, apm_home: Path) -> None:
    import json

    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    created = project / "d" / "a.md"
    store = _killed_journal(apm_home, project, created)
    with open(store / "journal.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"sealed": 0, "sub": "", "state": {"dir": 1}}) + "\n")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    assert kept.value.issues == [(str(store), "journal manifest holds a malformed write event")]
    assert created.read_bytes() == b"killed\n"


def test_an_unreadable_copy_keeps_the_journal(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.install import deploy_snapshot
    from apm_cli.install.deploy_snapshot import JournalReplayError
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    target = project / "a.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior\n")
    ledger = {"a.md": compute_file_hash(target)}
    _killed_journal(apm_home, project, target)

    def unreadable(_path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(deploy_snapshot, "compute_file_hash", unreadable)
    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert kept.value.issues == [("a.md", "journal copy unreadable: denied")]
    assert not kept.value.applied


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_mode_set_through_the_seam_is_part_of_what_apm_wrote(
    tmp_path: Path, apm_home: Path
) -> None:
    """A chmod after a copy is sealed, so replay removes APM's executable (#4863)."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.integration import write_seam

    project = tmp_path / "project"
    source, deployed = tmp_path / "run.sh", project / "bin" / "run.sh"
    source.write_bytes(b"#!/bin/sh\n")
    project.mkdir()
    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    write_seam.listen(record, journal.after_write)
    try:
        write_seam.mkdir(deployed.parent, parents=True, exist_ok=True)
        write_seam.copy2(source, deployed)
        write_seam.chmod(deployed, 0o700)
    finally:
        write_seam.stop_listening(record)

    outcome = _replay(apm_home, project)

    assert outcome.kept == ()
    assert not deployed.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_directory_mode_changed_after_the_kill_keeps_the_directory(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    project.mkdir()
    created = project / ".kiro" / "agents"
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(created, contents=False, expected="dir")
    created.mkdir(parents=True)
    journal.after_write(created)
    journal.record(created / "a.md", expected=None)
    (created / "a.md").write_bytes(b"apm\n")
    journal.after_write(created / "a.md")
    created.chmod(0o700)

    outcome = _replay(apm_home, project)

    assert outcome.kept == ((".kiro/agents", "it changed after the interrupted install"),)
    assert (created / "a.md").read_bytes() == b"apm\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_mode_changed_after_the_kill_keeps_the_file(tmp_path: Path, apm_home: Path) -> None:
    """A seal fingerprints modes too: a chmod after the kill is a user's change."""
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    script = project / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"prior\n")
    ledger = {"run.sh": compute_file_hash(script)}
    _killed_journal(apm_home, project, script)
    script.chmod(0o700)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == (("run.sh", "it changed after the interrupted install"),)
    assert script.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_journal_path_through_a_symlink_alias_is_refused(tmp_path: Path, apm_home: Path) -> None:
    import json

    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    claimed = project / ".kiro" / "claimed.md"
    claimed.parent.mkdir(parents=True)
    claimed.write_bytes(b"committed\n")
    (project / "alias").symlink_to(project / ".kiro", target_is_directory=True)
    store = _killed_journal(apm_home, project, project / "new.md")
    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text().splitlines()]
    lines[1]["path"] = "alias/claimed.md"
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: {".kiro/claimed.md": None})

    assert kept.value.issues == [("alias/claimed.md", "journal path escapes the deploy root")]
    assert claimed.read_bytes() == b"committed\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_replay_restores_a_file_with_its_mode(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal, replay_abandoned_journals
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    hook = project / ".github" / "hooks" / "run.sh"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(b"#!/bin/sh\necho prior\n")
    hook.chmod(0o700)
    ledger = {".github/hooks/run.sh": compute_file_hash(hook)}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(hook)
    hook.write_bytes(b"#!/bin/sh\necho killed\n")
    hook.chmod(0o755)
    journal.after_write(hook)

    restored = replay_abandoned_journals(
        apm_home / "install-snapshots", project, lambda: ledger, receipts=_receipts(apm_home)
    )

    assert (restored.restored, restored.kept) == (1, ())
    assert hook.read_bytes() == b"#!/bin/sh\necho prior\n"
    assert hook.stat().st_mode & 0o777 == 0o700
    assert not (apm_home / "install-snapshots").exists()


def test_rollback_removes_only_empty_target_roots_the_attempt_created(tmp_path: Path) -> None:
    """#4159: created roots left empty go; anything else stays."""
    project = tmp_path / "project"
    project.mkdir()
    empty_root = project / ".github"
    used_root = project / ".claude"
    preexisting = project / ".cursor"
    for root in (empty_root, used_root, preexisting):
        root.mkdir()
    (used_root / "user.md").write_bytes(b"mine\n")
    transaction = _transaction(project)
    transaction.track_created_target_roots([empty_root, used_root])

    transaction.rollback()

    assert not empty_root.exists()
    assert (used_root / "user.md").read_bytes() == b"mine\n"
    assert preexisting.is_dir()


def test_a_marked_store_survives_the_reaper_when_its_rename_fails(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal, replay_abandoned_journals

    target = tmp_path / "a.md"
    target.write_bytes(b"prior\n")
    journal = DeployJournal(apm_home / "install-snapshots")
    journal.record(target)

    def refuse(_self: Path, _target: Path) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(Path, "rename", refuse)
    retained = journal._retain()
    monkeypatch.undo()
    replay_abandoned_journals(
        apm_home / "install-snapshots", tmp_path, dict, receipts=_receipts(apm_home)
    )

    assert retained.name.startswith("deploy-")
    assert (retained / "0").read_bytes() == b"prior\n"


def test_an_append_through_a_symlink_restores_its_destination(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    shared = tmp_path / "shared.gitignore"
    shared.write_bytes(b"node_modules/\n")
    project.mkdir()
    (project / ".gitignore").symlink_to(shared)
    transaction = _transaction(project)
    transaction.journal_deploy_writes()

    with write_seam.open_for_write(project / ".gitignore", "a") as handle:
        handle.write("apm_modules/\n")
    transaction.rollback()

    assert shared.read_bytes() == b"node_modules/\n"
    assert (project / ".gitignore").is_symlink()
    assert (project / ".gitignore").resolve() == shared.resolve()


def test_a_rollback_with_nothing_deployed_discards_the_staged_lockfile(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    lock = project / "apm.lock.yaml"
    transaction = _transaction(project)

    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        transaction.journal_deploy_writes()
        assert stage_lockfile(lock, "lockfile_version: '1'\n")
        transaction.rollback()
        raise RuntimeError("failed with nothing deployed")

    assert not lock.exists()


def test_the_journal_settles_before_a_failing_post_commit_action(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.deps.lockfile_write_authority import after_lockfile_commit

    project = tmp_path / "project"
    lock = project / "apm.lock.yaml"
    deployed = project / "a.md"
    transaction = _transaction(project)

    def bind_claims() -> None:
        raise RuntimeError("claim binding failed")

    with pytest.raises(RuntimeError), lockfile_write_scope(LockfileWriter.INSTALL):
        transaction.journal_deploy_writes()
        write_seam.write_text_lf(deployed, "a\n")
        stage_lockfile(lock, "lockfile_version: '1'\n")
        after_lockfile_commit(bind_claims)
        transaction.release_deploy_targets()
    transaction.rollback()

    # The lockfile reached disk and records the deploy, so rollback keeps it.
    assert lock.exists()
    assert deployed.read_bytes() == b"a\n"


def test_a_rolled_back_install_restores_an_mcp_config_it_wrote(
    tmp_path: Path, apm_home: Path
) -> None:
    """#4594: an MCP config written before a later service failure is restored."""
    from apm_cli.adapters.client.config_journal import journal_config_write

    project = tmp_path / "project"
    config = project / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'{"mcpServers": {"mine": {"command": "x"}}}\n')
    created = project / ".gemini" / "settings.json"
    transaction = _transaction(project)
    transaction.journal_deploy_writes()

    journal_config_write(config, '{"mcpServers": {"mine": {}, "apm-srv": {}}}\n')
    config.write_bytes(b'{"mcpServers": {"mine": {}, "apm-srv": {}}}\n')
    journal_config_write(created, "{}\n")
    created.write_bytes(b"{}\n")
    transaction.rollback()

    assert config.read_bytes() == b'{"mcpServers": {"mine": {"command": "x"}}}\n'
    assert not (project / ".gemini").exists()


def _staging_install(project: Path, deployed: Path, key: str):
    """One InstallService run whose pipeline stages *deployed* for the deploy store."""
    from unittest.mock import MagicMock, patch

    from apm_cli.cache.deploy_store import stage
    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService
    from apm_cli.models.results import InstallResult

    transaction = _transaction(project)
    package = MagicMock()
    package.dependencies = {"apm": []}
    request = InstallRequest(
        apm_package=package, transaction=transaction, post_install_after_services=True
    )

    def pipeline(*_args, **_kwargs) -> InstallResult:
        stage(deployed, key)
        return InstallResult()

    with patch("apm_cli.install.pipeline.run_install_pipeline", side_effect=pipeline):
        result = InstallService().run(request)
    return transaction, result


@pytest.mark.parametrize("outcome", ["rolled-back", "committed", "committed-after-a-script"])
def test_deploy_store_evidence_waits_for_the_install_transaction(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from apm_cli.cache.deploy_store import DEPLOY_BUCKET, commit_staged
    from apm_cli.core.invocation_memo import forget_invocation_memos, invocation_scope
    from apm_cli.utils.content_hash import canonical_digest

    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")
    deployed = tmp_path / "AGENTS.md"
    deployed.write_bytes(b"Deployed instructions\n")
    key = canonical_digest(deployed.read_bytes())
    entry = tmp_path / "cache" / DEPLOY_BUCKET / key[:2] / key

    with invocation_scope():
        transaction, result = _staging_install(tmp_path / "project", deployed, key)
        assert not entry.exists(), "stored before the install transaction completed"
        if outcome == "rolled-back":
            # e.g. frozen service reconciliation or a deferred hook fails after the pipeline
            transaction.fail(RuntimeError("service reconciliation failed"))
            assert commit_staged() == 0, "a rolled-back attempt left evidence to publish"
        else:
            if outcome == "committed-after-a-script":
                # A deferred post-install command script drops every invocation memo.
                forget_invocation_memos()
            transaction.complete(result)

    assert entry.exists() is (outcome != "rolled-back")


@pytest.mark.parametrize("outcome", ["rollback", "commit"])
def test_a_legacy_lockfile_migrated_by_the_attempt_follows_its_outcome(
    tmp_path: Path, apm_home: Path, outcome: str
) -> None:
    from apm_cli.deps.lockfile import migrate_lockfile_if_needed
    from apm_cli.models.results import InstallResult

    project = tmp_path / "project"
    project.mkdir()
    legacy, current = project / "apm.lock", project / "apm.lock.yaml"
    legacy.write_bytes(b"lockfile_version: '1'\n")
    transaction = _transaction(project)

    migrate_lockfile_if_needed(project)
    transaction.undo_lockfile_migration_on_rollback(project)
    if outcome == "rollback":
        transaction.rollback()
    else:
        transaction.commit(InstallResult())

    kept, gone = (legacy, current) if outcome == "rollback" else (current, legacy)
    assert kept.read_bytes() == b"lockfile_version: '1'\n"
    assert not gone.exists()


_USER_MCP = b'{\n  "mcpServers": {\n    "mine": {"command": "mine"}\n  }\n}\n'
_APM_MCP = b'{\n  "mcpServers": {\n    "mine": {"command": "mine"},\n    "srv": {"command": "srv"}\n  }\n}\n'


def _apm_killed_write(apm_home: Path, project: Path, path: Path) -> Path:
    """Journal *path*, then let APM write it through its config file records and die."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.integration.config_file_provenance import recorded_config_write

    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(path)
    with recorded_config_write(path, manifest_root=project):
        path.write_bytes(_APM_MCP)
    journal.after_write(path)
    assert journal.store is not None
    return journal.store


def _user_mcp(project: Path) -> Path:
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(_USER_MCP)
    return config


def test_an_mcp_config_the_records_vouch_for_is_restored(tmp_path: Path, apm_home: Path) -> None:
    """#5009: the killed install's recorded write to the user's .mcp.json is undone."""
    project = tmp_path / "project"
    config = _user_mcp(project)
    store = _apm_killed_write(apm_home, project, config)

    outcome = _replay(apm_home, project)

    assert outcome.restored == 1
    assert config.read_bytes() == _USER_MCP
    assert not store.exists()


def test_a_tampered_seal_over_an_edited_mcp_config_keeps_the_journal(
    tmp_path: Path, apm_home: Path
) -> None:
    """A seal rewritten to match the user's later edit is not APM's write: kept."""
    import json

    from apm_cli.install.deploy_snapshot import JournalReplayError, _state

    project = tmp_path / "project"
    config = _user_mcp(project)
    store = _apm_killed_write(apm_home, project, config)
    edited = _APM_MCP.replace(b'"srv"', b'"edited"')
    config.write_bytes(edited)
    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        if "sealed" in line:
            line["state"] = _state(config)
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project)

    assert kept.value.issues == [(".mcp.json", "the deployment ledger does not record these bytes")]
    assert config.read_bytes() == edited


def test_a_shared_config_replaced_by_a_link_keeps_the_journal(
    tmp_path: Path, apm_home: Path
) -> None:
    """Checklist path: a link (whatever it points to) is never restored over."""
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config = _user_mcp(project)
    _apm_killed_write(apm_home, project, config)
    outside = tmp_path / "outside.json"
    outside.write_bytes(_APM_MCP)
    config.unlink()
    try:
        config.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(JournalReplayError):
        _replay(apm_home, project)

    assert config.is_symlink()
    assert outside.read_bytes() == _APM_MCP


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFO support")
def test_a_shared_config_replaced_by_a_fifo_is_kept_and_named(
    tmp_path: Path, apm_home: Path
) -> None:
    """Checklist path: a FIFO swapped in after the kill is a later write, kept unopened."""
    project = tmp_path / "project"
    config = _user_mcp(project)
    _apm_killed_write(apm_home, project, config)
    config.unlink()
    os.mkfifo(config)

    outcome = _replay(apm_home, project)

    assert [rel for rel, _ in outcome.kept] == [".mcp.json"]
    assert config.exists() and not config.is_file()


def test_a_shared_config_replaced_by_a_directory_keeps_the_journal(
    tmp_path: Path, apm_home: Path
) -> None:
    """Checklist path: an (empty) directory swapped in after the kill is never replaced."""
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config = _user_mcp(project)
    _apm_killed_write(apm_home, project, config)
    config.unlink()
    config.mkdir()

    with pytest.raises(JournalReplayError):
        _replay(apm_home, project)

    assert config.is_dir()


def test_a_forged_journal_copy_with_a_matching_hash_keeps_the_journal(
    tmp_path: Path, apm_home: Path
) -> None:
    """The records vouch for the pre-install bytes too: a journal copy swapped, with its
    manifest hash updated to match, is never written into the user's config."""
    import json

    from apm_cli.install.deploy_snapshot import JournalReplayError
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    config = _user_mcp(project)
    store = _apm_killed_write(apm_home, project, config)
    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    unit = next(line for line in lines if line.get("path") == ".mcp.json")
    forged = b'{"mcpServers": {"evil": {"command": "evil"}}}\n'
    (store / unit["copy"]).write_bytes(forged)
    unit["hash"] = compute_file_hash(store / unit["copy"])
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    with pytest.raises(JournalReplayError):
        _replay(apm_home, project)

    assert config.read_bytes() == _APM_MCP


class _Killed(BaseException):
    """A kill: no handler between the write and the process exit runs."""


def _partial_skill(project: Path, rel: str = ".kiro/skills/s") -> tuple[Path, dict[str, str]]:
    """A deployed skill directory of two files and the ledger rows that vouch for it."""
    from apm_cli.utils.content_hash import compute_file_hash

    skill = project / rel
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"skill\n")
    (skill / "reference.md").write_bytes(b"reference\n")
    return skill, {f"{rel}/{p.name}": compute_file_hash(p) for p in skill.iterdir()}


def _kill_partway_through_rmtree(
    monkeypatch: pytest.MonkeyPatch, apm_home: Path, project: Path, remove
) -> None:
    """Run *remove* with the journal listening; its rmtree removes one file, then dies."""
    import shutil

    from apm_cli.install.deploy_snapshot import DeployJournal

    journal = DeployJournal(apm_home / "install-snapshots", root=project)

    def partial(path, *_args, **_kwargs):
        (Path(path) / "SKILL.md").unlink()
        raise _Killed

    def record(path: Path, contents: bool, expected: str | None) -> None:
        journal.record(path, contents=contents, expected=expected)

    write_seam.listen(record, journal.after_write)
    try:
        with monkeypatch.context() as patch, pytest.raises(_Killed):
            patch.setattr(shutil, "rmtree", partial)
            remove()
    finally:
        write_seam.stop_listening(record)


def test_a_kill_partway_through_an_rmtree_rolls_the_directory_back(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5087: a partial directory a journaled rmtree left is APM's: replay rolls it back."""
    project = tmp_path / "project"
    skill, ledger = _partial_skill(project)
    prior = _tree(skill)
    _kill_partway_through_rmtree(monkeypatch, apm_home, project, lambda: write_seam.rmtree(skill))
    assert sorted(p.name for p in skill.iterdir()) == ["reference.md"]

    outcome = _replay(apm_home, project, lambda: ledger)

    assert (outcome.restored, outcome.kept, outcome.retained) == (1, (), ())
    assert _tree(skill) == prior
    assert not (apm_home / "install-snapshots").exists()


def test_a_kill_partway_through_a_skill_path_migration_rolls_it_back(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5087: a legacy skill directory a killed migration left partial is rolled back."""
    from apm_cli.install.skill_path_migration import MigrationPlan, execute_migration

    project = tmp_path / "project"
    legacy, ledger = _partial_skill(project, ".github/skills/s/assets")
    prior = _tree(legacy)
    plan = MigrationPlan(".github/skills/s/assets", ".agents/skills/s/assets", "pkg")

    class _Authorized:
        @staticmethod
        def authorizes(*_args, **_kwargs) -> bool:
            return True

    _kill_partway_through_rmtree(
        monkeypatch,
        apm_home,
        project,
        lambda: execute_migration([plan], object(), project, evidence=_Authorized()),
    )
    assert sorted(p.name for p in legacy.iterdir()) == ["reference.md"]

    outcome = _replay(apm_home, project, lambda: ledger)

    assert (outcome.restored, outcome.kept, outcome.retained) == (1, (), ())
    assert _tree(legacy) == prior
    assert not (apm_home / "install-snapshots").exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda skill: (skill / "reference.md").write_bytes(b"my edit\n"),
        lambda skill: (skill / "mine.md").write_bytes(b"mine\n"),
    ],
    ids=["edited", "added"],
)
def test_a_partial_directory_a_user_changed_after_the_kill_is_kept(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch, change
) -> None:
    """#5087: replay finishes only what APM left; a user's change keeps the directory."""
    project = tmp_path / "project"
    skill, ledger = _partial_skill(project)
    _kill_partway_through_rmtree(monkeypatch, apm_home, project, lambda: write_seam.rmtree(skill))
    change(skill)
    changed = _tree(skill)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == ((".kiro/skills/s", "it changed after the interrupted install"),)
    assert _tree(skill) == changed
    assert not (skill / "SKILL.md").exists()
    assert [store.name.split("-")[0] for store in outcome.retained] == ["retained"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_directory_a_user_made_where_a_killed_unlink_removed_a_file_is_kept(
    tmp_path: Path, apm_home: Path
) -> None:
    """#5087: only a directory APM was removing counts as a partial removal."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    script = project / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"prior\n")
    script.chmod(0o755)
    ledger = {"run.sh": compute_file_hash(script)}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(script, expected="absent")
    script.unlink()
    script.mkdir()
    script.chmod(0o755)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == (("run.sh", "it changed after the interrupted install"),)
    assert script.is_dir()


def test_a_partial_directory_with_an_unlistable_subtree_is_kept(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5087: replay never rolls back a directory it could not fully inspect."""
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    skill, ledger = _partial_skill(project)
    assets = skill / "assets"
    assets.mkdir()
    (assets / "a.md").write_bytes(b"asset\n")
    ledger[".kiro/skills/s/assets/a.md"] = compute_file_hash(assets / "a.md")
    _kill_partway_through_rmtree(monkeypatch, apm_home, project, lambda: write_seam.rmtree(skill))
    (assets / "mine.md").write_bytes(b"mine\n")
    scandir = os.scandir

    def unlistable(path="."):
        if not isinstance(path, int) and os.fspath(path) == str(assets):
            raise PermissionError(13, "Permission denied", str(assets))
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", unlistable)
        outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == ((".kiro/skills/s", "it changed after the interrupted install"),)
    assert (assets / "mine.md").read_bytes() == b"mine\n"


_CONFIG_BYTES = b'{"mcpServers": {"s": {"command": "c"}}}\n'


def _private_config(project: Path) -> tuple[Path, dict[str, str]]:
    from apm_cli.utils.content_hash import compute_file_hash

    config = project / ".mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(_CONFIG_BYTES)
    config.chmod(0o600)
    return config, {".mcp.json": compute_file_hash(config)}


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_replay_never_takes_a_journal_copys_mode(tmp_path: Path, apm_home: Path) -> None:
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    (store / "0").chmod(0o666)  # the copy's bytes are unchanged; only its mode widened

    restored = _replay(apm_home, project, lambda: ledger)

    assert (restored.restored, restored.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES
    assert config.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_replay_never_takes_the_modes_inside_a_journal_copy_directory(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    skill = project / ".kiro" / "skills" / "s"
    skill.mkdir(parents=True)
    skill.chmod(0o750)
    (skill / "SKILL.md").write_bytes(b"prior\n")
    (skill / "SKILL.md").chmod(0o600)
    (skill / "run.sh").write_bytes(b"#!/bin/sh\n")
    (skill / "run.sh").chmod(0o700)
    ledger = {
        f".kiro/skills/s/{name}": compute_file_hash(skill / name) for name in ("SKILL.md", "run.sh")
    }
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    (skill / "SKILL.md").write_bytes(b"killed\n")
    journal.after_write(skill / "SKILL.md")
    assert journal.store is not None
    (journal.store / "0" / "SKILL.md").chmod(0o666)
    (journal.store / "0" / "run.sh").chmod(0o777)
    (journal.store / "0").chmod(0o777)

    restored = _replay(apm_home, project, lambda: ledger)

    assert (restored.restored, restored.kept) == (1, ())
    assert (skill / "SKILL.md").read_bytes() == b"prior\n"
    assert (skill / "SKILL.md").stat().st_mode & 0o7777 == 0o600
    assert (skill / "run.sh").stat().st_mode & 0o7777 == 0o700
    assert skill.stat().st_mode & 0o7777 == 0o750


def _rewrite_unit_mode(store: Path, mode: object) -> None:
    """Set (or, with ``...``, drop) the recorded mode of the journal's first unit."""
    manifest = store / "journal.jsonl"
    lines = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    unit = next(line for line in lines[1:] if "kind" in line)
    unit.pop("mode", None)
    if mode is not ...:
        unit["mode"] = mode
    manifest.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize(
    "mode",
    [..., "0600", -1, 0o10000, 0o4600, True],
    ids=["older-apm-no-mode", "text", "negative", "out-of-range", "setuid", "bool"],
)
def test_a_unit_without_a_verifiable_mode_is_kept_and_reported(
    tmp_path: Path, apm_home: Path, mode: object
) -> None:
    """Downgrade safety: a unit line an older apm wrote (no mode), or one whose mode is
    not valid permission bits, is never restored with guessed permissions."""
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    _rewrite_unit_mode(store, mode)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert kept.value.issues == [(".mcp.json", "the journal does not record a verifiable mode")]
    assert config.read_bytes() == b"killed\n"


def _killed_skill(apm_home: Path, project: Path, mode: int):
    """A skill directory with mode *mode*, journaled, then written by a killed install."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    skill = project / ".kiro" / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"prior\n")
    (skill / "SKILL.md").chmod(0o600)
    ledger = {".kiro/skills/s/SKILL.md": compute_file_hash(skill / "SKILL.md")}
    skill.chmod(mode)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    skill.chmod(0o750)
    (skill / "SKILL.md").write_bytes(b"killed\n")
    journal.after_write(skill / "SKILL.md")
    journal.after_write(skill)
    assert journal.store is not None
    return skill, ledger, journal.store


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_users_mode_change_after_the_kill_is_kept_whatever_the_copy_says(
    tmp_path: Path, apm_home: Path
) -> None:
    """The keep-or-restore decision compares against the recorded mode, never the copy's:
    a copy whose mode matches the user's post-kill change does not make it APM's."""
    skill, ledger, store = _killed_skill(apm_home, tmp_path / "project", 0o750)
    skill.chmod(0o700)  # the user's own change after the kill
    (store / "0").chmod(0o700)  # the copy now claims that mode

    restored = _replay(apm_home, tmp_path / "project", lambda: ledger)

    assert restored.kept == ((".kiro/skills/s", "it changed after the interrupted install"),)
    assert skill.stat().st_mode & 0o7777 == 0o700
    assert (skill / "SKILL.md").read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_read_only_directory_is_restored_with_its_recorded_mode(
    tmp_path: Path, apm_home: Path
) -> None:
    skill, ledger, _store = _killed_skill(apm_home, tmp_path / "project", 0o550)

    restored = _replay(apm_home, tmp_path / "project", lambda: ledger)

    assert (restored.restored, restored.kept) == (1, ())
    assert (skill / "SKILL.md").read_bytes() == b"prior\n"
    assert skill.stat().st_mode & 0o7777 == 0o550
    skill.chmod(0o755)  # let pytest clean up


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_directory_mode_is_never_applied_through_a_link(tmp_path: Path) -> None:
    """The restored directory swapped for a link before its own mode is applied: the
    link's target keeps its mode and the restore fails (kept and reported)."""
    from apm_cli.install.journal_modes import apply_own_mode

    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o700)
    restored = tmp_path / "project" / "skill"
    restored.parent.mkdir()
    restored.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        apply_own_mode(restored, {"": 0o777})

    assert outside.stat().st_mode & 0o7777 == 0o700
    assert restored.is_symlink()


def _header(store: Path) -> dict:
    return json.loads((store / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])


def test_a_journal_with_recorded_modes_is_refused_by_older_readers(
    tmp_path: Path, apm_home: Path
) -> None:
    """An older apm replays only format version 1 journals, and it would restore a
    copy's mode bits: a journal that records modes is a version it refuses."""
    project = tmp_path / "project"
    config, _ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)

    header = _header(store)
    assert (store / "journal.jsonl").read_text(encoding="utf-8").startswith("{")
    assert header["format"] == "apm-deploy-journal"
    assert header["version"] != 1


def test_an_older_apms_journal_is_kept_and_reported(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    manifest = store / "journal.jsonl"
    lines = manifest.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps({**json.loads(lines[0]), "version": 1})
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(JournalReplayError):
        _replay(apm_home, project, lambda: ledger)

    assert config.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_sticky_directory_is_kept_not_restored_without_its_sticky_bit(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    skill, ledger, _store = _killed_skill(apm_home, tmp_path / "project", 0o1777)
    skill.chmod(0o1777)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, tmp_path / "project", lambda: ledger)

    assert kept.value.issues == [
        (".kiro/skills/s", "the journal does not record a verifiable mode")
    ]
    assert (skill / "SKILL.md").read_bytes() == b"killed\n"
    assert skill.stat().st_mode & 0o7777 == 0o1777


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_recorded_mode_wider_than_the_path_has_now_is_kept_and_reported(
    tmp_path: Path, apm_home: Path
) -> None:
    """The recorded mode is journal data: replay never widens a path beyond the mode it
    has now, even for valid permission bits (#5134)."""
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    _rewrite_unit_mode(store, 0o666)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == ((".mcp.json", "its recorded mode is wider than it has now"),)
    assert config.read_bytes() == b"killed\n"
    assert config.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_path_that_is_now_a_link_is_refused_and_its_target_untouched(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    _killed_journal(apm_home, project, config)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    outside.chmod(0o600)
    config.unlink()
    config.symlink_to(outside)

    from apm_cli.install.deploy_snapshot import JournalReplayError

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert [path for path, _reason in kept.value.issues] == [".mcp.json"]
    assert config.is_symlink()
    assert outside.read_bytes() == b"{}\n"
    assert outside.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_an_entry_the_path_cannot_reach_now_is_kept_not_a_crash(
    tmp_path: Path, apm_home: Path
) -> None:
    """A sealed write turned a journaled entry's parent directory into a file: the
    entry's mode cannot be bounded, so the unit is kept and reported."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    skill = project / ".kiro" / "skills" / "s"
    (skill / "ref").mkdir(parents=True)
    (skill / "ref" / "notes.md").write_bytes(b"prior\n")
    ledger = {".kiro/skills/s/ref/notes.md": compute_file_hash(skill / "ref" / "notes.md")}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    (skill / "ref" / "notes.md").unlink()
    (skill / "ref").rmdir()
    (skill / "ref").write_bytes(b"now a file\n")
    (skill / "ref").chmod(0o777)  # no narrower than the directory it replaced
    journal.after_write(skill)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert [rel for rel, _reason in outcome.kept] == [".kiro/skills/s"]
    assert (skill / "ref").read_bytes() == b"now a file\n"


@pytest.mark.skipif(os.name != "nt", reason="the Windows read-only flag")
def test_on_windows_only_clearing_a_read_only_flag_is_wider(tmp_path: Path, apm_home: Path) -> None:
    """Windows reports coarse modes (0o666 or 0o444 for a file): a restore is refused only
    when it would clear a read-only flag the path has now, so a removed file restores."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(config)
    config.unlink()
    journal.after_write(config)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES


def _set_restrictive_acl(path: Path) -> None:
    """Give *path* a POSIX ACL (user rw, a named user r, group none), or skip."""
    import struct

    undefined = 0xFFFFFFFF
    entries = [(0x01, 6, undefined), (0x02, 4, 65534), (0x04, 0, undefined)]
    entries += [(0x10, 4, undefined), (0x20, 0, undefined)]
    data = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(path, "system.posix_acl_access", data)
    except OSError as exc:
        pytest.skip(f"POSIX ACLs unavailable here: {exc}")


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux extended attributes")
def test_a_path_that_carries_a_posix_acl_is_kept_not_restored_without_it(
    tmp_path: Path, apm_home: Path
) -> None:
    """Restored bytes are staged with permission bits only, so a restrictive ACL would be
    lost: such a path is kept and reported instead."""
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    _set_restrictive_acl(config)
    _killed_journal(apm_home, project, config)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert outcome.kept == ((".mcp.json", "it carries a POSIX ACL"),)
    assert config.read_bytes() == b"killed\n"
    assert "system.posix_acl_access" in os.listxattr(config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_restored_directory_is_never_published_with_staging_permissions(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename that publishes a restored directory already sees its recorded mode."""
    skill, ledger, _store = _killed_skill(apm_home, tmp_path / "project", 0o550)
    published: list[int] = []
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if Path(destination) == skill:
            published.append(os.lstat(source).st_mode & 0o7777)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    restored = _replay(apm_home, tmp_path / "project", lambda: ledger)
    monkeypatch.undo()

    assert (restored.restored, restored.kept) == (1, ())
    assert published == [0o550]
    assert skill.stat().st_mode & 0o7777 == 0o550
    skill.chmod(0o755)  # let pytest clean up


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="Linux extended attributes")
def test_a_path_whose_extended_attributes_cannot_be_read_is_kept(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACL that cannot be checked is not assumed absent: the unit is kept."""
    import errno

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    _killed_journal(apm_home, project, config)

    def unreadable(path, *args, **kwargs):
        raise OSError(errno.EACCES, "permission denied", str(path))

    monkeypatch.setattr(os, "listxattr", unreadable)
    outcome = _replay(apm_home, project, lambda: ledger)
    monkeypatch.undo()

    assert outcome.kept == ((".mcp.json", "it carries a POSIX ACL"),)
    assert config.read_bytes() == b"killed\n"


def _set_default_acl(directory: Path) -> None:
    """Give *directory* a default POSIX ACL granting a named user read, or skip."""
    import struct

    undefined = 0xFFFFFFFF
    entries = [(0x01, 7, undefined), (0x02, 4, 65534), (0x04, 0, undefined)]
    entries += [(0x10, 4, undefined), (0x20, 0, undefined)]
    data = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(directory, "system.posix_acl_default", data)
    except OSError as exc:
        pytest.skip(f"POSIX ACLs unavailable here: {exc}")


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="Linux extended attributes")
def test_a_restore_that_would_inherit_a_default_acl_is_not_published(
    tmp_path: Path, apm_home: Path
) -> None:
    """The staged restore inherits its parent's default ACL: it is never published."""
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    _killed_journal(apm_home, project, config)
    _set_default_acl(project)

    with pytest.raises(JournalReplayError):
        _replay(apm_home, project, lambda: ledger)

    assert config.read_bytes() == b"killed\n"
    assert "system.posix_acl_access" not in os.listxattr(config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_a_file_mode_is_never_applied_through_a_link(tmp_path: Path) -> None:
    """The staged file swapped for a link before its mode is applied: the link's target
    keeps its mode and the restore fails (kept and reported)."""
    from apm_cli.install.journal_modes import apply_entry_modes

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x\n")
    outside.chmod(0o400)
    staged = tmp_path / "project" / ".apm-replay-staged"
    staged.parent.mkdir()
    staged.symlink_to(outside)

    with pytest.raises(OSError):
        apply_entry_modes(staged, 0o600)

    assert outside.stat().st_mode & 0o7777 == 0o400
    assert staged.is_symlink()


# -- replay trusts only a store private to this user; recreated modes honor the umask --
# (#5134) A snapshot store, and each journal file in it, must be a real directory or
# regular file owned by this user with no group or other write; otherwise every path in
# it is kept and reported. A path recreated from absence never gets more than the cap
# (0o644 file, 0o755 directory) narrowed by the current umask.

_NOT_PRIVATE = "the snapshot store is not private to this user; nothing was restored"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner and mode bits")
def test_a_store_others_can_write_is_kept_and_reported(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    store.chmod(0o775)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert [reason for _path, reason in kept.value.issues] == [_NOT_PRIVATE]
    assert config.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner and mode bits")
def test_a_store_another_user_owns_is_kept_and_reported(
    tmp_path: Path, apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    _killed_journal(apm_home, project, config)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(config).st_uid + 1)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert [reason for _path, reason in kept.value.issues] == [_NOT_PRIVATE]
    assert config.read_bytes() == b"killed\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_journal_that_is_a_link_is_kept_and_reported(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import JournalReplayError

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    planted = tmp_path / "planted.jsonl"
    planted.write_bytes((store / "journal.jsonl").read_bytes())
    (store / "journal.jsonl").unlink()
    (store / "journal.jsonl").symlink_to(planted)

    with pytest.raises(JournalReplayError) as kept:
        _replay(apm_home, project, lambda: ledger)

    assert [reason for _path, reason in kept.value.issues] == [_NOT_PRIVATE]
    assert config.read_bytes() == b"killed\n"


def _with_umask(mask: int):
    """Set the umask and forget the memoized read, so replay sees *mask*."""
    from apm_cli.cache.deploy_store import process_umask

    previous = os.umask(mask)
    process_umask.cache_clear()
    return previous


def _restore_umask(previous: int) -> None:
    from apm_cli.cache.deploy_store import process_umask

    os.umask(previous)
    process_umask.cache_clear()


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_a_file_recreated_from_absence_honors_the_umask(tmp_path: Path, apm_home: Path) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal

    project = tmp_path / "project"
    config, ledger = _private_config(project)
    config.chmod(0o644)
    ledger = {".mcp.json": ledger[".mcp.json"]}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(config)
    config.unlink()
    journal.after_write(config)

    previous = _with_umask(0o077)
    try:
        outcome = _replay(apm_home, project, lambda: ledger)
    finally:
        _restore_umask(previous)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES
    assert config.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_a_directory_recreated_from_absence_honors_the_umask(
    tmp_path: Path, apm_home: Path
) -> None:
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    project = tmp_path / "project"
    skill = project / ".kiro" / "skills" / "s"
    skill.mkdir(parents=True)
    skill.chmod(0o755)
    (skill / "SKILL.md").write_bytes(b"prior\n")
    (skill / "SKILL.md").chmod(0o644)
    ledger = {".kiro/skills/s/SKILL.md": compute_file_hash(skill / "SKILL.md")}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    (skill / "SKILL.md").unlink()
    skill.rmdir()
    journal.after_write(skill)

    previous = _with_umask(0o077)
    try:
        outcome = _replay(apm_home, project, lambda: ledger)
    finally:
        _restore_umask(previous)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert skill.stat().st_mode & 0o7777 == 0o700
    assert (skill / "SKILL.md").stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_planted_wider_mode_never_widens_a_recorded_0o644_file(
    tmp_path: Path, apm_home: Path
) -> None:
    """W3d: a journal whose unit line claims 0o755 for a file apm recorded as 0o644."""
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    config.chmod(0o644)
    store = _killed_journal(apm_home, project, config)
    _rewrite_unit_mode(store, 0o755)

    _replay(apm_home, project, lambda: ledger)

    assert config.stat().st_mode & 0o7777 == 0o644


@pytest.mark.xfail(
    strict=True,
    reason="#5134: acceptance cell for the optional journal seal; a same-user plant in a "
    "private store passes the store check, and a narrower mode passes the bound",
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_valid_but_different_recorded_mode_is_kept_and_reported(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    store = _killed_journal(apm_home, project, config)
    _rewrite_unit_mode(store, 0o400)

    outcome = _replay(apm_home, project, lambda: ledger)

    assert [rel for rel, _reason in outcome.kept] == [".mcp.json"]
    assert config.read_bytes() == b"killed\n"
    assert config.stat().st_mode & 0o7777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_a_store_created_under_a_group_writable_umask_is_still_replayed(
    tmp_path: Path, apm_home: Path
) -> None:
    """The journal creates its store privately, so a 0o002 umask never makes it untrusted."""
    project = tmp_path / "project"
    config, ledger = _private_config(project)
    previous = os.umask(0o002)
    try:
        _killed_journal(apm_home, project, config)
        outcome = _replay(apm_home, project, lambda: ledger)
    finally:
        os.umask(previous)

    assert (outcome.restored, outcome.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES
