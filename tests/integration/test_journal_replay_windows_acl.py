"""Replay keeps a journaled path that carries a Windows ACL (#5178).

Restored bytes are staged without any ACL of their own, so a path whose DACL
holds an entry it did not inherit would lose it. Replay of an abandoned deploy
journal keeps and reports such a path, as it does for a POSIX ACL on Linux; a
path whose ACEs are all inherited restores as before.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
    pytest.mark.skipif(os.name != "nt", reason="Windows DACLs"),
]

_CONFIG_BYTES = b'{"mcpServers": {"s": {"command": "c"}}}\n'
_KEEP_ACL = "it carries a Windows ACL"
# BUILTIN\Users, read: an explicit (non-inherited) grant icacls lists as ":(R)".
_EXPLICIT_GRANT = "*S-1-5-32-545:(R)"


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "apm-home"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


def _acl(path: Path) -> str:
    """The icacls listing of *path*, without the trailing summary line."""
    listing = subprocess.run(
        ["icacls", str(path)], check=True, capture_output=True, text=True
    ).stdout
    return "\n".join(line for line in listing.splitlines()[:-1] if line.strip())


def _grant_explicit_ace(path: Path) -> str:
    subprocess.run(
        ["icacls", str(path), "/grant", _EXPLICIT_GRANT], check=True, capture_output=True
    )
    listing = _acl(path)
    assert any(line.rstrip().endswith(":(R)") for line in listing.splitlines()), listing
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


def test_a_file_with_an_explicit_ace_is_kept_with_its_acl_and_reported(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    config, ledger = _killed_config(apm_home, project)
    before = _grant_explicit_ace(config)

    outcome = _replay(apm_home, project, ledger)

    assert (outcome.restored, outcome.kept) == (0, ((".mcp.json", _KEEP_ACL),))
    assert config.read_bytes() == b"killed\n"
    assert _acl(config) == before


def test_a_directory_with_an_explicit_ace_on_an_entry_is_kept_with_its_acl(
    tmp_path: Path, apm_home: Path
) -> None:
    project = tmp_path / "project"
    skill, ledger = _killed_skill(apm_home, project)
    before = _grant_explicit_ace(skill / "SKILL.md")

    outcome = _replay(apm_home, project, ledger)

    assert (outcome.restored, outcome.kept) == (0, ((".kiro/skills/s", _KEEP_ACL),))
    assert (skill / "SKILL.md").read_bytes() == b"killed\n"
    assert _acl(skill / "SKILL.md") == before


def test_paths_whose_aces_are_all_inherited_restore_as_before(
    tmp_path: Path, apm_home: Path
) -> None:
    """The no-ACL control: a file and a directory unit both restore, and the restored
    directory takes only inherited ACEs (staging adds no DACL of its own)."""
    config, config_ledger = _killed_config(apm_home, tmp_path / "file")
    skill, skill_ledger = _killed_skill(apm_home, tmp_path / "dir")

    file_outcome = _replay(apm_home, tmp_path / "file", config_ledger)
    dir_outcome = _replay(apm_home, tmp_path / "dir", skill_ledger)

    assert (file_outcome.restored, file_outcome.kept) == (1, ())
    assert config.read_bytes() == _CONFIG_BYTES
    assert (dir_outcome.restored, dir_outcome.kept) == (1, ())
    assert (skill / "SKILL.md").read_bytes() == b"prior\n"
    assert all("(I)" in line for line in _acl(skill).splitlines()), _acl(skill)
