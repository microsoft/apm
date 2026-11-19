"""Unit contracts for the Copilot project MCP destination owners (#3866, #3868)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.adapters.client.copilot_project import CopilotProjectClientAdapter
from apm_cli.factory import ClientFactory
from apm_cli.install.mcp.ownership import (
    NativeMcpConnectionState,
    claim_shared_destination,
    compare_native_mcp_connection,
    inherit_shared_destination_claims,
    mcp_native_hash,
    names_owned_by_active_peers,
)
from apm_cli.install.mcp.retired_destinations import (
    heal_retired_project_mcp_destinations,
    healable_names,
    lock_predates_root_mcp_json,
    restore_pending_retired_claims,
    split_mcp_claims,
)
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.manifest_targets import ManifestTargetDeclaration

_ECHO = {"name": "echo", "registry": False, "transport": "stdio", "command": "echo"}
_LEGACY_COPILOT = {"type": "local", "tools": ["*"], "id": "", "command": "echo", "args": []}


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_factory_routes_only_project_copilot_to_root_mcp_json(tmp_path: Path) -> None:
    project = ClientFactory.create_client("copilot", project_root=tmp_path)
    user = ClientFactory.create_client("copilot", project_root=tmp_path, user_scope=True)

    assert isinstance(project, CopilotProjectClientAdapter)
    assert Path(project.get_config_path()) == tmp_path / ".mcp.json"
    assert not isinstance(user, CopilotProjectClientAdapter)
    assert Path(user.get_config_path()).name == "mcp-config.json"
    with pytest.raises(ValueError, match="project-scope only"):
        CopilotProjectClientAdapter(project_root=tmp_path, user_scope=True)


def test_copilot_and_claude_render_one_identical_entry(tmp_path: Path) -> None:
    info = {"name": "echo", "is_self_defined": True, "_raw_stdio": {"command": "echo"}}
    copilot = ClientFactory.create_client("copilot", project_root=tmp_path)
    claude = ClientFactory.create_client("claude", project_root=tmp_path)

    assert copilot.render_server_config(info) == claude.render_server_config(info)


@pytest.mark.parametrize(
    ("claude_copy", "expected"),
    [(False, ".agents/skills/tool/run.sh"), (True, ".claude/skills/tool/run.sh")],
)
def test_skill_launcher_uses_claude_copy_only_when_it_exists(
    tmp_path: Path, claude_copy: bool, expected: str
) -> None:
    """Claude and Copilot resolve one launcher, so their shared entry renders identically."""
    if claude_copy:
        (tmp_path / ".claude/skills/tool").mkdir(parents=True)
    launcher = {**_ECHO, "command": ".agents/skills/tool/run.sh"}
    renders = [
        ClientFactory.create_client(runtime, project_root=tmp_path).render_server_config(
            MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(launcher))
        )
        for runtime in ("claude", "copilot")
    ]

    assert renders[0] == renders[1]
    assert renders[0]["command"] == expected


def test_receipt_proves_an_entry_whose_launcher_moved(tmp_path: Path) -> None:
    """Adding Claude's skill copy re-points the launcher; APM's own entry updates."""
    launcher = {**_ECHO, "command": ".agents/skills/tool/run.sh"}
    written = {"type": "stdio", "command": ".agents/skills/tool/run.sh", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"echo": written}})
    (tmp_path / ".claude/skills/tool").mkdir(parents=True)
    client = CopilotProjectClientAdapter(project_root=tmp_path)
    info = MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(launcher))

    def state(native_hash: str) -> NativeMcpConnectionState:
        return compare_native_mcp_connection(
            client,
            "echo",
            requested_info=info,
            stored_config=launcher,
            managed=True,
            native_hash=native_hash,
            declaration_changed=False,
        )

    assert state(mcp_native_hash(client.managed_server_config(written))) is (
        NativeMcpConnectionState.UPDATE_MANAGED
    )
    # Without a matching receipt the moved entry is still not APM's to touch.
    assert state("sha256:other") is NativeMcpConnectionState.CONFLICT


def test_peer_claim_is_adopted_and_refreshed_after_a_peer_write() -> None:
    owned = {"claude": {"echo"}, "copilot": set()}
    hashes = {"claude": {"echo": "sha256:new"}}

    claim_shared_destination(owned, hashes, "copilot", "echo", user_scope=False)
    assert owned["copilot"] == {"echo"}
    assert hashes["copilot"]["echo"] == "sha256:new"

    hashes["copilot"]["echo"] = "sha256:old"
    claim_shared_destination(
        owned, hashes, "copilot", "echo", user_scope=False, written_connections=set()
    )
    assert hashes["copilot"]["echo"] == "sha256:old"
    claim_shared_destination(
        owned,
        hashes,
        "copilot",
        "echo",
        user_scope=False,
        written_connections={("claude", "echo")},
    )
    assert hashes["copilot"]["echo"] == "sha256:new"


def test_user_scope_never_shares_claims() -> None:
    owned = {"claude": {"echo"}, "copilot": set()}

    claim_shared_destination(owned, {}, "copilot", "echo", user_scope=True)

    assert owned["copilot"] == set()


def test_retiring_runtime_passes_claims_and_keeps_peer_entries() -> None:
    owned = {"claude": {"echo", "other"}, "copilot": set()}
    hashes = {"claude": {"echo": "sha256:a"}}

    inherit_shared_destination_claims(owned, hashes, active_runtimes={"copilot"}, user_scope=False)

    assert owned["copilot"] == {"echo", "other"}
    assert hashes["copilot"] == {"echo": "sha256:a"}
    assert names_owned_by_active_peers(
        "claude", {"echo", "gone"}, {"copilot": {"echo"}}, user_scope=False
    ) == {"echo"}


@pytest.mark.parametrize(
    ("version", "expected"),
    [(None, True), ("0.36.1", True), ("0.35.1", True), ("0.37.0", False), ("1.2.0", False)],
)
def test_heal_runs_only_for_locks_written_before_root_mcp_json(
    version: str | None, expected: bool
) -> None:
    assert lock_predates_root_mcp_json(version) is expected


def test_heal_removes_only_proven_apm_entries_and_keeps_the_file(tmp_path: Path) -> None:
    edited = {**_LEGACY_COPILOT, "args": ["--mine"]}
    _write(
        tmp_path / ".github/mcp.json",
        {"mcpServers": {"echo": _LEGACY_COPILOT, "edited": edited, "user": {"command": "u"}}},
    )
    configs = {"echo": _ECHO, "edited": {**_ECHO, "name": "edited"}}

    heal = heal_retired_project_mcp_destinations(
        {"copilot": {"echo", "edited"}},
        stored_configs=configs,
        native_hashes={},
        project_root=tmp_path,
    )

    assert heal.removed == {"copilot": {"echo"}}
    assert heal.kept == {"copilot": {"edited"}}
    remaining = json.loads((tmp_path / ".github/mcp.json").read_text(encoding="utf-8"))
    assert set(remaining["mcpServers"]) == {"edited", "user"}


def test_dry_run_preview_names_exactly_what_the_heal_moves(tmp_path: Path) -> None:
    """``install --dry-run`` previews the .mcp.json move without writing (#3967)."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.mcp.retired_destinations import preview_retired_mcp_moves

    edited = {**_LEGACY_COPILOT, "args": ["--mine"]}
    retired = tmp_path / ".github/mcp.json"
    _write(retired, {"mcpServers": {"echo": _LEGACY_COPILOT, "edited": edited}})
    before = retired.read_bytes()
    lock = LockFile()
    lock.apm_version = "0.36.1"
    lock.mcp_target_servers = {"copilot": ["echo", "edited"]}
    lock.mcp_configs = {"echo": _ECHO, "edited": {**_ECHO, "name": "edited"}}

    assert preview_retired_mcp_moves(lock, tmp_path) == {".github/mcp.json": ["echo"]}
    assert retired.read_bytes() == before
    lock.apm_version = "0.37.0"
    assert preview_retired_mcp_moves(lock, tmp_path) == {}


def test_heal_keeps_an_emptied_github_mcp_json(tmp_path: Path) -> None:
    """.github/mcp.json is a copilot detection signal, so the heal never deletes it."""
    _write(tmp_path / ".github/mcp.json", {"mcpServers": {"echo": _LEGACY_COPILOT}})

    heal = heal_retired_project_mcp_destinations(
        {"copilot": {"echo"}},
        stored_configs={"echo": _ECHO},
        native_hashes={},
        project_root=tmp_path,
    )

    assert heal.removed == {"copilot": {"echo"}}
    assert heal.deleted_files == []
    assert json.loads((tmp_path / ".github/mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {}
    }


def _split(owned: dict, hashes: dict, root: Path, version: str = "0.36.1", **kwargs):
    return split_mcp_claims(
        owned, hashes, apm_version=version, project_root=root, user_scope=False, **kwargs
    )


def test_retired_claims_never_become_root_ownership(tmp_path: Path) -> None:
    """Retired claims prove writes to the retired files only; only the heal sees them.

    A legacy copilot claim kept as .mcp.json ownership would let the install
    adopt, overwrite or delete a user-authored root entry of the same name.
    """
    owned = {"copilot": {"a"}, "vscode": {"b"}, "claude": {"c"}}
    live = {"copilot": {"a": "sha256:github"}, "vscode": {"b": "sha256:vscode"}}

    retired = _split(owned, live, tmp_path)

    assert retired.names == {"copilot": {"a"}, "vscode": {"b"}}
    assert retired.hashes == {"copilot": {"a": "sha256:github"}, "vscode": {"b": "sha256:vscode"}}
    assert owned == {"copilot": set(), "claude": {"c"}}
    assert live == {"copilot": {}}


def test_user_scope_claims_are_never_split(tmp_path: Path) -> None:
    owned = {"copilot": {"a"}}

    retired = split_mcp_claims(
        owned, {}, apm_version="0.36.1", project_root=tmp_path, user_scope=True
    )

    assert retired.names == {}
    assert owned == {"copilot": {"a"}}


def test_names_already_moved_to_root_mcp_json_are_settled(tmp_path: Path) -> None:
    """A recorded copilot fingerprint matching .mcp.json means an earlier install moved it."""
    entry = {"type": "stdio", "command": "echo", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"moved": entry, "edited": {"command": "x"}}})
    hashes = {"copilot": {"moved": mcp_native_hash(entry), "edited": mcp_native_hash(entry)}}

    retired = _split({"copilot": {"moved", "edited", "pending"}}, hashes, tmp_path)

    assert retired.names == {"copilot": {"edited", "pending"}}


def test_vscode_claims_are_retired_even_when_copilot_settled(tmp_path: Path) -> None:
    """Settling refers to copilot's .mcp.json entry; a vscode claim names .vscode/mcp.json."""
    entry = {"type": "stdio", "command": "echo", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"moved": entry}})
    owned = {"copilot": {"moved"}, "vscode": {"moved"}}

    retired = _split(owned, {"copilot": {"moved": mcp_native_hash(entry)}}, tmp_path)

    assert retired.names == {"vscode": {"moved"}}
    assert owned == {"copilot": {"moved"}}


def test_registry_name_settles_under_its_native_key(tmp_path: Path) -> None:
    """The lock records a registry name; .mcp.json keys the entry by its short name."""
    entry = {"type": "http", "url": "https://mcp.example.invalid/registry-lifecycle"}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"registry-lifecycle": entry}})
    name = "com.example/registry-lifecycle"

    retired = _split(
        {"copilot": {name}},
        {"copilot": {name: mcp_native_hash(entry)}},
        tmp_path,
        stored_configs={name: {"name": name, "registry": "https://registry.example"}},
    )

    assert retired.names == {}


def test_unhealed_claims_round_trip_under_copilot_by_fingerprint(tmp_path: Path) -> None:
    """A lock never records vscode; a kept claim's fingerprint names its retired file.

    Only an entry APM provably wrote is kept, so a user-edited retired entry
    never becomes a claim, and a newer lock reads each kept claim back as
    retired (never as .mcp.json ownership).
    """
    vscode_entry = {"type": "stdio", "command": "echo", "args": []}
    _write(
        tmp_path / ".github/mcp.json",
        {"mcpServers": {"gh": _LEGACY_COPILOT, "gh-edited": {**_LEGACY_COPILOT, "args": ["x"]}}},
    )
    _write(tmp_path / ".vscode/mcp.json", {"servers": {"vs": vscode_entry}})
    configs = {name: {**_ECHO, "name": name} for name in ("gh", "gh-edited", "vs", "done")}
    retired = _split({"copilot": {"gh", "gh-edited", "done"}, "vscode": {"vs"}}, {}, tmp_path)
    claims: dict[str, set[str]] = {"copilot": {"done"}}
    hashes: dict[str, dict[str, str]] = {"copilot": {"done": "sha256:root"}}

    restore_pending_retired_claims(
        retired,
        {"copilot": {"done"}},
        target_servers=claims,
        native_hashes=hashes,
        project_root=tmp_path,
        stored_configs=configs,
    )

    assert claims == {"copilot": {"done", "gh", "vs"}}
    assert set(hashes["copilot"]) == {"done", "gh", "vs"}
    again = _split(claims, hashes, tmp_path, version="0.37.0", stored_configs=configs)
    assert again.names == {"copilot": {"gh"}, "vscode": {"vs"}}
    assert claims == {"copilot": {"done"}}


def test_kept_vscode_claim_never_settles_against_an_identical_root_entry(tmp_path: Path) -> None:
    """A .vscode stdio entry fingerprints like its .mcp.json twin; the receipt tells them apart."""
    entry = {"type": "stdio", "command": "echo", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"echo": entry}})
    _write(tmp_path / ".vscode/mcp.json", {"servers": {"echo": entry}})
    configs = {"echo": _ECHO}
    claims: dict[str, set[str]] = {}
    hashes: dict[str, dict[str, str]] = {}
    restore_pending_retired_claims(
        _split({"vscode": {"echo"}}, {}, tmp_path),
        {},
        target_servers=claims,
        native_hashes=hashes,
        project_root=tmp_path,
        stored_configs=configs,
    )
    assert hashes["copilot"]["echo"] != mcp_native_hash(entry)

    again = _split(claims, hashes, tmp_path, version="0.37.0", stored_configs=configs)

    assert again.names == {"vscode": {"echo"}}
    assert claims == {"copilot": set()}


def test_newer_lock_live_claim_is_not_retired_by_an_identical_retired_copy(
    tmp_path: Path,
) -> None:
    """A 0.37 root fingerprint never names a user's identical .vscode/mcp.json copy."""
    entry = {"type": "stdio", "command": "echo", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"echo": {**entry, "args": ["edited"]}}})
    _write(tmp_path / ".vscode/mcp.json", {"servers": {"echo": entry}})
    claims = {"copilot": {"echo"}}

    retired = _split(
        claims, {"copilot": {"echo": mcp_native_hash(entry)}}, tmp_path, version="0.37.0"
    )

    assert retired.names == {}
    assert claims == {"copilot": {"echo"}}


def test_retired_entries_move_only_after_their_replacement_is_owned() -> None:
    retired = {"copilot": {"written", "failed", "dropped"}, "vscode": {"written"}}

    healable = healable_names(retired, declared={"written", "failed"}, copilot_claims={"written"})

    assert healable == {"copilot": {"written", "dropped"}, "vscode": {"written"}}
    assert healable_names(retired, declared={"written"}, copilot_claims=None) == retired


def test_heal_accepts_a_matching_fingerprint_and_deletes_an_apm_only_file(tmp_path: Path) -> None:
    entry = {"type": "stdio", "command": "echo", "args": ["custom"]}
    _write(tmp_path / ".vscode/mcp.json", {"servers": {"echo": entry}, "inputs": []})

    heal = heal_retired_project_mcp_destinations(
        {"vscode": {"echo"}},
        stored_configs={"echo": _ECHO},
        native_hashes={"vscode": {"echo": mcp_native_hash(entry)}},
        project_root=tmp_path,
    )

    assert heal.removed == {"vscode": {"echo"}}
    assert heal.deleted_files == [".vscode/mcp.json"]
    assert not (tmp_path / ".vscode/mcp.json").exists()


def test_heal_never_follows_a_symlinked_retired_file(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    _write(real, {"mcpServers": {"echo": _LEGACY_COPILOT}})
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/mcp.json").symlink_to(real)

    heal = heal_retired_project_mcp_destinations(
        {"copilot": {"echo"}},
        stored_configs={"echo": _ECHO},
        native_hashes={},
        project_root=tmp_path,
    )

    assert heal.removed == {}
    assert "echo" in json.loads(real.read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.parametrize(
    ("value", "user_scope", "expected"),
    [
        ("claude,copilot", False, ("claude", "copilot")),
        (["agents", "claude"], False, ("copilot", "claude")),
        ("vscode", False, ("copilot",)),
        (None, False, ()),
        ("intellij", False, None),
        ("all", True, None),
    ],
)
def test_targets_replacement_deploys_identically_or_refuses(
    value: object, user_scope: bool, expected: tuple[str, ...] | None
) -> None:
    declaration = ManifestTargetDeclaration.from_mapping({"target": value})

    assert declaration.targets_replacement(user_scope=user_scope) == expected


def test_singular_all_replacement_is_the_explicit_install_set() -> None:
    from apm_cli.core.target_catalog import expand_all

    declaration = ManifestTargetDeclaration.from_mapping({"target": "all"})

    assert declaration.targets_replacement() == expand_all("install")
    assert "copilot" in expand_all("install")
    assert "vscode" not in expand_all("install")


def test_root_mcp_json_is_not_a_target_detection_signal(tmp_path: Path) -> None:
    """Claude Code reads .mcp.json too, so it names no single harness; .github/mcp.json does."""
    from apm_cli.core.target_detection import detect_signals

    _write(tmp_path / ".mcp.json", {"mcpServers": {}})
    assert detect_signals(tmp_path) == []

    _write(tmp_path / ".github/mcp.json", {"mcpServers": {}})
    assert [signal.target for signal in detect_signals(tmp_path)] == ["copilot"]


@pytest.mark.parametrize("snapshot", [True, False], ids=["pre-install-snapshot", "stamped-only"])
def test_heal_eligibility_reads_the_pre_install_lock(tmp_path: Path, snapshot: bool) -> None:
    """The package phase may stamp the lock with 0.37 before MCP runs; the heal still runs."""
    from unittest.mock import MagicMock, patch

    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.mcp import run_mcp_integration

    _write(tmp_path / ".github/mcp.json", {"mcpServers": {"echo": _LEGACY_COPILOT}})
    stamped = LockFile(apm_version="0.37.0")
    stamped.mcp_servers = ["echo"]
    stamped.mcp_configs = {"echo": _ECHO}
    DeploymentLedgerCodec.replace_mcp_target_servers(stamped, {"copilot": ["echo"]})
    lock_path = tmp_path / "apm.lock.yaml"
    stamped.write(lock_path)
    package = MagicMock(scripts=None, targets=None, target=None, allow_executables=None)
    package.package_path = tmp_path
    dep = MCPDependency.from_dict(_ECHO)
    package.get_all_mcp_dependencies.return_value = [dep]

    def install(*_args, managed_target_servers, **_kwargs):
        managed_target_servers.setdefault("copilot", set()).add("echo")
        return 1

    with (
        patch("apm_cli.integration.mcp_integrator.MCPIntegrator") as integrator,
        patch(
            "apm_cli.install.mcp.retired_destinations.heal_retired_project_mcp_destinations"
        ) as heal,
    ):
        integrator.deduplicate.side_effect = lambda deps: deps
        integrator.install.side_effect = install
        integrator.get_server_names.return_value = {"echo"}
        heal.return_value = MagicMock(removed={}, kept={}, deleted_files=[])
        run_mcp_integration(
            apm_package=package,
            mcp_deps=[dep],
            apm_modules_path=tmp_path / "apm_modules",
            lock_path=lock_path,
            old_mcp_servers={"echo"},
            old_mcp_configs={"echo": _ECHO},
            old_mcp_provenance={},
            old_mcp_target_servers={"copilot": ["echo"]},
            project_root=tmp_path,
            user_scope=False,
            should_install=True,
            logger=InstallLogger(verbose=False),
            no_policy=True,
            pre_install_lock=LockFile(apm_version="0.36.1") if snapshot else None,
        )

    if snapshot:
        assert heal.call_args.args[0] == {"copilot": {"echo"}}
    else:
        heal.assert_not_called()


@pytest.mark.parametrize("version", ["0.36.1", "0.37.0"], ids=["raw-0.36-lock", "kept-claim"])
def test_uninstall_heals_retired_claims_and_never_touches_user_root_entries(
    tmp_path: Path, version: str
) -> None:
    """A retired copilot claim names .github/mcp.json, never the user's .mcp.json entry."""
    from apm_cli.commands.uninstall.engine import _cleanup_stale_mcp
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.utils.yaml_io import dump_yaml

    user_root = {"type": "stdio", "command": "echo", "args": []}
    _write(tmp_path / ".mcp.json", {"mcpServers": {"echo": user_root}})
    _write(tmp_path / ".github/mcp.json", {"mcpServers": {"echo": _LEGACY_COPILOT}})
    dump_yaml({"name": "app", "version": "1.0.0"}, tmp_path / "apm.yml")
    from apm_cli.adapters.client.copilot import CopilotClientAdapter
    from apm_cli.install.mcp.retired_destinations import _retired_receipt

    comparable = CopilotClientAdapter(
        project_root=tmp_path, user_scope=False
    ).managed_server_config(_LEGACY_COPILOT)
    # A raw 0.36 lock records the plain fingerprint; a kept 0.37 claim, the receipt.
    github_hash = (
        mcp_native_hash(comparable)
        if version == "0.36.1"
        else _retired_receipt("copilot", comparable)
    )
    lock = LockFile(apm_version=version)
    lock.mcp_servers = ["echo"]
    lock.mcp_configs = {"echo": _ECHO}
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lock, {"copilot": ["echo"]}, native_hashes={"copilot": {"echo": github_hash}}
    )

    logger = MagicMock()
    _cleanup_stale_mcp(
        APMPackage.from_apm_yml(tmp_path / "apm.yml"),
        lock,
        tmp_path / "apm.lock.yaml",
        {"echo"},
        modules_dir=tmp_path / "apm_modules",
        project_root=tmp_path,
        persist=False,
        logger=logger,
    )

    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == {"echo": user_root}
    assert json.loads((tmp_path / ".github/mcp.json").read_text())["mcpServers"] == {}
    assert not lock.mcp_target_servers
    # Uninstall reports the heal exactly as install does (C5d).
    progress = " ".join(str(call.args[0]) for call in logger.progress.call_args_list)
    assert "Removed APM-managed MCP server 'echo' from .github/mcp.json" in progress


def test_bundle_retarget_keeps_the_entry_the_new_runtime_owns(tmp_path: Path) -> None:
    """A bundle moving from claude to copilot retires only claude's claim."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.mcp.integration import run_owned_mcp_integration

    (tmp_path / ".claude").mkdir()
    lock_path = tmp_path / "apm.lock.yaml"
    dep = MCPDependency.from_dict(_ECHO)
    for targets in (["claude"], ["copilot"]):
        run_owned_mcp_integration(
            dependencies=[dep],
            owner="b",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=targets,
        )

    assert "echo" in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert LockFile.read(lock_path).mcp_target_servers.get("copilot") == ["echo"]


@pytest.mark.parametrize(
    ("native_timeout", "expected"),
    [
        (1, NativeMcpConnectionState.MATCHING_MANAGED),
        (True, NativeMcpConnectionState.MATCHING_FOREIGN),
    ],
)
def test_moved_root_entry_is_adopted_only_when_json_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_timeout: object, expected: object
) -> None:
    """Python equality says True == 1; adopting an unrecorded entry needs the exact JSON."""
    from apm_cli.install.mcp.ownership import adoptable_root_mcp_entries

    _write(
        tmp_path / ".mcp.json",
        {"mcpServers": {"echo": {"type": "stdio", "command": "echo", "timeout": native_timeout}}},
    )
    client = CopilotProjectClientAdapter(project_root=tmp_path)
    monkeypatch.setattr(
        client,
        "render_server_config",
        lambda *_args, **_kwargs: {"type": "stdio", "command": "echo", "timeout": 1},
    )
    info = MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(_ECHO))

    with adoptable_root_mcp_entries(frozenset({"echo"})):
        state = compare_native_mcp_connection(
            client, "echo", requested_info=info, stored_config=_ECHO, managed=False
        )

    assert state is expected
