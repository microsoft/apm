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

    rows = json.loads(store.read_text(encoding="utf-8"))["files"]
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
