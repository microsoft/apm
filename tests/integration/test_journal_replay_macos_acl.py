"""Replay keeps a journaled path that carries a macOS ACL (#5178).

Restored bytes are staged without any ACL of their own, so a path with an
extended ACL (for example ``everyone deny delete``) would lose it. Replay of
an abandoned deploy journal keeps and reports such a path, as it does for a
POSIX ACL on Linux and an explicit ACE on Windows; a path without an ACL
restores as before.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs"),
]

_CONFIG_BYTES = b'{"mcpServers": {"s": {"command": "c"}}}\n'
_KEEP_ACL = "it carries a macOS ACL"
_DENY_DELETE = "everyone deny delete"


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "apm-home"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


@pytest.fixture
def acl_paths() -> Iterator[list[Path]]:
    """Paths given an ACL; it is removed afterwards so the temp tree can be deleted."""
    paths: list[Path] = []
    yield paths
    for path in paths:
        subprocess.run(["chmod", "-N", str(path)], check=False, capture_output=True)


def _acl(path: Path) -> str:
    """The ``ls -led`` ACL lines of *path* (the first line is the entry itself)."""
    listing = subprocess.run(
        ["ls", "-led", str(path)], check=True, capture_output=True, text=True
    ).stdout
    return "\n".join(listing.splitlines()[1:])


def _deny_delete(path: Path, acl_paths: list[Path]) -> str:
    subprocess.run(["chmod", "+a", _DENY_DELETE, str(path)], check=True, capture_output=True)
    acl_paths.append(path)
    listing = _acl(path)
    assert "deny delete" in listing, listing
    return listing


def _replay(apm_home: Path, project: Path, ledger: dict[str, str]):
    from apm_cli.install.deploy_snapshot import replay_abandoned_journals

    return replay_abandoned_journals(
        apm_home / "install-snapshots",
        project,
        lambda: ledger,
        receipts=apm_home / "deployment-receipts",
    )


def _killed_config(apm_home: Path, project: Path) -> tuple[Path, dict[str, str]]:
    """A config file, journaled, then overwritten by a killed install."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    config = project / ".mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(_CONFIG_BYTES)
    ledger = {".mcp.json": compute_file_hash(config)}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(config)
    config.write_bytes(b"killed\n")
    journal.after_write(config)
    return config, ledger


def _killed_skill(apm_home: Path, project: Path) -> tuple[Path, dict[str, str]]:
    """A skill directory, journaled, then written by a killed install."""
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.utils.content_hash import compute_file_hash

    skill = project / ".kiro" / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"prior\n")
    ledger = {".kiro/skills/s/SKILL.md": compute_file_hash(skill / "SKILL.md")}
    journal = DeployJournal(apm_home / "install-snapshots", root=project)
    journal.record(skill)
    (skill / "SKILL.md").write_bytes(b"killed\n")
    journal.after_write(skill / "SKILL.md")
    journal.after_write(skill)
    return skill, ledger


def test_a_file_with_a_deny_entry_is_kept_with_its_acl_and_reported(
    tmp_path: Path, apm_home: Path, acl_paths: list[Path]
) -> None:
    project = tmp_path / "project"
    config, ledger = _killed_config(apm_home, project)
    before = _deny_delete(config, acl_paths)

    outcome = _replay(apm_home, project, ledger)

    assert (outcome.restored, outcome.kept) == (0, ((".mcp.json", _KEEP_ACL),))
    assert config.read_bytes() == b"killed\n"
    assert _acl(config) == before


def test_a_directory_with_a_deny_entry_on_an_entry_is_kept_with_its_acl(
    tmp_path: Path, apm_home: Path, acl_paths: list[Path]
) -> None:
    project = tmp_path / "project"
    skill, ledger = _killed_skill(apm_home, project)
    before = _deny_delete(skill / "SKILL.md", acl_paths)

    outcome = _replay(apm_home, project, ledger)

    assert (outcome.restored, outcome.kept) == (0, ((".kiro/skills/s", _KEEP_ACL),))
    assert (skill / "SKILL.md").read_bytes() == b"killed\n"
    assert _acl(skill / "SKILL.md") == before


def test_paths_without_an_acl_restore_as_before(tmp_path: Path, apm_home: Path) -> None:
    """The no-ACL control: a file and a directory unit both restore."""
    config, config_ledger = _killed_config(apm_home, tmp_path / "file")
    skill, skill_ledger = _killed_skill(apm_home, tmp_path / "dir")

    file_outcome = _replay(apm_home, tmp_path / "file", config_ledger)
    dir_outcome = _replay(apm_home, tmp_path / "dir", skill_ledger)

    assert (file_outcome.restored, file_outcome.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES
    assert (dir_outcome.restored, dir_outcome.kept) == (1, ())
    assert (skill / "SKILL.md").read_bytes() == b"prior\n"
    assert _acl(skill / "SKILL.md") == ""
