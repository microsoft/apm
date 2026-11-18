"""Unit contracts for the Copilot project MCP destination owners (#3866, #3868)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.adapters.client.copilot_project import CopilotProjectClientAdapter
from apm_cli.factory import ClientFactory
from apm_cli.install.mcp.ownership import (
    claim_shared_destination,
    inherit_shared_destination_claims,
    mcp_native_hash,
    names_owned_by_active_peers,
)
from apm_cli.install.mcp.retired_destinations import (
    apply_retired_mcp_heal,
    heal_retired_project_mcp_destinations,
    lock_predates_root_mcp_json,
)
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
    if claude_copy:
        (tmp_path / ".claude/skills/tool").mkdir(parents=True)
    adapter = CopilotProjectClientAdapter(project_root=tmp_path)

    assert adapter._rewrite_self_defined_skill_command(".agents/skills/tool/run.sh") == expected


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

    owned = {"copilot": ["echo", "edited"]}
    hashes = {"copilot": {"echo": "sha256:x"}}
    apply_retired_mcp_heal(heal, owned, hashes)
    assert owned == {"copilot": {"edited"}}
    assert hashes == {"copilot": {}}


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
