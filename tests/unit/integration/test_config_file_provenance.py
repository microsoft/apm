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


def test_a_record_store_inside_the_project_never_deletes(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    monkeypatch.setenv("APM_HOME", str(project / ".apm-home"))
    config = project / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)

    assert _apm_empties(config, root=project) is False
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


# -- Q4: provenance keyed by the resolved file --------------------------------------


def test_a_profile_path_with_spaces_and_non_ascii_keeps_its_record(tmp_path, apm_home) -> None:
    """(1) COPILOT_HOME with spaces and non-ASCII characters."""
    config = tmp_path / "My Profile \u00e9" / "mcp-config.json"
    _apm_writes(config, _WITH_ENTRY)

    assert config_file_origin(config) is Origin.CREATED
    assert _apm_empties(config, root=tmp_path) is True


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


def test_nfc_and_nfd_spellings_name_one_record(tmp_path, apm_home) -> None:
    """(4) A profile directory spelled NFC or NFD is one file on macOS: one key."""
    nfc = unicodedata.normalize("NFC", "caf\u00e9")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd

    assert config_file_key(tmp_path / nfd / "x.json") == config_file_key(tmp_path / nfc / "x.json")


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


def test_traversal_in_a_config_path_names_the_resolved_file(tmp_path, apm_home) -> None:
    """(5) ``..`` in a config or profile path resolves to the one file it names."""
    config = tmp_path / "project" / ".mcp.json"
    (tmp_path / "project" / "sub").mkdir(parents=True)
    _apm_writes(tmp_path / "project" / "sub" / ".." / ".mcp.json", _WITH_ENTRY)

    assert config_file_origin(config) is Origin.CREATED


def test_a_tampered_record_with_a_relative_or_traversing_path_is_ignored(
    tmp_path, apm_home
) -> None:
    """(5) A record whose key is not an absolute resolved path proves nothing."""
    project = tmp_path / "project"
    config = project / ".mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(_SKELETON, encoding="utf-8")
    _apm_writes(config, _WITH_ENTRY, manifest_root=project)  # recorded as the user's
    store = apm_home / "deployment-receipts" / "config-files.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    created = {"origin": "created"}
    data["files"] = {
        **data["files"],
        "project/.mcp.json": created,
        f"{project.as_posix()}/sub/../.mcp.json": created,
    }
    store.write_text(json.dumps(data), encoding="utf-8")

    assert config_file_origin(config) is Origin.PREEXISTING
    assert _apm_empties(config, root=project) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_an_unreadable_record_store_keeps_every_file(tmp_path, apm_home) -> None:
    config = tmp_path / "project" / ".mcp.json"
    _apm_writes(config, _WITH_ENTRY)
    (apm_home / "deployment-receipts" / "config-files.json").write_text("{", encoding="utf-8")

    assert _apm_empties(config, root=tmp_path / "project") is False
    assert config.is_file()
