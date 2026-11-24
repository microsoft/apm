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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apm_cli.integration.config_file_provenance import (
    Origin,
    config_file_key,
    config_file_origin,
    delete_emptied_config,
    recorded_config_write,
    vouches_for_current_bytes,
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
    store = _holding(apm_home, config)
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
    before = _store_files(apm_home)

    _apm_writes(config, _WITH_ENTRY, manifest_root=project)

    assert _store_files(apm_home) == before
    assert [path.read_bytes() for path in before] == [state[2] for state in before.values()]
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows collapses '..' before following links: the write and its key both "
    "name the file beside the link",
)
def test_dotdot_after_a_linked_directory_names_the_file_the_write_reaches(
    tmp_path, apm_home
) -> None:
    """(5) ``link/..`` follows the link first, as the file system does: the record goes to
    the file the write reached, never to the user's file beside the link."""
    project = tmp_path / "project"
    (tmp_path / "other" / "child").mkdir(parents=True)
    project.mkdir()
    (project / "link").symlink_to(tmp_path / "other" / "child", target_is_directory=True)
    users = project / ".mcp.json"
    users.write_text(_WITH_ENTRY, encoding="utf-8")

    _apm_writes(project / "link" / ".." / ".mcp.json", _WITH_ENTRY)

    assert (tmp_path / "other" / ".mcp.json").is_file()
    assert config_file_origin(tmp_path / "other" / ".mcp.json") is Origin.CREATED
    assert config_file_origin(users) is None
    assert users.read_text(encoding="utf-8") == _WITH_ENTRY


def test_empty_hook_event_scaffolding_with_a_lockfile_is_the_users(tmp_path, apm_home) -> None:
    """An event list with no hooks holds nothing APM wrote: the user's skeleton."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="utf-8")
    settings = project / ".claude" / "settings.json"
    settings.write_text('{"hooks": {"SessionStart": []}}\n', encoding="utf-8")
    _apm_writes(settings, '{"hooks": {"SessionStart": [{"hooks": []}]}}\n', manifest_root=project)

    assert config_file_origin(settings) is Origin.PREEXISTING
    assert _apm_empties(settings, root=project) is False
    assert settings.is_file()


def _atomic_write(path: Path, text: str) -> None:
    """Replace *path* as APM's atomic writers do: a new file renamed over it."""
    staged = path.with_name(path.name + ".staged")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


def test_a_users_origin_survives_an_atomic_rewrite_of_a_hardlinked_path(tmp_path, apm_home) -> None:
    """The user's record under another hard link keeps the rewritten path the user's:
    the origin is decided before the write, not looked up after the link breaks."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\n", encoding="utf-8")
    first = project / "first.json"
    first.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(first, _WITH_ENTRY, manifest_root=project)  # the user's file, merged into
    assert config_file_origin(first) is Origin.PREEXISTING
    second = project / ".mcp.json"
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hard links are unavailable")

    with recorded_config_write(second, manifest_root=project):
        _atomic_write(second, _WITH_ENTRY.replace("srv", "srv2"))

    assert config_file_origin(second) is Origin.PREEXISTING
    assert _apm_empties(second, root=project) is False
    assert second.is_file() and config_file_origin(first) is Origin.PREEXISTING


def test_a_new_config_never_probes_unrelated_records(
    tmp_path, apm_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a new config costs no identity checks against other records,
    however many the machine holds: a file APM creates is never looked up."""
    for index in range(20):
        _apm_writes(tmp_path / f"p{index}" / f"other-{index}.json", _WITH_ENTRY)
    calls: list[tuple] = []
    real = os.path.samefile
    monkeypatch.setattr(os.path, "samefile", lambda a, b: calls.append((a, b)) or real(a, b))

    config = tmp_path / "new" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert calls == []
    assert config_file_origin(config) is Origin.CREATED
    assert config.is_file()


def test_a_record_of_a_file_linked_for_now_survives_other_saves(tmp_path, apm_home) -> None:
    """A stored key is checked as written, never re-resolved: project A's config, a
    link for a while, keeps its record through project B's saves."""
    a_config = tmp_path / "a" / ".mcp.json"
    a_config.parent.mkdir()
    a_config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(a_config, _WITH_ENTRY, manifest_root=tmp_path / "a")
    held = a_config.read_bytes()
    a_config.unlink()
    a_config.symlink_to(tmp_path / "elsewhere.json")
    _apm_writes(tmp_path / "b" / ".mcp.json", _WITH_ENTRY)  # project B saves the store
    a_config.unlink()
    a_config.write_bytes(held)

    assert config_file_origin(a_config) is Origin.PREEXISTING
    assert _apm_empties(a_config, root=tmp_path / "a") is False
    assert a_config.read_bytes() == held


# -- a cleanup rewrite of an unrecorded file records it as legacy (#5038) ---------------


def test_a_cleanup_rewrite_of_an_unrecorded_file_records_it_as_legacy(tmp_path, apm_home) -> None:
    """MCP and hook cleanup pass no manifest root. A file an older APM wrote into still
    gets a record, so crash recovery can verify the cleanup's write and undo it."""
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_WITH_ENTRY, encoding="utf-8")  # written by an older APM
    before = config.read_bytes()
    with recorded_config_write(config):  # cleanup removes one entry
        config.write_text(_WITH_ENTRY.replace("srv", "kept"), encoding="utf-8")

    assert config_file_origin(config) is Origin.LEGACY
    assert vouches_for_current_bytes(config, before) is True
    assert vouches_for_current_bytes(config, b"{}") is False


def test_a_cleanup_rewrite_of_an_unrecorded_skeleton_records_nothing(tmp_path, apm_home) -> None:
    """Without entries before the write there is no content proof of an older APM."""
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    before = config.read_bytes()
    with recorded_config_write(config):
        config.write_text(_SKELETON.replace("{}", "{ }"), encoding="utf-8")

    assert config_file_origin(config) is None
    assert vouches_for_current_bytes(config, before) is False


def test_a_cleanup_that_leaves_an_unrecorded_file_bare_records_nothing(tmp_path, apm_home) -> None:
    """A new legacy record never claims a skeleton: a later install still decides its origin."""
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_WITH_ENTRY, encoding="utf-8")
    with recorded_config_write(config):  # cleanup removes the last entry
        config.write_text(_SKELETON, encoding="utf-8")

    assert config_file_origin(config) is None
    assert config.read_text(encoding="utf-8") == _SKELETON


# -- a Windows UNC key survives a reload (#5016 review) ---------------------------------

_UNC_KEY = "//server/share/project/.mcp.json"


def test_a_unc_record_survives_another_projects_save(tmp_path, apm_home) -> None:
    """A record keyed by a UNC path (how a share's file is keyed on Windows) is valid:
    it is kept when another project's write saves the store, never dropped as malformed."""
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    store.parent.mkdir(parents=True)
    row = {"path": _UNC_KEY, "origin": "preexisting", "hash": "sha256:" + "0" * 64}
    store.write_text(json.dumps({"version": 1, "files": [row]}), encoding="utf-8")

    _apm_writes(tmp_path / "other" / ".mcp.json", _WITH_ENTRY)

    rows = [
        stored
        for path in store.parent.glob("*.json")
        for stored in json.loads(path.read_text(encoding="utf-8")).get("files", [])
    ]
    assert row in rows, rows


def _unc(path: Path) -> Path | None:
    """*path* through this machine's administrative share, when one is reachable."""
    drive = path.drive.rstrip(":")
    if not drive:
        return None
    unc = Path(f"\\\\localhost\\{drive}$") / path.relative_to(path.anchor)
    try:
        return unc if unc.exists() else None
    except OSError:
        return None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC paths")
def test_a_users_config_on_a_unc_path_is_never_deleted(tmp_path, apm_home) -> None:
    project = _unc(tmp_path)
    if project is None:
        pytest.skip("no administrative share on this machine")
    config = project / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")  # the user's own skeleton
    _apm_writes(config, _WITH_ENTRY, manifest_root=config.parent)
    config.write_text(_SKELETON, encoding="utf-8")  # APM's entry retired again

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=config.parent) is False
    assert config.read_text(encoding="utf-8") == _SKELETON


# -- the first bytes of an install's deploy journal (#5051) ------------------------------


@contextmanager
def _journal(token: str) -> Iterator[None]:
    """APM writes inside an install whose deploy journal is *token*."""
    from apm_cli.integration import write_seam

    def recorder(path, contents, expected=None) -> None:
        return None

    write_seam.listen(recorder, journal=token)
    try:
        yield
    finally:
        write_seam.stop_listening(recorder)


def test_every_write_of_one_install_vouches_for_its_first_bytes(tmp_path, apm_home) -> None:
    """Two packages rewrite one config in one install: the journal keeps the bytes the
    install first found, and the records vouch for undoing both writes to them."""
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    first = config.read_bytes()
    with _journal("install-1"):
        _apm_writes(config, _WITH_ENTRY.replace("srv", "a"))
        middle = config.read_bytes()
        _apm_writes(config, _WITH_ENTRY.replace("srv", "b"))

    assert vouches_for_current_bytes(config, first, journal="install-1") is True
    assert vouches_for_current_bytes(config, middle, journal="install-1") is False
    assert vouches_for_current_bytes(config, first, journal="another-install") is False
    assert vouches_for_current_bytes(config, first) is False
    assert '"b"' in config.read_text(encoding="utf-8")


def test_the_next_install_starts_its_own_first_bytes(tmp_path, apm_home) -> None:
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    first = config.read_bytes()
    with _journal("install-1"):
        _apm_writes(config, _WITH_ENTRY.replace("srv", "a"))
    between = config.read_bytes()
    with _journal("install-2"):
        _apm_writes(config, _WITH_ENTRY.replace("srv", "b"))

    assert vouches_for_current_bytes(config, between, journal="install-2") is True
    assert vouches_for_current_bytes(config, first, journal="install-2") is False
    assert vouches_for_current_bytes(config, first, journal="install-1") is False
    assert '"b"' in config.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="a backslash is a separator on Windows")
def test_a_users_config_under_a_backslash_directory_name_is_never_deleted(
    tmp_path, apm_home
) -> None:
    """On POSIX a backslash is a filename character: the record keyed by it is valid."""
    project = tmp_path / "project\\name"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")  # the user's own skeleton
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    config.write_text(_SKELETON, encoding="utf-8")  # APM's entry retired again

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.read_text(encoding="utf-8") == _SKELETON


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_link_to_a_users_config_never_lends_it_apms_record(tmp_path, apm_home) -> None:
    """APM's config replaced by a link to the user's identical file: APM's next write
    into the user's file classifies it as the user's, never as APM-created through
    that link, so retiring APM's entry later keeps the file."""
    apms = tmp_path / "apm" / ".mcp.json"
    _apm_writes(apms, _SKELETON)  # APM created it
    users = tmp_path / "user" / ".mcp.json"
    users.parent.mkdir()
    users.write_text(_SKELETON, encoding="utf-8")  # the user's own, same bytes
    apms.unlink()
    apms.symlink_to(users)

    _apm_writes(users, _WITH_ENTRY, manifest_root=users.parent)  # APM merges into it
    users.write_text(_SKELETON, encoding="utf-8")  # APM's entry retired again

    assert config_file_origin(users) is Origin.PREEXISTING
    assert _apm_empties(users, root=tmp_path / "user") is False
    assert users.read_text(encoding="utf-8") == _SKELETON


def test_a_record_turned_into_a_symlink_loop_never_breaks_another_lookup(
    tmp_path, apm_home
) -> None:
    """Project A's recorded config becoming a link loop leaves project B's lookup alone."""
    recorded = tmp_path / "a" / ".mcp.json"
    _apm_writes(recorded, _WITH_ENTRY)
    recorded.unlink()
    try:
        recorded.symlink_to(recorded)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    other = tmp_path / "b" / ".mcp.json"
    other.parent.mkdir(parents=True)
    other.write_text(_WITH_ENTRY, encoding="utf-8")

    assert config_file_origin(other) is not Origin.CREATED
    assert recorded.is_symlink() and other.read_text(encoding="utf-8") == _WITH_ENTRY


def _store_files(apm_home: Path) -> dict[Path, tuple[int, int, bytes]]:
    """Every file of the record store: identity, mtime and bytes."""
    root = apm_home / "deployment-receipts" / "config-files"
    found = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    return {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in found}


def _holding(apm_home: Path, config: Path) -> Path:
    """The one store file holding *config*'s record."""
    key = config_file_key(config).encode()
    found = [path for path, state in _store_files(apm_home).items() if key in state[2]]
    assert len(found) == 1, found
    return found[0]


def test_a_write_does_not_rewrite_unrelated_records(tmp_path, apm_home) -> None:
    """Project B's write leaves the store file holding project A's record untouched."""
    a_config = tmp_path / "a" / ".mcp.json"
    _apm_writes(a_config, _WITH_ENTRY)
    a_key = config_file_key(a_config)
    holding_a = {
        path: state for path, state in _store_files(apm_home).items() if a_key.encode() in state[2]
    }
    assert holding_a, "no store file holds project A's record"

    _apm_writes(tmp_path / "b" / ".mcp.json", _WITH_ENTRY)

    after = _store_files(apm_home)
    assert {path: after.get(path) for path in holding_a} == holding_a
    assert all(path.read_bytes() == state[2] for path, state in holding_a.items())
    assert config_file_origin(tmp_path / "b" / ".mcp.json") is Origin.CREATED
    assert config_file_origin(a_config) is Origin.CREATED


def _v1_reader(apm_home: Path) -> dict:
    """The record store reader that shipped with layout version 1 (#4959), verbatim in
    what it accepts: anything else is an unreadable store, which keeps every file."""
    path = apm_home / "deployment-receipts" / "config-files" / "records.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), list):
        raise ValueError("unknown version or layout")
    return data


def test_an_older_apm_fails_closed_on_the_indexed_store(tmp_path, apm_home) -> None:
    """Downgrade safety: a version 1 reader never mis-reads the new store as empty or
    partial (which could delete a user's file); it finds an unknown layout."""
    _apm_writes(tmp_path / "a" / ".mcp.json", _WITH_ENTRY)
    _apm_writes(tmp_path / "b" / ".mcp.json", _WITH_ENTRY)

    with pytest.raises(ValueError, match="unknown version or layout"):
        _v1_reader(apm_home)


def test_a_version_1_store_is_read_without_losing_a_record(tmp_path, apm_home) -> None:
    """Migration: every record of an existing store survives the first write."""
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    store.parent.mkdir(parents=True)
    users = tmp_path / "users" / ".mcp.json"
    users.parent.mkdir()
    users.write_text(_SKELETON, encoding="utf-8")
    rows = [
        {"path": config_file_key(users), "origin": "preexisting", "hash": "sha256:" + "0" * 64},
        {
            "path": config_file_key(tmp_path / "gone" / ".mcp.json"),
            "origin": "created",
            "hash": "sha256:" + "1" * 64,
        },
    ]
    store.write_text(json.dumps({"version": 1, "files": rows}), encoding="utf-8")

    _apm_writes(tmp_path / "new" / ".mcp.json", _WITH_ENTRY)

    assert config_file_origin(users) is Origin.PREEXISTING
    assert config_file_origin(tmp_path / "gone" / ".mcp.json") is Origin.CREATED
    assert config_file_origin(tmp_path / "new" / ".mcp.json") is Origin.CREATED
    assert _apm_empties(users, root=tmp_path / "users") is False
    assert users.read_text(encoding="utf-8") == _SKELETON


@contextmanager
def _killed_at_write(apm_home: Path, after: int) -> Iterator[None]:
    """The process dies at record store replace number *after* + 1 (nothing of it lands)."""
    real = os.replace
    store = apm_home / "deployment-receipts" / "config-files"
    calls = {"n": 0}

    def replace(source, destination, *args, **kwargs):
        if Path(destination).parent == store:
            calls["n"] += 1
            if calls["n"] > after:
                raise OSError("killed")
        return real(source, destination, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", replace)
        yield


def test_a_kill_mid_write_loses_no_saved_record(tmp_path, apm_home) -> None:
    """Per-write durability: a record already saved survives a kill in a later save."""
    first = tmp_path / "project" / ".mcp.json"
    _apm_writes(first, _WITH_ENTRY)
    second = tmp_path / "project" / "other.json"
    with _killed_at_write(apm_home, after=0):  # the config write lands, its record does not
        _apm_writes(second, _WITH_ENTRY)

    assert second.read_text(encoding="utf-8") == _WITH_ENTRY
    assert config_file_origin(first) is Origin.CREATED
    assert _apm_empties(first, root=tmp_path / "project") is True
    assert not first.exists()


def _version_1_store(tmp_path: Path, apm_home: Path) -> tuple[Path, Path]:
    """A layout 1 store recording a user's file and an APM file in two projects."""
    users = tmp_path / "users" / ".mcp.json"
    apms = tmp_path / "apms" / ".mcp.json"
    for config, text in ((users, _SKELETON), (apms, _WITH_ENTRY)):
        config.parent.mkdir()
        config.write_bytes(text.encode("utf-8"))  # the bytes the seeded digest names
    rows = [
        {"path": config_file_key(users), "origin": "preexisting", "hash": "sha256:" + "0" * 64},
        {"path": config_file_key(apms), "origin": "created", "hash": _sha(_WITH_ENTRY)},
    ]
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"version": 1, "files": rows}), encoding="utf-8")
    return users, apms


def _sha(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("after", [0, 1, 2, 3])
def test_an_interrupted_migration_leaves_the_old_store_whole(tmp_path, apm_home, after) -> None:
    """Killed after *after* store writes of the migration: the layout 1 store is still
    the whole store (a version 1 reader reads it), and the next write migrates it."""
    users, apms = _version_1_store(tmp_path, apm_home)
    old = (apm_home / "deployment-receipts" / "config-files" / "records.json").read_bytes()
    with _killed_at_write(apm_home, after=after):
        _apm_writes(tmp_path / "new" / ".mcp.json", _WITH_ENTRY)

    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    # Three record files, then the marker: every kill point lands before the marker.
    assert store.read_bytes() == old
    assert len(_v1_reader(apm_home)["files"]) == 2
    assert config_file_origin(users) is Origin.PREEXISTING
    assert config_file_origin(apms) is Origin.CREATED

    _apm_writes(tmp_path / "later" / ".mcp.json", _WITH_ENTRY)

    with pytest.raises(ValueError, match="unknown version or layout"):
        _v1_reader(apm_home)
    assert config_file_origin(users) is Origin.PREEXISTING
    assert _apm_empties(users, root=tmp_path / "users") is False
    assert users.read_text(encoding="utf-8") == _SKELETON
    assert _apm_empties(apms, root=tmp_path / "apms") is True
    assert not apms.exists()


def test_a_malformed_record_file_keeps_the_file_and_vouches_for_nothing(tmp_path, apm_home) -> None:
    """An unreadable directory record file fails closed like an unreadable store."""
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    held = config.read_bytes()
    _holding(apm_home, config).write_text("{", encoding="utf-8")

    assert _apm_empties(config, root=tmp_path / "project") is False
    assert config.read_bytes() == held
    assert vouches_for_current_bytes(config, b"") is False
    assert config_file_origin(config) is None


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="needs non-UTF-8 file names")
def test_a_users_config_under_a_non_utf8_directory_name_keeps_its_record(
    tmp_path, apm_home
) -> None:
    """A POSIX directory name that is not UTF-8 (surrogate-escaped) has a record file."""
    project = tmp_path / os.fsdecode(b"project-\xff")
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")  # the user's own skeleton
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)
    config.write_text(_SKELETON, encoding="utf-8")  # APM's entry retired again

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False
    assert config.read_text(encoding="utf-8") == _SKELETON


def test_two_first_writers_keep_each_others_records(tmp_path, apm_home) -> None:
    """Two installs that both find no store yet: neither drops the other's record file."""
    from apm_cli.integration.config_file_provenance import _Store

    first_config = tmp_path / "a" / ".mcp.json"
    second_config = tmp_path / "b" / ".mcp.json"
    first, second = _Store.load(), _Store.load()  # both see no store yet
    first.files[config_file_key(first_config)] = {"origin": "preexisting", "hash": "sha256:1"}
    first.save()
    second.files[config_file_key(second_config)] = {"origin": "preexisting", "hash": "sha256:2"}
    second.save()

    assert config_file_origin(first_config) is Origin.PREEXISTING
    assert config_file_origin(second_config) is Origin.PREEXISTING
    held = [path.read_text(encoding="utf-8") for path in _store_files(apm_home)]
    assert any(config_file_key(first_config) in text for text in held), held


def test_record_files_left_by_a_removed_store_are_never_read(tmp_path, apm_home) -> None:
    """A store removed by hand (its index gone) starts empty: leftover record files are
    another generation's and prove nothing."""
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(config, _WITH_ENTRY, manifest_root=config.parent)
    assert config_file_origin(config) is Origin.PREEXISTING
    (apm_home / "deployment-receipts" / "config-files" / "records.json").unlink()

    _apm_writes(tmp_path / "other" / ".mcp.json", _WITH_ENTRY)

    assert config_file_origin(config) is None
    assert config.read_text(encoding="utf-8") == _WITH_ENTRY


def test_two_migrations_of_one_layout_1_store_keep_every_record(tmp_path, apm_home) -> None:
    """Two writers that both read the layout 1 store migrate it once: the second joins the
    first's index, so neither's record files are hidden under another generation."""
    from apm_cli.integration.config_file_provenance import _Store

    users, apms = _version_1_store(tmp_path, apm_home)
    first_config = tmp_path / "first" / ".mcp.json"
    second_config = tmp_path / "second" / ".mcp.json"
    first, second = _Store.load(), _Store.load()  # both read layout 1
    first.files[config_file_key(first_config)] = {"origin": "preexisting", "hash": "sha256:1"}
    first.save()
    second.files[config_file_key(second_config)] = {"origin": "preexisting", "hash": "sha256:2"}
    second.save()

    assert config_file_origin(first_config) is Origin.PREEXISTING
    assert config_file_origin(second_config) is Origin.PREEXISTING
    assert config_file_origin(users) is Origin.PREEXISTING
    assert config_file_origin(apms) is Origin.CREATED
    held = [path.read_text(encoding="utf-8") for path in _store_files(apm_home)]
    assert any(config_file_key(first_config) in text for text in held), held


def test_an_unfinished_index_never_blocks_later_writes(tmp_path, apm_home) -> None:
    """A kill while the index is created leaves no empty index: the marker is published
    whole or not at all, so the next write is recorded."""
    real_dumps = json.dumps

    def killed(obj, *args, **kwargs):
        if isinstance(obj, dict) and "layout" in obj:
            raise KeyboardInterrupt  # the process dies while it writes the index
        return real_dumps(obj, *args, **kwargs)

    first = tmp_path / "first" / ".mcp.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(json, "dumps", killed)
        with pytest.raises(KeyboardInterrupt):
            _apm_writes(first, _WITH_ENTRY)

    second = tmp_path / "second" / ".mcp.json"
    _apm_writes(second, _WITH_ENTRY)

    assert config_file_origin(second) is Origin.CREATED
    assert second.read_text(encoding="utf-8") == _WITH_ENTRY


def test_a_record_file_of_another_generation_is_never_deleted(tmp_path, apm_home) -> None:
    """A lookup that reads another generation's record file and then saves other records
    leaves that file in place."""
    from apm_cli.integration.config_file_provenance import _Store

    old = tmp_path / "old" / ".mcp.json"
    _apm_writes(old, _WITH_ENTRY)
    old_file = _holding(apm_home, old)
    marker = apm_home / "deployment-receipts" / "config-files" / "records.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    marker.write_text(json.dumps({**data, "generation": "another"}), encoding="utf-8")

    store = _Store.load()
    store.lookup(old)  # reads the old generation's file, which proves nothing now
    store.files[config_file_key(tmp_path / "new" / ".mcp.json")] = {"origin": "created"}
    store.save()

    assert old_file.is_file()
    assert config_file_origin(old) is None


def test_a_busy_apm_home_names_the_other_apm_and_leaves_the_users_file(
    tmp_path, apm_home, monkeypatch
) -> None:
    """Another apm holding APM_HOME's lock: the write is refused with that reason, not a
    request to remove the records."""
    import filelock

    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")  # the user's own file

    def busy(self, *args, **kwargs):
        raise filelock.Timeout(self.lock_file)

    monkeypatch.setattr(filelock.FileLock, "acquire", busy)
    with pytest.raises(OSError, match="another apm") as refused:
        _apm_writes(config, _WITH_ENTRY, manifest_root=project)

    assert "remove the config file records" not in str(refused.value)
    assert config.read_text(encoding="utf-8") == _SKELETON


def test_a_journal_first_touch_entry_survives_migration_to_record_files(tmp_path, apm_home) -> None:
    """#5051 x #5037: a layout 1 store's {"prior", "journal"} history entry is carried into
    the record files, so replay of that journal still vouches for its first bytes."""
    config = tmp_path / "project" / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(_WITH_ENTRY.encode("utf-8"))
    first = b'{"mcpServers": {}}\n'
    key = config_file_key(config)
    v1 = {
        "version": 1,
        "files": [{"path": key, "origin": "created", "hash": _sha(_WITH_ENTRY)}],
        "history": {
            key: {
                "prior": _sha(first.decode("utf-8")),
                "journal": "install-1",
            }
        },
    }
    store = apm_home / "deployment-receipts" / "config-files" / "records.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps(v1), encoding="utf-8")

    _apm_writes(tmp_path / "other" / ".mcp.json", _WITH_ENTRY)  # migrates the store

    assert json.loads(store.read_text(encoding="utf-8"))["version"] == 2
    assert vouches_for_current_bytes(config, first, journal="install-1") is True
    assert vouches_for_current_bytes(config, first, journal="install-2") is False


def test_a_save_from_a_stale_read_keeps_another_writers_preexisting_record(
    tmp_path, apm_home
) -> None:
    """#5270: writer B read the record file before writer A saved the user's record into it;
    B's save applies only B's change, so the user's file keeps its deletion protection."""
    from apm_cli.integration.config_file_provenance import _Store

    project = tmp_path / "project"
    ours, users = project / "other.json", project / ".mcp.json"
    _apm_writes(ours, _WITH_ENTRY)  # a record already in that directory's record file
    stale = _Store.load()
    stale.lookup(ours)  # writer B reads the record file
    users.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(users, _WITH_ENTRY, manifest_root=project)  # writer A records the user's file
    stale.files[config_file_key(ours)] = {"origin": "created", "hash": _sha(_SKELETON)}
    stale.save()  # writer B saves its change

    assert config_file_origin(users) is Origin.PREEXISTING
    before = users.read_bytes()
    assert _apm_empties(users, root=project) is False
    assert users.read_bytes() == before
    ours.write_text(_SKELETON, encoding="utf-8")
    assert _apm_empties(ours, root=project) is True, "writer B's own change was kept"
    assert not ours.exists()


def test_a_save_from_a_stale_read_never_brings_back_a_record_another_writer_forgot(
    tmp_path, apm_home
) -> None:
    """#5270: a record removed after writer B read the file stays removed."""
    from apm_cli.integration.config_file_provenance import _Store, forget_config_file

    project = tmp_path / "project"
    ours, gone = project / "other.json", project / ".mcp.json"
    _apm_writes(ours, _WITH_ENTRY)
    _apm_writes(gone, _WITH_ENTRY)
    stale = _Store.load()
    stale.lookup(ours)  # writer B reads the record file, both records in it
    gone.unlink()
    forget_config_file(gone)  # writer A removed its file and forgot its record
    stale.files[config_file_key(ours)] = {"origin": "created", "hash": _sha(_SKELETON)}
    stale.save()

    assert config_file_origin(gone) is None
    assert config_file_origin(ours) is Origin.CREATED
    store = apm_home / "deployment-receipts" / "config-files"
    stored = b"".join(path.read_bytes() for path in sorted(store.glob("*.json")))
    assert json.dumps(config_file_key(gone)).encode() not in stored
