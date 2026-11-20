"""Copilot project MCP claim and retirement decisions."""

from __future__ import annotations

import pytest

from apm_cli.install.mcp.ownership import (
    claim_shared_destination,
    inherit_shared_destination_claims,
    names_owned_by_active_peers,
)
from apm_cli.install.mcp.retired_destinations import (
    healable_names,
    lock_predates_root_mcp_json,
)
from apm_cli.models.manifest_targets import ManifestTargetDeclaration

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-legacy-mcp-ownership-key-migration")]


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


def test_retired_entries_move_only_after_their_replacement_is_owned() -> None:
    retired = {"copilot": {"written", "failed", "dropped"}, "vscode": {"written"}}

    healable = healable_names(retired, declared={"written", "failed"}, copilot_claims={"written"})

    assert healable == {"copilot": {"written", "dropped"}, "vscode": {"written"}}
    assert healable_names(retired, declared={"written"}, copilot_claims=None) == retired


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
