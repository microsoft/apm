"""Tests for canonical deployment state and lockfile encoding."""

from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconciler,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.core.scope import InstallScope
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.targets import TargetProfile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("values", "bundles", "expected"),
    [
        ({"a/SKILL.md"}, {"a/SKILL.md"}, {"a/SKILL.md"}),
        ({"a"}, {"a/SKILL.md"}, {"a"}),
        ({"b"}, {"a/SKILL.md"}, set()),
        ({"a", "ab"}, {"ab/SKILL.md"}, {"ab"}),
        ({"a"}, set(), set()),
    ],
    ids=["file", "ancestor", "sibling", "prefix-not-directory", "empty"],
)
def test_local_bundle_scope_respects_directory_boundaries(
    values: set[str], bundles: set[str], expected: set[str]
) -> None:
    assert DeploymentLedgerCodec.local_bundle_scope(values, bundles) == frozenset(expected)


def _target(name: str, root: str, managed: Path | None = None) -> TargetProfile:
    capability = TARGET_CAPABILITIES.get(name)
    if capability is None:
        capability = replace(
            TARGET_CAPABILITIES["copilot"],
            name=name,
            aliases=(),
            runtimes=(),
        )
    return TargetProfile(
        capability=capability,
        root_dir=str(managed or root),
        primitives={},
        resolved_deploy_root=managed,
    )


def _locator(
    value: str = ".github/agents/demo.agent.md",
    *,
    target: str = "copilot",
    runtime: str | None = None,
) -> DeploymentLocator:
    return DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target=target,
        value=value,
        runtime=runtime,
        scope="project",
    )


def _record(locator: DeploymentLocator, owners=("old",), active="old") -> DeploymentRecord:
    return DeploymentRecord(
        locator=locator,
        owners=owners,
        active_owner=active,
        content_hash="sha256:old",
    )


def test_legacy_package_claims_share_one_handoff_decision() -> None:
    """Current ownership and prior eligibility must come from one owner."""
    shared = ".claude/skills/shared/SKILL.md"
    unique = ".claude/skills/loser-only/SKILL.md"
    reconcile = getattr(DeploymentReconciler, "reconcile_package_claims", None)

    assert callable(reconcile)
    claims = reconcile(
        package_keys=("loser", "winner"),
        current_claims={"loser": [shared, unique], "winner": [shared]},
        prior_files={"loser": [shared, unique], "winner": []},
        prior_hashes={
            "loser": {shared: "sha256:shared", unique: "sha256:unique"},
            "winner": {},
        },
    )

    assert claims["loser"].current_files == (unique,)
    assert claims["loser"].prior_files == (unique,)
    assert claims["loser"].prior_hashes == {unique: "sha256:unique"}
    assert claims["winner"].current_files == (shared,)


def test_current_claimed_paths_come_from_canonical_handoff() -> None:
    """Cleanup protection must consume the canonical current-claim owner."""
    shared = ".claude/skills/shared/SKILL.md"
    unique = ".claude/skills/loser-only/SKILL.md"

    claimed = DeploymentReconciler.current_claimed_paths(
        {
            "loser": [shared, unique],
            "winner": [shared],
        }
    )

    assert claimed == frozenset({shared, unique})


def test_codec_reports_invalid_owner_references_deterministically() -> None:
    alpha = LockedDependency(repo_url="alpha")
    lockfile = LockFile(dependencies={"alpha": alpha})
    first = _locator(".claude/rules/first.md", target="claude")
    second = _locator(".claude/rules/second.md", target="claude")
    lockfile.deployment_ledger = DeploymentLedger(
        records={
            second.key: _record(second, owners=("missing",), active="missing"),
            first.key: _record(
                first,
                owners=("alpha", "missing"),
                active="missing",
            ),
        }
    )
    lockfile._deployments_present = True

    violations = DeploymentLedgerCodec.owner_reference_violations(lockfile)

    assert [violation.locator.value for violation in violations] == [
        ".claude/rules/first.md",
        ".claude/rules/second.md",
    ]
    assert violations[0].invalid_owners == ("missing",)
    assert violations[0].invalid_active_owner == "missing"
    assert violations[1].invalid_owners == ("missing",)
    assert violations[1].invalid_active_owner == "missing"


def test_codec_valid_owner_keys_include_workspace_and_local_bundle() -> None:
    lockfile = LockFile(
        dependencies={
            "alpha": LockedDependency(repo_url="alpha"),
            "beta": LockedDependency(repo_url="beta"),
        }
    )

    assert DeploymentLedgerCodec.valid_owner_keys(lockfile) == frozenset(
        {".", "local-bundle", "alpha", "beta"}
    )
    assert DeploymentLedgerCodec.valid_owner_keys(
        lockfile,
        excluded_dependency_keys={"beta"},
    ) == frozenset({".", "local-bundle", "alpha"})


def test_deployment_record_rejects_active_owner_outside_owners() -> None:
    with pytest.raises(ValueError, match="active_owner must be present in owners"):
        _record(_locator(), owners=("survivor",), active="removed")


def test_legacy_import_and_dual_write_are_semantically_equivalent() -> None:
    dependency = LockedDependency(
        repo_url="owner/package",
        deployed_files=[".github/agents/demo.agent.md"],
        deployed_file_hashes={".github/agents/demo.agent.md": "sha256:demo"},
    )
    lockfile = LockFile()
    lockfile.add_dependency(dependency)
    lockfile.local_deployed_files = [".claude/rules/local.md"]
    lockfile.local_deployed_file_hashes = {".claude/rules/local.md": "sha256:local"}
    lockfile.mcp_target_servers = {"vscode": ["demo"]}

    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    DeploymentLedgerCodec.apply_to_lockfile(ledger, lockfile)
    rebuilt = LockFile.from_yaml(lockfile.to_yaml())

    assert rebuilt.deployment_ledger == ledger
    assert rebuilt.is_semantically_equivalent(lockfile)


def test_legacy_shared_agents_path_has_unattributable_target() -> None:
    """A shared .agents root must not be attributed to the deprecated alias."""
    locator = DeploymentLedgerCodec._legacy_locator(".agents/skills/demo/SKILL.md")

    assert locator.target == "legacy"


def test_legacy_owner_update_preserves_canonical_shared_root_locator() -> None:
    """A compatibility projection must not demote a concrete shared-root target."""
    path = ".agents/skills/demo/SKILL.md"
    owner = "owner/package"
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url=owner,
            deployed_files=[path],
            deployed_file_hashes={path: "sha256:demo"},
        )
    )
    concrete = _locator(path, target="copilot")
    lockfile.deployment_ledger = DeploymentLedger(
        records={
            concrete.key: DeploymentRecord(
                locator=concrete,
                owners=(owner,),
                active_owner=owner,
                content_hash="sha256:demo",
            )
        }
    )
    lockfile._deployments_present = True

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        owner,
        [path],
        {path: "sha256:demo"},
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert len(records) == 1
    assert records[0].locator.target == "copilot"


def test_legacy_owner_update_adopts_reconciled_shared_root_locator() -> None:
    """A reconciled locator replaces the prior unattributed shared-root target."""
    path = ".agents/skills/demo/SKILL.md"
    lockfile = LockFile()
    lockfile.local_deployed_files = [path]
    lockfile.local_deployed_file_hashes = {path: "sha256:demo"}
    concrete = _locator(path, target="copilot")
    reconciled = DeploymentLedger(
        records={
            concrete.key: DeploymentRecord(
                locator=concrete,
                owners=(".",),
                active_owner=".",
                content_hash="sha256:demo",
            )
        }
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        ".",
        [path],
        {path: "sha256:demo"},
        provenance=reconciled,
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert len(records) == 1
    assert records[0].locator.target == "copilot"
    assert records[0].owners == (".",)


def test_legacy_owner_update_adopts_only_the_reconciled_locator() -> None:
    """A co-owned path keeps its active owner, and unattributed locators never win."""
    path = ".agents/skills/shared/SKILL.md"
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="owner/dep",
            deployed_files=[path],
            deployed_file_hashes={path: "sha256:demo"},
        )
    )
    lockfile.local_deployed_files = [path]
    lockfile.local_deployed_file_hashes = {path: "sha256:demo"}
    unattributed = _locator(path, target="legacy")
    lockfile.deployment_ledger = DeploymentLedger(
        records={
            unattributed.key: DeploymentRecord(
                locator=unattributed,
                owners=(".", "owner/dep"),
                active_owner="owner/dep",
                content_hash="sha256:demo",
            )
        }
    )
    lockfile._deployments_present = True
    concrete = _locator(path, target="copilot")
    reconciled = DeploymentLedger(
        records={
            locator.key: DeploymentRecord(
                locator=locator,
                owners=("legacy",),
                active_owner="legacy",
                content_hash="sha256:demo",
            )
            for locator in (concrete, unattributed)
        }
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        ".",
        [path],
        {path: "sha256:demo"},
        provenance=reconciled,
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert len(records) == 1
    assert records[0].locator.target == "copilot"
    assert records[0].active_owner == "owner/dep"


@pytest.mark.parametrize("current", ["codex", "copilot"])
def test_legacy_owner_provenance_prefers_a_current_target(current: str) -> None:
    """With several concrete locators for one path, the current run's target wins."""
    path = ".agents/skills/demo/SKILL.md"
    lockfile = LockFile()
    lockfile.local_deployed_files = [path]
    lockfile.local_deployed_file_hashes = {path: "sha256:demo"}
    provenance = DeploymentLedger(
        records={
            locator.key: DeploymentRecord(
                locator=locator,
                owners=(".",),
                active_owner=".",
                content_hash="sha256:demo",
            )
            for locator in (_locator(path, target="codex"), _locator(path, target="copilot"))
        }
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        ".",
        [path],
        {path: "sha256:demo"},
        provenance=provenance,
        current_targets=[_target(current, f".{current}")],
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert [record.locator.target for record in records] == [current]


def test_legacy_owner_provenance_keeps_a_fresh_current_target_on_shared_paths() -> None:
    """Old provenance never overrides a co-owned row this run already attributed."""
    path = ".agents/skills/shared/SKILL.md"
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="owner/dep",
            deployed_files=[path],
            deployed_file_hashes={path: "sha256:demo"},
        )
    )
    fresh = _locator(path, target="codex")
    old = _locator(path, target="copilot")
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            records={
                fresh.key: DeploymentRecord(
                    locator=fresh,
                    owners=("owner/dep",),
                    active_owner="owner/dep",
                    content_hash="sha256:demo",
                )
            }
        ),
        lockfile,
    )
    provenance = DeploymentLedger(
        records={
            old.key: DeploymentRecord(
                locator=old,
                owners=(".", "owner/dep"),
                active_owner="owner/dep",
                content_hash="sha256:demo",
            )
        }
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        ".",
        [path],
        {path: "sha256:demo"},
        provenance=provenance,
        current_targets=[_target("claude", ".claude"), _target("codex", ".codex")],
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert [record.locator.target for record in records] == ["codex"]


def test_legacy_owner_drops_malformed_carried_bundle_provenance() -> None:
    """A malformed carried-forward bundle hash degrades to a plain row instead of raising."""
    bundled = ".agents/skills/bundled/SKILL.md"
    marked = _locator(bundled, target="legacy")
    provenance = DeploymentLedger(
        records={
            marked.key: DeploymentRecord(
                locator=marked,
                owners=(".", "local-bundle"),
                active_owner="local-bundle",
                content_hash="sha256:not-canonical",
            )
        }
    )
    lockfile = LockFile()

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        ".",
        [bundled],
        {bundled: "sha256:not-canonical"},
        provenance=provenance,
    )

    assert DeploymentLedgerCodec.local_bundle_paths(lockfile) == frozenset()
    assert lockfile.local_deployed_files == [bundled]


def test_legacy_owner_provenance_restores_dependency_locators() -> None:
    """Provenance also restores concrete locators for a dependency owner's files."""
    path = ".agents/skills/dep/SKILL.md"
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="owner/dep"))
    concrete = _locator(path, target="copilot")
    provenance = DeploymentLedger(
        records={
            concrete.key: DeploymentRecord(
                locator=concrete,
                owners=("owner/dep",),
                active_owner="owner/dep",
                content_hash="sha256:dep",
            )
        }
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        "owner/dep",
        [path],
        {path: "sha256:dep"},
        provenance=provenance,
    )

    records = tuple(lockfile.deployment_ledger.records.values())
    assert [(record.locator.target, record.owners) for record in records] == [
        ("copilot", ("owner/dep",))
    ]


def test_renaming_a_local_path_keeps_other_bundle_provenance() -> None:
    """Renaming an authored path must not demote unrelated bundle rows."""
    authored = ".github/skills/demo/SKILL.md"
    bundled = ".agents/skills/bundled/SKILL.md"
    lockfile = LockFile()
    lockfile.local_deployed_files = [authored]
    lockfile.local_deployed_file_hashes = {authored: "sha256:authored"}
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [bundled], {bundled: f"sha256:{'b' * 64}"}, scope=InstallScope.PROJECT
    )

    DeploymentLedgerCodec.rename_local_deployed_path(
        lockfile, authored, ".agents/skills/demo/SKILL.md"
    )

    rebuilt = LockFile.from_yaml(lockfile.to_yaml())
    assert DeploymentLedgerCodec.local_bundle_paths(rebuilt) == frozenset({bundled})


def test_legacy_owner_provenance_is_scoped_to_the_owner_files() -> None:
    """Carried-forward provenance restores local rows without touching dependency rows."""
    dependency_path = ".agents/skills/dep/SKILL.md"
    bundled = ".agents/skills/bundled/SKILL.md"
    bundle_hash = f"sha256:{'b' * 64}"
    existing = LockFile()
    existing.add_dependency(
        LockedDependency(
            repo_url="owner/dep",
            deployed_files=[dependency_path],
            deployed_file_hashes={dependency_path: "sha256:dep"},
        )
    )
    DeploymentLedgerCodec.record_local_bundle_files(
        existing, [bundled], {bundled: bundle_hash}, scope=InstallScope.PROJECT
    )
    stale = _locator(dependency_path, target="copilot")
    provenance = DeploymentLedger(
        records={
            **existing.deployment_ledger.records,
            stale.key: DeploymentRecord(
                locator=stale,
                owners=("owner/dep",),
                active_owner="owner/dep",
                content_hash="sha256:dep",
            ),
        }
    )
    fresh = LockFile()
    fresh.add_dependency(
        LockedDependency(
            repo_url="owner/dep",
            deployed_files=[dependency_path],
            deployed_file_hashes={dependency_path: "sha256:dep"},
        )
    )
    current = _locator(dependency_path, target="codex")
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            records={
                current.key: DeploymentRecord(
                    locator=current,
                    owners=("owner/dep",),
                    active_owner="owner/dep",
                    content_hash="sha256:dep",
                )
            }
        ),
        fresh,
    )

    DeploymentLedgerCodec.replace_legacy_owner(
        fresh,
        ".",
        [bundled],
        {bundled: bundle_hash},
        provenance=provenance,
    )

    targets = {
        record.locator.value: record.locator.target
        for record in fresh.deployment_ledger.records.values()
    }
    assert targets[dependency_path] == "codex"
    assert DeploymentLedgerCodec.local_bundle_paths(fresh) == frozenset({bundled})


def test_local_bundle_provenance_round_trips_beside_authored_local_files() -> None:
    authored = ".github/instructions/authored.instructions.md"
    bundled = ".agents/skills/bundled/SKILL.md"
    lockfile = LockFile()
    lockfile.local_deployed_files = [authored]
    lockfile.local_deployed_file_hashes = {authored: "sha256:authored"}

    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile,
        [bundled],
        {bundled: f"sha256:{'b' * 64}"},
        scope=InstallScope.PROJECT,
    )
    rebuilt = LockFile.from_yaml(lockfile.to_yaml())

    assert rebuilt.local_deployed_files == [bundled, authored]
    assert rebuilt.local_deployed_file_hashes == {
        bundled: f"sha256:{'b' * 64}",
        authored: "sha256:authored",
    }
    assert DeploymentLedgerCodec.local_bundle_paths(rebuilt) == frozenset({bundled})
    records = {
        record.locator.value: record for record in rebuilt.deployment_ledger.records.values()
    }
    assert records[bundled].owners == (".", "local-bundle")
    assert records[bundled].active_owner == "local-bundle"
    assert records[authored].owners == (".",)
    assert records[authored].active_owner == "."


def test_local_bundle_provenance_survives_canonical_ledger_rebuilds() -> None:
    bundled = ".agents/skills/bundled/SKILL.md"
    sibling = ".agents/skills/sibling/SKILL.md"
    renamed = ".agents/skills/renamed/SKILL.md"
    lockfile = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile,
        [bundled, sibling],
        {
            bundled: f"sha256:{'a' * 64}",
            sibling: f"sha256:{'c' * 64}",
        },
        scope=InstallScope.PROJECT,
    )

    dependency = LockedDependency(
        repo_url="owner/package",
        deployed_files=[".github/agents/demo.agent.md"],
        deployed_file_hashes={".github/agents/demo.agent.md": f"sha256:{'b' * 64}"},
    )
    lockfile.add_dependency(dependency)
    DeploymentLedgerCodec.replace_mcp_target_servers(lockfile, {"vscode": ["demo"]})
    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile,
        dependency.get_unique_key(),
        dependency.deployed_files,
        dependency.deployed_file_hashes,
    )
    DeploymentLedgerCodec.refresh_from_legacy(lockfile)

    assert DeploymentLedgerCodec.local_bundle_paths(lockfile) == frozenset({bundled, sibling})

    lockfile.rename_local_deployed_path(bundled, renamed)

    assert DeploymentLedgerCodec.local_bundle_paths(lockfile) == frozenset({renamed, sibling})
    rebuilt = LockFile.from_yaml(lockfile.to_yaml())
    assert DeploymentLedgerCodec.local_bundle_paths(rebuilt) == frozenset({renamed, sibling})


def test_service_rows_with_same_name_and_runtime_keep_distinct_targets() -> None:
    """MCP and LSP URI ownership must not collide in canonical state."""
    lockfile = LockFile()
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lockfile,
        {"claude": ["shared-server"]},
    )
    DeploymentLedgerCodec.replace_lsp_target_servers(
        lockfile,
        {"claude": ["shared-server"]},
    )

    rebuilt = LockFile.from_yaml(lockfile.to_yaml())

    assert rebuilt.mcp_target_servers == {"claude": ["shared-server"]}
    assert rebuilt.lsp_target_servers == {"claude": ["shared-server"]}
    service_targets = {
        record.locator.target
        for record in rebuilt.deployment_ledger.records.values()
        if record.locator.value == "shared-server"
    }
    assert service_targets == {"mcp", "lsp"}


def test_local_bundle_provenance_rejects_missing_or_malformed_hashes() -> None:
    bundled = ".agents/skills/bundled/SKILL.md"
    lockfile = LockFile()

    with pytest.raises(ValueError, match="requires a canonical sha256"):
        DeploymentLedgerCodec.record_local_bundle_files(
            lockfile, [bundled], {}, scope=InstallScope.PROJECT
        )

    locator = _locator(bundled, target="agents")
    lockfile.deployment_ledger = DeploymentLedger(
        records={
            locator.key: DeploymentRecord(
                locator=locator,
                owners=(".", "local-bundle"),
                active_owner="local-bundle",
                content_hash=None,
            )
        }
    )
    lockfile._deployments_present = True

    with pytest.raises(ValueError, match="requires a canonical sha256"):
        DeploymentLedgerCodec.local_bundle_paths(lockfile)


def test_from_rows_preserves_hashless_legacy_ownership_row() -> None:
    rows = [
        {
            "kind": "project-relative",
            "target": "copilot",
            "value": ".github/agents/verified.agent.md",
            "runtime": None,
            "scope": "project",
            "owners": ["verified"],
            "active_owner": "verified",
            "content_hash": "sha256:verified",
        },
        {
            "kind": "project-relative",
            "target": "copilot",
            "value": ".github/agents/unverified.agent.md",
            "runtime": None,
            "scope": "project",
            "owners": ["unverified"],
            "active_owner": "unverified",
            "content_hash": None,
        },
    ]

    ledger = DeploymentLedgerCodec.from_rows(rows)

    assert tuple(record.locator.value for record in ledger.records.values()) == (
        ".github/agents/verified.agent.md",
        ".github/agents/unverified.agent.md",
    )


# ---------------------------------------------------------------------------
#  MCP native hash baseline — ledger-side support
# ---------------------------------------------------------------------------

_HASH_A = "sha256:" + "a1" * 32
_HASH_B = "sha256:" + "b2" * 32
_HASH_C = "sha256:" + "c3" * 32


def _seed_lockfile_with_mcp_hashes(
    servers: dict[str, list[str]],
    hashes: dict[str, dict[str, str]],
) -> LockFile:
    """Create a lockfile whose deployment ledger carries MCP URI hashes."""
    lockfile = LockFile()
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lockfile,
        servers,
        native_hashes=hashes,
    )
    return lockfile


class TestMcpNativeHashes:
    """DeploymentLedgerCodec.mcp_native_hashes getter."""

    def test_returns_empty_when_no_mcp_records(self) -> None:
        lockfile = LockFile()
        assert DeploymentLedgerCodec.mcp_native_hashes(lockfile) == {}

    def test_returns_empty_when_hashes_are_none(self) -> None:
        lockfile = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a"]},
        )
        assert DeploymentLedgerCodec.mcp_native_hashes(lockfile) == {}

    def test_projects_valid_hashes(self) -> None:
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a", "server-b"]},
            {"copilot": {"server-a": _HASH_A, "server-b": _HASH_B}},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A, "server-b": _HASH_B}}

    def test_excludes_malformed_hashes(self) -> None:
        """Hashes that are not sha256:<64 lower hex> are silently excluded."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["good", "bad"]},
            {"copilot": {"good": _HASH_A, "bad": "md5:short"}},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"good": _HASH_A}}

    def test_excludes_lsp_records(self) -> None:
        """LSP URI rows must not appear in MCP hash projection."""
        lockfile = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["shared"]},
            native_hashes={"copilot": {"shared": _HASH_A}},
        )
        DeploymentLedgerCodec.replace_lsp_target_servers(
            lockfile,
            {"copilot": ["shared"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"shared": _HASH_A}}

    def test_multiple_runtimes(self) -> None:
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["s1"], "claude": ["s2"]},
            {"copilot": {"s1": _HASH_A}, "claude": {"s2": _HASH_B}},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"s1": _HASH_A}, "claude": {"s2": _HASH_B}}


class TestReplaceMcpTargetServersNativeHashes:
    """replace_mcp_target_servers with the native_hashes kwarg."""

    def test_none_preserves_existing_hashes_for_retained_coords(self) -> None:
        """native_hashes=None (default) carries forward prior hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a", "server-b"]},
            {"copilot": {"server-a": _HASH_A, "server-b": _HASH_B}},
        )
        # Update with same servers, no explicit hashes -> preserve
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a", "server-b"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A, "server-b": _HASH_B}}

    def test_none_drops_hash_for_removed_server(self) -> None:
        """Removing a server from target_servers drops its hash."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["kept", "removed"]},
            {"copilot": {"kept": _HASH_A, "removed": _HASH_B}},
        )
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["kept"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"kept": _HASH_A}}

    def test_none_drops_hash_for_removed_runtime(self) -> None:
        """Removing an entire runtime from target_servers drops its hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["s1"], "claude": ["s2"]},
            {"copilot": {"s1": _HASH_A}, "claude": {"s2": _HASH_B}},
        )
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["s1"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"s1": _HASH_A}}

    def test_explicit_map_replaces_all_hashes(self) -> None:
        """An explicit map is authoritative — no fallback to prior hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a", "server-b"]},
            {"copilot": {"server-a": _HASH_A, "server-b": _HASH_B}},
        )
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a", "server-b"]},
            native_hashes={"copilot": {"server-a": _HASH_C}},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        # server-a gets new hash, server-b gets None (not in explicit map)
        assert hashes == {"copilot": {"server-a": _HASH_C}}

    def test_explicit_empty_map_clears_all_hashes(self) -> None:
        """An explicit empty map clears all MCP hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a"]},
            native_hashes={},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {}

    def test_explicit_map_rejects_malformed_hash(self) -> None:
        """Malformed envelope in explicit map is stored as None, not the bad value."""
        lockfile = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a"]},
            native_hashes={"copilot": {"server-a": "not-a-hash"}},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {}

    def test_backward_compat_no_kwarg(self) -> None:
        """Existing callers without native_hashes kwarg still work."""
        lockfile = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a"]},
        )
        assert lockfile.mcp_target_servers == {"copilot": ["server-a"]}
        assert DeploymentLedgerCodec.mcp_native_hashes(lockfile) == {}


class TestMcpHashCarryForward:
    """MCP URI hashes survive serialization, rebuild, and carry-forward cycles."""

    @pytest.mark.parametrize("bundle", [False, True])
    def test_local_rename_preserves_mcp_receipt(self, bundle: bool) -> None:
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"claude": ["server-a"]}, {"claude": {"server-a": _HASH_A}}
        )
        if bundle:
            DeploymentLedgerCodec.record_local_bundle_files(
                lockfile, ["old.md"], {"old.md": _HASH_B}, scope=InstallScope.PROJECT
            )
        else:
            lockfile.local_deployed_files = ["old.md"]
            DeploymentLedgerCodec.refresh_from_legacy(lockfile)

        lockfile.rename_local_deployed_path("old.md", "new.md")

        assert lockfile.local_deployed_files == ["new.md"]
        rebuilt = LockFile.from_yaml(lockfile.to_yaml())
        assert DeploymentLedgerCodec.mcp_native_hashes(rebuilt) == {"claude": {"server-a": _HASH_A}}
        assert DeploymentLedgerCodec.local_bundle_paths(rebuilt) == (
            frozenset({"new.md"}) if bundle else frozenset()
        )

    def test_yaml_round_trip_preserves_hashes(self) -> None:
        """Hashes survive to_yaml -> from_yaml round-trip."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        yaml_str = lockfile.to_yaml()
        rebuilt = LockFile.from_yaml(yaml_str)
        hashes = DeploymentLedgerCodec.mcp_native_hashes(rebuilt)
        assert hashes == {"copilot": {"server-a": _HASH_A}}

    def test_rebuild_from_legacy_preserves_uri_hashes(self) -> None:
        """refresh_from_legacy (called by replace_mcp_target_servers) preserves URI hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        # Add a file dependency to trigger a more complex rebuild
        lockfile.local_deployed_files = [".claude/rules/local.md"]
        lockfile.local_deployed_file_hashes = {".claude/rules/local.md": "sha256:" + "dd" * 32}
        DeploymentLedgerCodec.refresh_from_legacy(lockfile)

        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A}}

    def test_invalidate_legacy_projection_preserves_mcp_uri_via_rebuild(self) -> None:
        """invalidate_legacy_projection preserves MCP URI content_hash
        by capturing the prior ledger and passing it to _rebuild_from_legacy."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        # After replace_mcp_target_servers, _deployments_present is True.
        # A full YAML round-trip makes the persisted ledger survive from_lockfile.
        yaml_str = lockfile.to_yaml()
        lockfile = LockFile.from_yaml(yaml_str)
        assert lockfile._deployments_present is True

        # invalidate clears the ledger then rebuilds from legacy views,
        # carrying forward URI hashes from the captured prior ledger.
        DeploymentLedgerCodec.invalidate_legacy_projection(lockfile)

        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A}}

    def test_is_semantically_equivalent_detects_hash_change(self) -> None:
        """Adding a hash to an MCP URI record changes semantic equivalence."""
        without_hash = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            without_hash,
            {"copilot": ["server-a"]},
        )
        with_hash = LockFile()
        DeploymentLedgerCodec.replace_mcp_target_servers(
            with_hash,
            {"copilot": ["server-a"]},
            native_hashes={"copilot": {"server-a": _HASH_A}},
        )
        assert not without_hash.is_semantically_equivalent(with_hash)

    def test_semantically_equivalent_when_hashes_match(self) -> None:
        """Two lockfiles with identical MCP hashes are semantically equivalent."""
        lf1 = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        lf2 = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        assert lf1.is_semantically_equivalent(lf2)

    def test_hash_survives_adding_second_server(self) -> None:
        """Adding a new server to a runtime preserves existing server's hash."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        # Add server-b, preserve server-a's hash (native_hashes=None)
        DeploymentLedgerCodec.replace_mcp_target_servers(
            lockfile,
            {"copilot": ["server-a", "server-b"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A}}

    def test_hash_survives_yaml_round_trip_then_replace(self) -> None:
        """Hash persisted in YAML survives re-read and subsequent replace."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        rebuilt = LockFile.from_yaml(lockfile.to_yaml())
        # Re-replace with same servers, no explicit hashes
        DeploymentLedgerCodec.replace_mcp_target_servers(
            rebuilt,
            {"copilot": ["server-a"]},
        )
        hashes = DeploymentLedgerCodec.mcp_native_hashes(rebuilt)
        assert hashes == {"copilot": {"server-a": _HASH_A}}

    def test_file_dependency_mutation_does_not_drop_mcp_hashes(self) -> None:
        """Mutating file-based dependency state does not affect MCP URI hashes."""
        lockfile = _seed_lockfile_with_mcp_hashes(
            {"copilot": ["server-a"]},
            {"copilot": {"server-a": _HASH_A}},
        )
        dep = LockedDependency(
            repo_url="owner/package",
            deployed_files=[".github/agents/demo.agent.md"],
            deployed_file_hashes={".github/agents/demo.agent.md": "sha256:" + "ee" * 32},
        )
        lockfile.add_dependency(dep)
        # add_dependency calls invalidate_legacy_projection, but the hash
        # is carried in the prior ledger through the rebuild path.
        DeploymentLedgerCodec.refresh_from_legacy(lockfile)
        hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
        assert hashes == {"copilot": {"server-a": _HASH_A}}
