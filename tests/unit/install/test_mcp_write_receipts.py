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

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-legacy-mcp-ownership-key-migration"),
]

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
    before = _config(project).read_bytes()

    retire_undeclared_mcp_writes(
        {SERVER}, project_root=project, user_scope=False, exclude=None, logger=_Logger()
    )

    assert _config(project).read_bytes() == before


def _reconcile(
    root: Path,
    logger: _Logger,
    admitted: dict[str, set[str]] | None,
    *,
    declared: set[str] = frozenset({SERVER}),
    retire: bool = True,
) -> None:
    retire_undeclared_mcp_writes(
        set(declared),
        project_root=root,
        user_scope=False,
        exclude=None,
        logger=logger,
        retire=retire,
        admitted=admitted,
    )


_UNTARGETED = (
    f"Kept MCP server '{SERVER}' in .mcp.json: APM no longer deploys it for copilot. "
    "Remove the entry if you no longer use it, or check targets: in apm.yml and in the "
    "server's declaration."
)


@pytest.mark.parametrize(
    ("admitted", "reported"),
    [
        ({"cursor": {SERVER}}, True),
        ({"copilot": set()}, True),
        ({"claude": {SERVER}}, False),
        ({"copilot": {SERVER}}, False),
        (None, False),
    ],
    ids=["untargeted", "not-admitted", "shared-destination", "targeted", "no-manifest-targets"],
)
def test_a_declared_entry_no_targeted_runtime_deploys_is_kept_and_reported(
    project: Path, admitted: dict[str, set[str]] | None, reported: bool
) -> None:
    """Never removed; reported until a targeted runtime deploys there again (#4784)."""
    _install_write(project, _Logger())
    before = _config(project).read_bytes()
    logger = _Logger()

    _reconcile(project, logger, admitted)

    assert _config(project).read_bytes() == before
    assert not logger.removed
    kept = kept_mcp_writes(project, user_scope=False, managed={SERVER})
    if not reported:
        assert not logger.warnings and kept == []
        return
    assert logger.warnings == [_UNTARGETED]
    assert [(write.name, config) for write, config in kept] == [(SERVER, ".mcp.json")]
    # Later installs announce it, with a lockfile or with --runtime (no admitted set).
    for retire, again in ((False, admitted), (True, None)):
        announced = _Logger()
        _reconcile(project, announced, again, retire=retire)
        assert announced.warnings == [_UNTARGETED]
    assert _config(project).read_bytes() == before


@pytest.mark.parametrize("retire", [True, False])
def test_a_runtime_sharing_the_destination_clears_the_keep(project: Path, retire: bool) -> None:
    """Claude reuses copilot's .mcp.json entry without a write: it is in use again."""
    _install_write(project, _Logger())
    before = _config(project).read_bytes()
    _reconcile(project, _Logger(), {"cursor": {SERVER}})
    logger = _Logger()

    _reconcile(project, logger, {"claude": {SERVER}}, retire=retire)

    assert _config(project).read_bytes() == before
    assert not logger.warnings
    assert kept_mcp_writes(project, user_scope=False, managed={SERVER}) == []


@pytest.mark.parametrize(("flag", "reported"), [("copilot", False), (None, True)])
def test_a_runtime_the_target_flag_selects_is_not_reported(
    project: Path, flag: str | None, reported: bool
) -> None:
    """apm.yml targets cursor; `--target copilot` selects copilot for this run."""
    from types import SimpleNamespace

    from apm_cli.core.target_detection import FLAG_TARGET_SOURCE, EffectiveTargetDecision

    _install_write(project, _Logger())
    before = _config(project).read_bytes()
    logger = _Logger()

    retire_undeclared_mcp_writes(
        {SERVER},
        project_root=project,
        user_scope=False,
        exclude=None,
        logger=logger,
        manifest=SimpleNamespace(targets=["cursor"]),
        dependencies=[SERVER],
        target_decision=EffectiveTargetDecision([flag], FLAG_TARGET_SOURCE) if flag else None,
    )

    assert _config(project).read_bytes() == before
    assert logger.warnings == ([_UNTARGETED] if reported else [])


def test_a_kept_entry_whose_server_is_no_longer_declared_stays_reported(project: Path) -> None:
    """Restoring copilot does not adopt an entry the lockfile no longer manages."""
    _install_write(project, _Logger())
    before = _config(project).read_bytes()
    _reconcile(project, _Logger(), {"cursor": {SERVER}})
    logger = _Logger()

    _reconcile(project, logger, {"copilot": set()}, declared=set(), retire=False)

    assert _config(project).read_bytes() == before
    assert logger.warnings == [_UNTARGETED]
    assert [
        write.name for write, _ in kept_mcp_writes(project, user_scope=False, managed=set())
    ] == [SERVER]


def test_an_unrecorded_identical_entry_is_never_retired(project: Path) -> None:
    _write_entries(project, {SERVER: ENTRY})
    before = _config(project).read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert _config(project).read_bytes() == before
    assert not logger.warnings and not logger.removed


@pytest.mark.parametrize("change", ["edited", "unusable-hash"])
def test_an_unproven_entry_is_kept_marked_and_reported(project: Path, change: str) -> None:
    _install_write(project, _Logger())
    if change == "edited":
        _write_entries(project, {SERVER: {**ENTRY, "args": ["--edited"]}})
    else:
        path = McpWriteReceipts.open(project, user_scope=False).path
        data = json.loads(path.read_text())
        data["writes"][0]["hash"] = "sha256:bad"
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


def test_evidence_bound_to_another_installation_is_set_aside_once(project: Path) -> None:
    """#5396: evidence another installation wrote is renamed once, never trusted or deleted."""
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    old = path.read_bytes()
    identity = project / ".apm-local" / "installation"
    identity.write_text("0" * 32 + "\n")
    logger = _Logger()

    with pytest.raises(ValueError, match="Invalid MCP write evidence"):
        McpWriteReceipts.open(project, user_scope=False)
    _retire(project, logger)
    _retire(project, again := _Logger())

    assert SERVER in _servers(project), "set-aside evidence must not remove anything"
    _assert_set_aside(logger.warnings, path, old)
    assert again.warnings == [], "the set-aside is reported once"
    assert kept_mcp_writes(project, user_scope=False, managed=()) == []


def test_a_reclone_sets_aside_the_sibling_receipt_its_new_identity_cannot_bind(
    project: Path,
) -> None:
    """Copilot #5403: fresh MCP evidence creates this checkout's identity; the old
    checkout's materialization receipt beside it must not then fail every ledger load."""
    import shutil

    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope

    _install_write(project, _Logger())
    session = DeploymentMaterializationSession(project, InstallScope.PROJECT, None)
    sibling = session.state_path
    sibling.write_text(
        json.dumps({**session.binding(), "records": [], "destinations": {}, "bindings": {}}),
        encoding="utf-8",
    )
    shutil.rmtree(project / ".apm-local")  # a re-clone: the identity is not committed

    _install_write(project, _Logger())

    DeploymentMaterializationSession(project, InstallScope.PROJECT, None)  # loads, no raise
    assert sibling.with_name(sibling.name + ".superseded").is_file(), "kept, never deleted"
    assert not sibling.exists()


@pytest.mark.parametrize("shape", ["not-json", "malformed-records"])
def test_an_unusable_sibling_receipt_stays_while_the_evidence_is_set_aside(
    project: Path, shape: str
) -> None:
    """Copilot #5403: a sibling receipt that is not well-formed is never archived (the
    ledger keeps reporting it), and it never undoes the evidence set-aside or its warning."""
    import shutil

    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope

    _install_write(project, _Logger())
    session = DeploymentMaterializationSession(project, InstallScope.PROJECT, None)
    evidence, sibling = session.mcp_state_path, session.state_path
    old = evidence.read_bytes()
    body = {**session.binding(), "records": [{"bad": 1}], "destinations": {}, "bindings": {}}
    sibling.write_text("{not json" if shape == "not-json" else json.dumps(body), encoding="utf-8")
    before = sibling.read_bytes()
    shutil.rmtree(project / ".apm-local")
    logger = _Logger()

    _install_write(project, logger)

    _assert_set_aside(logger.warnings, evidence, old)
    assert sibling.read_bytes() == before, "an unusable sibling stays in place"


def _null_root_identity(data: dict) -> None:
    data["root_identity"] = None


def _string_root_identity(data: dict) -> None:
    data["root_identity"] = "corrupt"


def _device_only_change(data: dict) -> None:
    """Same UUID and inode, another device and no matching birth time: ambiguous."""
    data["root_identity"][0] += 1
    data["root_birth_time"] = 1.0


def _another_uuid_no_writes(data: dict) -> None:
    data["installation"] = "0" * 32
    del data["writes"]


@pytest.mark.parametrize(
    "corrupt",
    [
        "text",
        _null_root_identity,
        _string_root_identity,
        _device_only_change,
        _another_uuid_no_writes,
    ],
    ids=[
        "not-json",
        "null-root-identity",
        "string-root-identity",
        "device-only-change",
        "another-uuid-no-writes",
    ],
)
def test_corrupt_evidence_is_never_set_aside_by_an_install(project: Path, corrupt) -> None:
    """Only well-formed evidence with positive proof of another installation rotates."""
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    if corrupt == "text":
        path.write_text("{not json")
    else:
        data = json.loads(path.read_text())
        corrupt(data)
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert SERVER in _servers(project)
    assert path.read_bytes() == before, "corrupt evidence stays in place"
    assert not path.with_name(path.name + ".superseded").exists()
    assert logger.warnings and "Kept every MCP entry" in logger.warnings[0]


def _assert_set_aside(warnings: list[str], path: Path, old: bytes) -> None:
    aside = path.with_name(path.name + ".superseded")
    assert len(warnings) == 1 and "Set aside MCP write evidence" in warnings[0], warnings
    assert aside.read_bytes() == old, "the old evidence is kept byte for byte"


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


def test_the_exact_written_key_is_recorded_not_an_identical_user_entry(project: Path) -> None:
    """A registry server written under its short key never claims the user's full-name entry."""
    full = "owner/demo"
    _write_entries(project, {full: ENTRY})
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        _write_entries(project, {**_existing(project), "demo": ENTRY})
        note_mcp_write("copilot", full, mcp_native_hash(ENTRY), None)
    logger = _Logger()

    _retire(project, logger)

    assert set(json.loads(_config(project).read_text())["mcpServers"]) == {full}
    assert logger.removed == {full}


def test_a_declared_server_on_the_same_native_key_keeps_its_entry(project: Path) -> None:
    """Replacing registry ``owner/demo`` with a self-defined ``demo`` keeps the shared entry."""
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        _write_entries(project, {"demo": ENTRY})
        note_mcp_write("copilot", "owner/demo", mcp_native_hash(ENTRY), None)
    before = _config(project).read_bytes()

    retire_undeclared_mcp_writes(
        {"demo"}, project_root=project, user_scope=False, exclude=None, logger=_Logger()
    )

    assert _config(project).read_bytes() == before


@pytest.mark.parametrize("exclude", ["Copilot", "copilot"])
def test_an_excluded_runtime_is_never_retired(project: Path, exclude: str) -> None:
    _install_write(project, _Logger())
    before = _config(project).read_bytes()

    retire_undeclared_mcp_writes(
        set(), project_root=project, user_scope=False, exclude=exclude, logger=_Logger()
    )

    assert _config(project).read_bytes() == before


def test_an_unreadable_config_keeps_its_evidence_and_reports_it(project: Path) -> None:
    _install_write(project, _Logger())
    _config(project).write_text("{not json")
    logger = _Logger()

    _retire(project, logger)
    forget_removed_mcp_writes(project, user_scope=False)

    assert _config(project).read_text() == "{not json"
    assert [w for w in logger.warnings if f"Kept MCP server '{SERVER}'" in w]
    kept = kept_mcp_writes(project, user_scope=False, managed=())
    assert [write.name for write, _ in kept] == [SERVER]
    _write_entries(project, {SERVER: ENTRY})
    _retire(project, _Logger())
    assert _config(project).read_text().count(SERVER) == 0, "repaired: the entry is retired"


def test_a_removal_that_does_not_happen_keeps_the_record(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_write(project, _Logger())
    monkeypatch.setattr(
        "apm_cli.integration.mcp_integrator._clean_json_mcp_config", lambda *a, **k: 0
    )
    logger = _Logger()

    _retire(project, logger)

    assert SERVER in _config(project).read_text() and not logger.removed
    assert [w for w in logger.warnings if f"Kept MCP server '{SERVER}'" in w]
    kept = kept_mcp_writes(project, user_scope=False, managed=())
    assert [write.name for write, _ in kept] == [SERVER]


def test_exact_key_removal_handles_hermes_yaml_and_opencode_v2(project: Path) -> None:
    import yaml

    from apm_cli.factory import ClientFactory
    from apm_cli.integration.mcp_integrator_install import _remove_native_entry

    hermes = ClientFactory.create_client("hermes", project_root=project, user_scope=True)
    hermes_cfg = Path(hermes.get_config_path())
    hermes_cfg.parent.mkdir(parents=True)
    hermes_cfg.write_text("mcp_servers:\n  demo:\n    command: x\n  keep:\n    command: y\n")
    opencode = ClientFactory.create_client("opencode", project_root=project, user_scope=False)
    opencode_cfg = Path(opencode.get_config_path())
    v2 = {"mcp": {"servers": {"demo": {"command": ["x"]}, "keep": {"command": ["y"]}}}}
    opencode_cfg.write_text(json.dumps(v2))

    _remove_native_entry(hermes, "demo", NullCommandLogger())
    _remove_native_entry(opencode, "demo", NullCommandLogger())

    assert set(yaml.safe_load(hermes_cfg.read_text())["mcp_servers"]) == {"keep"}
    assert set(json.loads(opencode_cfg.read_text())["mcp"]["servers"]) == {"keep"}


def test_audit_reports_unusable_evidence(project: Path) -> None:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.policy.ci_checks import _kept_mcp_entries

    _install_write(project, _Logger())
    McpWriteReceipts.open(project, user_scope=False).path.write_text("{not json")
    before = _config(project).read_bytes()

    (check,) = _kept_mcp_entries(project, LockFile(), user_scope=False, lockless=True)

    assert check.name == "kept-mcp-entries" and not check.passed
    assert any("Invalid MCP write evidence" in detail for detail in check.details)
    assert _config(project).read_bytes() == before, "audit must not change the config"
    locked = _kept_mcp_entries(project, LockFile(), user_scope=False)
    assert locked == () and _config(project).read_bytes() == before, "a lockfile governs"


def test_audit_reads_user_scope_evidence_from_the_deployment_root(project: Path) -> None:
    from apm_cli.factory import ClientFactory
    from apm_cli.policy.ci_checks import run_baseline_checks

    home = Path.home()
    config = Path(
        ClientFactory.create_client("copilot", project_root=home, user_scope=True).get_config_path()
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    with journal_mcp_writes(home, user_scope=True, logger=_Logger()):
        config.write_text(json.dumps({"mcpServers": {SERVER: ENTRY}}))
        note_mcp_write("copilot", SERVER, mcp_native_hash(ENTRY))
    config.write_text(json.dumps({"mcpServers": {SERVER: {**ENTRY, "args": ["--edited"]}}}))
    retire_undeclared_mcp_writes(
        set(), project_root=home, user_scope=True, exclude=None, logger=_Logger()
    )
    apm_home = home / ".apm"
    apm_home.mkdir(exist_ok=True)
    (apm_home / "apm.yml").write_text("name: user\nversion: 1.0.0\n")

    result = run_baseline_checks(apm_home, ci_mode=True)

    kept = [check for check in result.checks if check.name == "kept-mcp-entries"]
    assert kept and not kept[0].passed, [check.name for check in result.checks]
    assert SERVER in config.read_text(), "the kept user-scope entry stays configured"


def test_a_self_defined_server_with_a_full_name_does_not_protect_the_short_key(
    project: Path,
) -> None:
    """``owner/demo`` (under ``demo``) replaced by self-defined ``other/demo``: ``demo`` retires."""
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        _write_entries(project, {"demo": ENTRY, "other/demo": ENTRY})
        note_mcp_write("copilot", "owner/demo", mcp_native_hash(ENTRY), None)

    retire_undeclared_mcp_writes(
        {"other/demo"},
        declared_configs={"other/demo": {"name": "other/demo", "registry": False}},
        project_root=project,
        user_scope=False,
        exclude=None,
        logger=_Logger(),
    )

    assert set(json.loads(_config(project).read_text())["mcpServers"]) == {"other/demo"}


def test_audit_reports_symlinked_evidence_without_following_it(
    project: Path, tmp_path: Path
) -> None:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.policy.ci_checks import _kept_mcp_entries

    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(path.read_text())
    path.unlink()
    path.symlink_to(elsewhere)
    before = elsewhere.read_bytes()

    (check,) = _kept_mcp_entries(project, LockFile(), user_scope=False, lockless=True)

    assert not check.passed
    assert any("Unusable MCP write evidence" in detail for detail in check.details)
    assert elsewhere.read_bytes() == before


@pytest.mark.parametrize("writes", [{}, ""])
def test_a_receipt_whose_writes_is_not_a_list_is_unusable(project: Path, writes) -> None:
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    data = json.loads(path.read_text())
    data["writes"] = writes
    path.write_text(json.dumps(data))
    before = _config(project).read_bytes()

    _retire(project, _Logger())

    assert _config(project).read_bytes() == before
    with pytest.raises(ValueError, match="Invalid MCP write evidence"):
        McpWriteReceipts.open(project, user_scope=False)


def test_a_config_the_remover_cannot_rewrite_keeps_the_entry(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.install.errors import RequiredIntegrationError

    def refuse(*_args, **_kwargs):
        raise RequiredIntegrationError("cannot rewrite")

    _install_write(project, _Logger())
    monkeypatch.setattr("apm_cli.integration.mcp_integrator._clean_json_mcp_config", refuse)
    logger = _Logger()

    _retire(project, logger)

    assert SERVER in _config(project).read_text() and not logger.removed
    assert [w for w in logger.warnings if f"Kept MCP server '{SERVER}'" in w]


# -- follow-ups (#4801) --------------------------------------------------------


def _user_write(home: Path, profile: str, logger: _Logger) -> Path:
    """A user-scope install with ``COPILOT_HOME`` set to *profile*: write and note the entry."""
    from apm_cli.factory import ClientFactory

    config = Path(
        ClientFactory.create_client("copilot", project_root=home, user_scope=True).get_config_path()
    )
    assert config.parent == home / profile
    config.parent.mkdir(parents=True, exist_ok=True)
    with journal_mcp_writes(home, user_scope=True, logger=logger):
        config.write_text(json.dumps({"mcpServers": {SERVER: ENTRY}}))
        note_mcp_write("copilot", SERVER, mcp_native_hash(ENTRY))
    return config


def test_each_runtime_profile_keeps_its_own_record(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    monkeypatch.setenv("COPILOT_HOME", str(home / "a"))
    first = _user_write(home, "a", _Logger())
    monkeypatch.setenv("COPILOT_HOME", str(home / "b"))
    second = _user_write(home, "b", _Logger())

    configs = {w.config for w in McpWriteReceipts.open(home, user_scope=True).writes.values()}
    assert configs == {first.absolute().as_posix(), second.absolute().as_posix()}

    logger = _Logger()
    retire_undeclared_mcp_writes(
        set(), project_root=home, user_scope=True, exclude=None, logger=logger
    )

    assert SERVER not in json.loads(second.read_text())["mcpServers"]
    assert SERVER in json.loads(first.read_text())["mcpServers"], "another profile changed"
    assert not logger.warnings, "another profile's record is not this install's to report"
    monkeypatch.setenv("COPILOT_HOME", str(home / "a"))
    retire_undeclared_mcp_writes(
        set(), project_root=home, user_scope=True, exclude=None, logger=_Logger()
    )
    assert SERVER not in json.loads(first.read_text())["mcpServers"]
    assert McpWriteReceipts.open(home, user_scope=True).writes == {}


def test_a_record_of_another_config_is_left_untouched(project: Path) -> None:
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    data = json.loads(path.read_text())
    data["writes"][0]["config"] = "/elsewhere/mcp.json"
    path.write_text(json.dumps(data))
    before = _config(project).read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert _config(project).read_bytes() == before
    assert not logger.warnings and not logger.removed
    (write,) = McpWriteReceipts.open(project, user_scope=False).writes.values()
    assert write.config == "/elsewhere/mcp.json" and not write.kept
    assert kept_mcp_writes(project, user_scope=False, managed=()) == []


def test_a_kept_entry_is_announced_while_a_lockfile_governs(project: Path) -> None:
    _install_write(project, _Logger())
    _write_entries(project, {SERVER: {**ENTRY, "args": ["--edited"]}})
    _retire(project, _Logger())
    before = _config(project).read_bytes()
    logger = _Logger()

    retire_undeclared_mcp_writes(
        set(), project_root=project, user_scope=False, exclude=None, logger=logger, retire=False
    )

    assert _config(project).read_bytes() == before
    assert [w for w in logger.warnings if f"Kept MCP server '{SERVER}' in .mcp.json" in w]
    declared = _Logger()
    retire_undeclared_mcp_writes(
        {SERVER},
        project_root=project,
        user_scope=False,
        exclude=None,
        logger=declared,
        retire=False,
    )
    assert not declared.warnings, "a declared server is not announced as kept"


def test_unusable_evidence_met_without_a_lockfile_stays_reported(project: Path) -> None:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.policy.ci_checks import _kept_mcp_entries

    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    path.write_text("{not json")
    assert _kept_mcp_entries(project, LockFile(), user_scope=False) == (), "not yet met"

    _retire(project, _Logger())

    (check,) = _kept_mcp_entries(project, LockFile(), user_scope=False)
    assert any("Invalid MCP write evidence" in detail for detail in check.details)
    logger = _Logger()
    retire_undeclared_mcp_writes(
        set(), project_root=project, user_scope=False, exclude=None, logger=logger, retire=False
    )
    assert [w for w in logger.warnings if "Kept every MCP entry" in w]
    assert path.read_text() == "{not json", "the unusable evidence itself is left as is"

    path.unlink()
    assert _kept_mcp_entries(project, LockFile(), user_scope=False) == ()
    _retire(project, _Logger())
    assert not list(path.parent.glob("*.unusable")), "the note is cleared once evidence is usable"


def test_the_unusable_note_is_never_written_through_a_linked_directory(
    project: Path, tmp_path: Path
) -> None:
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    receipts = path.parent.parent
    elsewhere = tmp_path / "elsewhere"
    receipts.rename(elsewhere)
    receipts.symlink_to(elsewhere, target_is_directory=True)

    _retire(project, _Logger())

    assert SERVER in _servers(project), "unusable evidence must not remove anything"
    assert not list(elsewhere.rglob("*.unusable")), "nothing is written outside APM_HOME"


def test_a_located_rewrite_supersedes_the_key_less_record(project: Path) -> None:
    """#4861: ``owner/demo`` recorded key-less, then rewritten under ``demo``: one record."""
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        note_mcp_write("copilot", "owner/demo", mcp_native_hash(ENTRY), None)
    assert [w.key for w in McpWriteReceipts.open(project, user_scope=False).writes.values()] == [
        None
    ]
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        _write_entries(project, {"demo": ENTRY})
        note_mcp_write("copilot", "owner/demo", mcp_native_hash(ENTRY), None)
    _retire(project, _Logger())
    logger = _Logger()

    retire_undeclared_mcp_writes(
        set(), project_root=project, user_scope=False, exclude=None, logger=logger, retire=False
    )

    assert "demo" not in _existing(project), "the proven rewrite retires"
    assert McpWriteReceipts.open(project, user_scope=False).writes == {}
    assert not logger.warnings, "no stale key-less record is announced"
    assert kept_mcp_writes(project, user_scope=False, managed=()) == []


def test_announce_only_skips_a_server_redeclared_on_the_same_native_key(project: Path) -> None:
    """#4861: a kept ``owner/demo`` (key ``demo``) redeclared as self-defined ``demo``."""
    with journal_mcp_writes(project, user_scope=False, logger=_Logger()):
        _write_entries(project, {"demo": ENTRY})
        note_mcp_write("copilot", "owner/demo", mcp_native_hash(ENTRY), None)
    _write_entries(project, {"demo": {**ENTRY, "args": ["--edited"]}})
    _retire(project, _Logger())
    before = _config(project).read_bytes()
    logger = _Logger()

    retire_undeclared_mcp_writes(
        {"demo"}, project_root=project, user_scope=False, exclude=None, logger=logger, retire=False
    )

    assert _config(project).read_bytes() == before, "announce-only never edits the config"
    assert not logger.warnings, logger.warnings
    assert kept_mcp_writes(project, user_scope=False, managed={"demo"}) == [], "audit agrees"


def test_symlinked_evidence_is_reported_on_a_lockless_install(
    project: Path, tmp_path: Path
) -> None:
    """#4861: unusable evidence that is not McpWriteEvidenceError is still announced."""
    _install_write(project, _Logger())
    path = McpWriteReceipts.open(project, user_scope=False).path
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(path.read_text())
    path.unlink()
    path.symlink_to(elsewhere)
    before = _config(project).read_bytes()
    logger = _Logger()

    _retire(project, logger)

    assert _config(project).read_bytes() == before, "unusable evidence must not remove anything"
    assert [w for w in logger.warnings if "Kept every MCP entry APM no longer deploys" in w]
