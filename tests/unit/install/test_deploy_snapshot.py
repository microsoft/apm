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
        replay_abandoned_journals(apm_home / "install-snapshots", project, dict)

    assert kept.value.issues == [("a.md", "the deployment ledger does not record these bytes")]
    assert receipt.read_bytes() == deployed.read_bytes() == b"killed\n"


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
    DeployJournal(apm_home / "install-snapshots", root=project).record(hook)
    hook.write_bytes(b"#!/bin/sh\necho killed\n")
    hook.chmod(0o644)

    restored = replay_abandoned_journals(apm_home / "install-snapshots", project, lambda: ledger)

    assert restored == 1
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
    replay_abandoned_journals(apm_home / "install-snapshots", tmp_path, dict)

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
    from apm_cli.adapters.client.base import journal_config_write

    project = tmp_path / "project"
    config = project / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'{"mcpServers": {"mine": {"command": "x"}}}\n')
    created = project / ".gemini" / "settings.json"
    transaction = _transaction(project)
    transaction.journal_deploy_writes()

    journal_config_write(config)
    config.write_bytes(b'{"mcpServers": {"mine": {}, "apm-srv": {}}}\n')
    journal_config_write(created)
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
