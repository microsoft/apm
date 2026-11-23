"""Lazy deploy journal: only touched paths are kept, and rollback restores them (#4282)."""

from __future__ import annotations

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
    hook.chmod(0o755)
    ledger = {".github/hooks/run.sh": compute_file_hash(hook)}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(hook)
    hook.write_bytes(b"#!/bin/sh\necho killed\n")
    hook.chmod(0o644)
    journal.after_write(hook)

    restored = replay_abandoned_journals(
        apm_home / "install-snapshots", project, lambda: ledger, receipts=_receipts(apm_home)
    )

    assert (restored.restored, restored.kept) == (1, ())
    assert hook.read_bytes() == b"#!/bin/sh\necho prior\n"
    assert hook.stat().st_mode & 0o777 == 0o755
    assert not any((apm_home / "install-snapshots").iterdir())


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
    created.parent.mkdir(parents=True)
    created.write_bytes(b"{}\n")
    transaction.rollback()

    assert config.read_bytes() == b'{"mcpServers": {"mine": {"command": "x"}}}\n'
    assert not (project / ".gemini").exists()


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
