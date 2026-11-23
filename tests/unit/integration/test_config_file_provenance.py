"""Config file provenance: the single owner of deleting a config file APM emptied (#4867).

PROMISE: P-LAW-ownership.preserve_unowned -- a native MCP or hook config file
APM did not create is never deleted, empty or not (source:
src/apm_cli/integration/config_file_provenance.py).

Q3: a provenance record alone never deletes. A file goes only when (a) the
record says APM created it, (b) after cleanup it holds only APM's empty
skeleton, and (c) the record store is local state outside any VCS-tracked
path. Q4: records are keyed by the resolved file, one cell per path class.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from pathlib import Path

import pytest

from apm_cli.integration.config_file_provenance import (
    Origin,
    config_file_key,
    config_file_origin,
    delete_emptied_config,
    recorded_config_write,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-ownership.preserve_unowned")]

_SKELETON = '{\n  "mcpServers": {}\n}\n'
_WITH_ENTRY = '{\n  "mcpServers": {\n    "srv": {"command": "srv"}\n  }\n}\n'


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home" / ".apm"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


def _apm_writes(path: Path, text: str, manifest_root: Path | None = None) -> None:
    """APM writes *path* through the recorded write, as the integrators do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with recorded_config_write(path, manifest_root=manifest_root or path.parent):
        path.write_text(text, encoding="utf-8")


def _apm_empties(path: Path, *, root: Path | None) -> bool:
    """APM retires its last entry: the owner decides whether the skeleton goes."""
    return delete_emptied_config(path, root=root, empty=True)


# -- Q3: every condition must hold ------------------------------------------------


def test_a_file_apm_created_and_emptied_is_removed(tmp_path, apm_home) -> None:
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    assert config_file_origin(config) is Origin.CREATED

    assert _apm_empties(config, root=tmp_path / "project") is True
    assert not config.exists()
    assert config_file_origin(config) is None, "the record leaves with the file"


def test_an_empty_file_the_user_created_is_kept(tmp_path, apm_home) -> None:
    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")  # the user's own skeleton
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)  # APM merges into it
    assert config_file_origin(config) is Origin.PREEXISTING
    before = config.read_bytes()

    assert _apm_empties(config, root=project) is False
    assert config.read_bytes() == before


def test_a_created_file_that_still_holds_content_is_never_removed(tmp_path, apm_home) -> None:
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert delete_emptied_config(config, root=tmp_path / "project", empty=False) is False
    assert config.is_file()


def test_a_record_in_a_vcs_tracked_store_never_deletes(tmp_path, monkeypatch) -> None:
    """(c): a record a clone could ship is not evidence."""
    home = tmp_path / "dotfiles"
    (home / ".git").mkdir(parents=True)
    monkeypatch.setenv("APM_HOME", str(home / ".apm"))
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert _apm_empties(config, root=tmp_path / "project") is False
    assert config.is_file()


def test_a_stale_created_record_never_claims_a_file_the_user_recreated(tmp_path, apm_home) -> None:
    """APM created the file, it went away (for example a rolled-back install), and the
    user created their own: the stale record must not make it APM's."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    config.unlink()
    config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.is_file()


def test_a_file_without_a_record_keeps_the_content_proof(tmp_path, apm_home) -> None:
    """A file written before provenance existed keeps today's behaviour (#4852)."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    assert config_file_origin(config) is None

    assert _apm_empties(config, root=project) is True
    assert not config.exists()


def test_a_pre_provenance_install_records_its_existing_files_as_legacy(tmp_path, apm_home) -> None:
    """The first recorded write into an unrecorded file of an installation that already
    has a lockfile cannot tell APM's file from the user's: today's content proof stays."""
    project = tmp_path / "project"
    (project / "apm.lock.yaml").parent.mkdir(parents=True)
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="utf-8")
    config = project / ".mcp.json"
    config.write_text(_WITH_ENTRY, encoding="utf-8")  # written by an older APM
    _apm_writes(config, _WITH_ENTRY.replace("srv", "srv2"), manifest_root=project)

    assert config_file_origin(config) is Origin.LEGACY
    assert _apm_empties(config, root=project) is True
    assert not config.exists()


def test_an_empty_skeleton_in_an_installation_with_a_lockfile_is_the_users(
    tmp_path, apm_home
) -> None:
    """An install that wrote a lockfile but no config file yet (MCP not approved) meets
    the user's empty skeleton: it holds nothing APM wrote, so it stays the user's."""
    project = tmp_path / "project"
    (project / "apm.lock.yaml").parent.mkdir(parents=True)
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="utf-8")
    config = project / ".mcp.json"
    config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.is_file()


# -- Q4: provenance keyed by the resolved file --------------------------------------


def test_a_profile_path_with_spaces_and_non_ascii_keeps_its_record(tmp_path, apm_home) -> None:
    """(1) COPILOT_HOME with spaces and non-ASCII characters."""
    config = tmp_path / "My Profile \u00e9" / "mcp-config.json"
    _apm_writes(config, _WITH_ENTRY)

    assert config_file_origin(config) is Origin.CREATED
    assert _apm_empties(config, root=tmp_path) is True
    assert not config.exists()


def test_a_missing_file_is_keyed_through_its_resolved_parent(tmp_path, apm_home) -> None:
    """(2) A file not created yet resolves through its parent: the link and the real
    directory name one record."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert config_file_key(alias / ".mcp.json") == config_file_key(real / ".mcp.json")
    _apm_writes(alias / ".mcp.json", _WITH_ENTRY)
    assert config_file_origin(real / ".mcp.json") is Origin.CREATED
    assert _apm_empties(real / ".mcp.json", root=real) is True
    assert not (real / ".mcp.json").exists()


def test_a_config_file_symlinked_outside_is_never_removed_or_followed(tmp_path, apm_home) -> None:
    """(3) Never touch a symlink's target, whatever the record says."""
    project = tmp_path / "project"
    outside = tmp_path / "outside.json"
    _apm_writes(outside, _SKELETON)
    project.mkdir()
    link = project / ".mcp.json"
    link.symlink_to(outside)

    assert _apm_empties(link, root=project) is False
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == _SKELETON


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFO support")
def test_a_fifo_at_the_config_path_is_never_removed(tmp_path, apm_home) -> None:
    """(3) A FIFO is not a config file: kept, and never opened (no hang)."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    config.unlink()
    os.mkfifo(config)

    assert _apm_empties(config, root=project) is False
    assert config.exists() and not config.is_file()


def _nfc_nfd(tmp_path: Path) -> tuple[Path, Path, bool]:
    """Both spellings of one directory name, and whether the file system aliases them."""
    nfc = tmp_path / unicodedata.normalize("NFC", "caf\u00e9")
    nfd = tmp_path / unicodedata.normalize("NFD", "caf\u00e9")
    nfc.mkdir()
    return nfc, nfd, nfd.exists()


def test_nfc_and_nfd_directories_that_are_distinct_keep_independent_records(
    tmp_path, apm_home
) -> None:
    """(4) On a normalization-sensitive file system (Linux) the two spellings are two
    files: APM's record of one never speaks for the user's file in the other."""
    nfc, nfd, aliased = _nfc_nfd(tmp_path)
    if aliased:
        pytest.skip("normalization-insensitive file system: the spellings are one file")
    nfd.mkdir()
    _apm_writes(nfc / "x.json", _WITH_ENTRY)
    (nfd / "x.json").write_text(_SKELETON, encoding="utf-8")  # the user's own file
    _apm_writes(nfd / "x.json", _WITH_ENTRY, manifest_root=tmp_path)

    assert config_file_key(nfc / "x.json") != config_file_key(nfd / "x.json")
    assert config_file_origin(nfc / "x.json") is Origin.CREATED
    assert config_file_origin(nfd / "x.json") is Origin.PREEXISTING
    assert _apm_empties(nfd / "x.json", root=tmp_path) is False
    assert (nfd / "x.json").is_file()


def test_an_nfd_spelling_finds_the_record_on_a_normalizing_file_system(tmp_path, apm_home) -> None:
    """(4) On macOS the NFD spelling of a profile directory is the same file."""
    nfc, nfd, aliased = _nfc_nfd(tmp_path)
    if not aliased:
        pytest.skip("normalization-sensitive file system: the spellings are two files")
    _apm_writes(nfc / "x.json", _WITH_ENTRY)

    assert config_file_origin(nfd / "x.json") is Origin.CREATED
    assert _apm_empties(nfd / "x.json", root=tmp_path) is True
    assert not (nfc / "x.json").exists()


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_a_differently_cased_spelling_finds_the_record_on_a_case_insensitive_fs(
    tmp_path, apm_home
) -> None:
    """(4) On a case-insensitive file system another case spelling is the same file."""
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive file system: another case is another file")
    config = tmp_path / "Project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert config_file_origin(tmp_path / "project" / ".mcp.json") is Origin.CREATED
    assert _apm_empties(tmp_path / "project" / ".mcp.json", root=tmp_path) is True
    assert not config.exists()


def test_traversal_in_a_config_path_names_the_resolved_file(tmp_path, apm_home) -> None:
    """(5) ``..`` in a config or profile path resolves to the one file it names."""
    config = tmp_path / "project" / ".mcp.json"
    (tmp_path / "project" / "sub").mkdir(parents=True)
    _apm_writes(tmp_path / "project" / "sub" / ".." / ".mcp.json", _WITH_ENTRY)

    assert config_file_origin(config) is Origin.CREATED
    assert _apm_empties(config, root=tmp_path / "project") is True
    assert not config.exists()


def test_a_tampered_record_with_a_relative_or_traversing_path_is_ignored(
    tmp_path, apm_home
) -> None:
    """(5) A record whose key is not an absolute resolved path proves nothing."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)  # recorded as the user's
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    created = {"origin": "created"}
    data["files"] += [
        {"path": "project/.mcp.json", **created},
        {"path": f"{project.as_posix()}/sub/../.mcp.json", **created},
    ]
    store.write_text(json.dumps(data), encoding="utf-8")

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_an_unreadable_record_store_keeps_every_file(tmp_path, apm_home) -> None:
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    (apm_home / "deployment-receipts" / "config-files" / "records.json").write_text(
        "{", encoding="utf-8"
    )

    assert _apm_empties(config, root=tmp_path / "project") is False
    assert config.is_file()


def test_a_record_store_linked_into_a_vcs_tree_never_deletes(tmp_path, apm_home) -> None:
    """(c): a receipts directory that links into a checkout is not local evidence."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "receipts").mkdir()
    apm_home.mkdir(parents=True)
    (apm_home / "deployment-receipts").symlink_to(checkout / "receipts", target_is_directory=True)
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert _apm_empties(config, root=tmp_path / "project") is False
    assert config.is_file()


def test_a_created_file_replaced_with_other_bytes_is_kept(tmp_path, apm_home) -> None:
    """(b): the record proves APM's bytes only; a file the user replaced (same entries,
    other bytes) is no longer the file APM created."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    config.write_text(json.dumps(json.loads(_WITH_ENTRY)), encoding="utf-8")

    assert _apm_empties(config, root=project) is False
    assert config.is_file()


def test_a_scratch_render_records_nothing(tmp_path, apm_home) -> None:
    """Audit and cleanup replays render into private scratch: no durable record."""
    from apm_cli.integration.config_file_provenance import replay_render

    config = tmp_path / "scratch" / ".mcp.json"
    with replay_render():
        _apm_writes(config, _WITH_ENTRY)

    assert config.is_file()
    assert not (apm_home / "deployment-receipts" / "config-files").exists()
    assert config_file_origin(config) is None


def test_a_converged_write_leaves_the_record_store_untouched(tmp_path, apm_home) -> None:
    """I2: a repeat install that changes no config bytes rewrites no record."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    before = (store.stat().st_ino, store.read_bytes())

    _apm_writes(config, _WITH_ENTRY, manifest_root=project)

    assert (store.stat().st_ino, store.read_bytes()) == before
    assert config_file_origin(config) is Origin.CREATED


def test_a_replaced_file_stays_the_users_through_later_apm_writes(tmp_path, apm_home) -> None:
    """(b): the user replaced APM's file with other bytes holding the same entries; a
    later APM rewrite must not make the replacement APM's again, so retirement keeps it."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    config.write_text(json.dumps(json.loads(_WITH_ENTRY)), encoding="utf-8")  # replaced
    _apm_writes(config, _WITH_ENTRY.replace("srv", "srv2"), manifest_root=project)

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.is_file()


def test_an_empty_record_store_leaves_no_file(tmp_path, apm_home) -> None:
    """Like the other receipts, a store with no records is removed, so a world that
    never kept a record and one whose records all left hold the same state."""
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    assert _apm_empties(config, root=tmp_path / "project") is True

    assert not (apm_home / "deployment-receipts" / "config-files").exists() or not any(
        (apm_home / "deployment-receipts" / "config-files").iterdir()
    )


def test_a_record_of_a_temporarily_absent_file_survives_other_saves(tmp_path, apm_home) -> None:
    """The store is shared: saving project B while project A's file is absent (an
    unmounted drive) keeps A's record, so A's file is still the user's when it returns."""
    a_config = tmp_path / "a" / ".mcp.json"
    a_config.parent.mkdir()
    a_config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(a_config, _WITH_ENTRY, manifest_root=tmp_path / "a")
    held = a_config.read_bytes()
    a_config.unlink()  # the drive is away
    _apm_writes(tmp_path / "b" / ".mcp.json", _WITH_ENTRY)  # project B saves the store
    a_config.write_bytes(held)  # the drive is back

    assert config_file_origin(a_config) is Origin.PREEXISTING
    assert _apm_empties(a_config, root=tmp_path / "a") is False
    assert a_config.read_bytes() == held


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_users_file_is_not_written_when_its_record_cannot_be_saved(tmp_path, apm_home) -> None:
    """Without a durable record a user's file would later look legacy and be deleted:
    APM refuses the write instead, leaving the file as it was."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    apm_home.mkdir(parents=True)
    (apm_home / "deployment-receipts").write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError, match="refusing to write"):
        _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    assert config.read_text(encoding="utf-8") == _SKELETON
