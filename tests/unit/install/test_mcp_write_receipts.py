"""MCP write evidence: record, bind, load, reject unusable records, retire (#4737)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.install.mcp.ownership import mcp_native_hash
from apm_cli.install.mcp.write_receipts import (
    McpWriteReceipts,
    forget_removed_mcp_writes,
    journal_mcp_writes,
    kept_mcp_writes,
    note_mcp_write,
    retire_undeclared_mcp_writes,
)
from apm_cli.utils.path_security import PathTraversalError

pytestmark = pytest.mark.component

SERVER = "demo-mcp"
ENTRY = {"type": "stdio", "command": "demo", "args": ["--stdio"]}


class _Logger(NullCommandLogger):
    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []
        self.removed: set[str] = set()

    def warning(self, message: str, symbol: str = "warning") -> None:
        self.warnings.append(message)

    def record_mcp_removed(self, names) -> None:
        self.removed.update(names)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APM_HOME", str(home / ".apm"))
    root = tmp_path / "consumer"
    root.mkdir()
    return root


def _config(root: Path) -> Path:
    return root / ".mcp.json"


def _write_entries(root: Path, servers: dict) -> None:
    _config(root).write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")


def _servers(root: Path) -> dict:
    return json.loads(_config(root).read_text())["mcpServers"]


def _install_write(root: Path, logger: _Logger, entry: dict = ENTRY) -> None:
    """What an install does: write the entry, then note it inside the journal."""
    with journal_mcp_writes(root, user_scope=False, logger=logger):
        _write_entries(root, {**_existing(root), SERVER: entry})
        note_mcp_write("copilot", SERVER, mcp_native_hash(entry))


def _existing(root: Path) -> dict:
    return _servers(root) if _config(root).is_file() else {}


def _retire(root: Path, logger: _Logger) -> None:
    retire_undeclared_mcp_writes(
        set(), project_root=root, user_scope=False, exclude=None, logger=logger
    )


def test_install_records_only_the_entries_it_wrote(project: Path) -> None:
    _write_entries(project, {"user-mcp": {"type": "stdio", "command": "mine", "args": []}})
    _install_write(project, _Logger())

    (write,) = McpWriteReceipts.open(project, user_scope=False).writes.values()
    assert (write.runtime, write.name, write.key) == ("copilot", SERVER, SERVER)
    assert write.config == _config(project).absolute().as_posix()
    assert write.content_hash == mcp_native_hash(ENTRY)
    assert write.usable and not write.kept


def test_evidence_outside_a_journal_is_not_recorded(project: Path) -> None:
    _write_entries(project, {SERVER: ENTRY})
    note_mcp_write("copilot", SERVER, mcp_native_hash(ENTRY))

    assert McpWriteReceipts.open(project, user_scope=False).writes == {}


def test_a_proven_entry_no_longer_declared_is_retired(project: Path) -> None:
    _write_entries(project, {"user-mcp": {"type": "stdio", "command": "mine", "args": []}})
    _install_write(project, _Logger())
    logger = _Logger()

    _retire(project, logger)

    assert set(_servers(project)) == {"user-mcp"}
    assert logger.removed == {SERVER} and not logger.warnings
    assert McpWriteReceipts.open(project, user_scope=False).writes == {}


def test_a_declared_server_is_never_retired(project: Path) -> None:
    _install_write(project, _Logger())

    retire_undeclared_mcp_writes(
        {SERVER}, project_root=project, user_scope=False, exclude=None, logger=_Logger()
    )

    assert SERVER in _servers(project)


def test_an_unrecorded_identical_entry_is_never_retired(project: Path) -> None:
    _write_entries(project, {SERVER: ENTRY})
    before = _config(project).read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert _config(project).read_bytes() == before
    assert not logger.warnings and not logger.removed


@pytest.mark.parametrize("change", ["edited", "unusable-hash", "other-config"])
def test_an_unproven_entry_is_kept_marked_and_reported(project: Path, change: str) -> None:
    _install_write(project, _Logger())
    if change == "edited":
        _write_entries(project, {SERVER: {**ENTRY, "args": ["--edited"]}})
    else:
        path = McpWriteReceipts.open(project, user_scope=False).path
        data = json.loads(path.read_text())
        field, value = ("hash", "sha256:bad") if change == "unusable-hash" else ("config", "/x")
        data["writes"][0][field] = value
        path.write_text(json.dumps(data))
    before = _config(project).read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert _config(project).read_bytes() == before
    assert not logger.removed
    assert [w for w in logger.warnings if f"Kept MCP server '{SERVER}' in .mcp.json" in w]
    kept = kept_mcp_writes(project, user_scope=False, managed=())
    assert [(write.name, config) for write, config in kept] == [(SERVER, ".mcp.json")]
    assert kept_mcp_writes(project, user_scope=False, managed={SERVER}) == []


def test_a_kept_record_is_forgotten_once_its_entry_is_removed(project: Path) -> None:
    _install_write(project, _Logger())
    _write_entries(project, {SERVER: {**ENTRY, "args": ["--edited"]}})
    _retire(project, _Logger())
    _write_entries(project, {})

    forget_removed_mcp_writes(project, user_scope=False)

    assert McpWriteReceipts.open(project, user_scope=False).writes == {}
    assert kept_mcp_writes(project, user_scope=False, managed=()) == []


def test_evidence_bound_to_another_installation_is_unusable(project: Path) -> None:
    _install_write(project, _Logger())
    identity = project / ".apm-local" / "installation"
    identity.write_text("0" * 32 + "\n")
    logger = _Logger()

    with pytest.raises(ValueError, match="Invalid MCP write evidence"):
        McpWriteReceipts.open(project, user_scope=False)
    _retire(project, logger)

    assert SERVER in _servers(project), "unusable evidence must not remove anything"
    assert logger.warnings and "Kept every MCP entry" in logger.warnings[0]
    assert kept_mcp_writes(project, user_scope=False, managed=()) == []


def test_a_corrupt_evidence_file_is_rejected(project: Path) -> None:
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    path.write_text("{not json")

    with pytest.raises(ValueError, match="remove that file to start a new record"):
        McpWriteReceipts.open(project, user_scope=False)


def test_a_symlinked_evidence_file_is_refused(project: Path, tmp_path: Path) -> None:
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(path.read_text())
    path.unlink()
    path.symlink_to(elsewhere)

    with pytest.raises(PathTraversalError):
        McpWriteReceipts.open(project, user_scope=False)
