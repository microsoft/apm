"""Tests for canonical deployment state and lockfile encoding."""

from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentIntent,
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconciler,
    DeploymentRecord,
    LocatorKind,
    MaterializationResult,
    MaterializationStatus,
    NativePayloadValidation,
)
from apm_cli.core.scope import InstallScope
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.targets import TargetProfile
from apm_cli.utils.diagnostics import DiagnosticCollector

VALID = NativePayloadValidation(valid=True, contract="file")


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


def _materialization(
    owner: str,
    locator: DeploymentLocator,
    *,
    status: MaterializationStatus = MaterializationStatus.WRITTEN,
    valid: bool = True,
    content_hash: str | None = "sha256:new",
) -> MaterializationResult:
    return MaterializationResult(
        locator=locator,
        owners=frozenset({owner}),
        status=status,
        content_hash=content_hash,
        validation=NativePayloadValidation(valid=valid, contract="native-v1"),
    )


def _record(locator: DeploymentLocator, owners=("old",), active="old") -> DeploymentRecord:
    return DeploymentRecord(
        locator=locator,
        owners=owners,
        active_owner=active,
        content_hash="sha256:old",
    )


def _reconciler(tmp_path: Path) -> DeploymentReconciler:
    profiles = {
        "copilot": _target("copilot", ".github"),
        "claude": _target("claude", ".claude"),
        "cursor": _target("cursor", ".cursor"),
    }
    return DeploymentReconciler(
        tmp_path,
        profiles,
        diagnostics=DiagnosticCollector(),
    )


def _intent(
    *,
    active=frozenset({"copilot"}),
    declared=None,
    owners=None,
    authoritative=True,
) -> DeploymentIntent:
    return DeploymentIntent(
        active_targets=active,
        declared_targets=declared,
        desired_owners=owners,
        authoritative_targets=authoritative,
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


def test_last_successful_writer_is_active_and_order_is_deterministic(tmp_path: Path) -> None:
    locator = _locator()
    results = [
        _materialization("alpha", locator, content_hash="sha256:a"),
        _materialization("beta", locator, content_hash="sha256:b"),
        _materialization("alpha", locator, content_hash="sha256:c"),
    ]

    reconciled = _reconciler(tmp_path).reconcile(
        DeploymentLedger(records={}),
        results,
        _intent(owners=frozenset({"alpha", "beta"})),
    )

    record = reconciled.ledger.records[locator.key]
    assert record.owners == ("beta", "alpha")
    assert record.active_owner == "alpha"
    assert record.content_hash == "sha256:c"


def test_uninstall_handoff_requires_successful_survivor(tmp_path: Path) -> None:
    locator = _locator()
    prior = DeploymentLedger(
        records={locator.key: _record(locator, owners=("survivor", "removed"), active="removed")}
    )

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [_materialization("survivor", locator)],
        _intent(owners=frozenset({"survivor"})),
    )

    record = reconciled.ledger.records[locator.key]
    assert record.active_owner == "survivor"
    assert reconciled.owner_handoffs == ((locator.key, "removed", "survivor"),)


def test_uninstall_without_new_proof_transfers_active_owner_to_survivor(
    tmp_path: Path,
) -> None:
    locator = _locator()
    prior = DeploymentLedger(
        records={
            locator.key: _record(
                locator,
                owners=("zeta", "alpha", "removed"),
                active="removed",
            )
        }
    )

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [],
        _intent(
            owners=frozenset({"alpha", "zeta"}),
            authoritative=False,
        ),
    )

    record = reconciled.ledger.records[locator.key]
    assert record.owners == ("zeta", "alpha")
    assert record.active_owner == "zeta"
    assert record.active_owner in record.owners


def test_deployment_record_rejects_active_owner_outside_owners() -> None:
    with pytest.raises(ValueError, match="active_owner must be present in owners"):
        _record(_locator(), owners=("survivor",), active="removed")


def test_no_handoff_without_successful_survivor(tmp_path: Path) -> None:
    locator = _locator()
    prior = DeploymentLedger(
        records={locator.key: _record(locator, owners=("survivor", "removed"), active="removed")}
    )

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [_materialization("survivor", locator, status=MaterializationStatus.FAILED)],
        _intent(owners=frozenset({"survivor"})),
    )

    assert reconciled.ledger.records[locator.key] == prior.records[locator.key]
    assert reconciled.owner_handoffs == ()


def test_same_target_stale_and_undeclared_target_are_removed(tmp_path: Path) -> None:
    active = _locator("active.md")
    ghost = _locator(".claude/rules/ghost.md", target="claude")
    prior = DeploymentLedger(
        records={
            active.key: _record(active),
            ghost.key: _record(ghost),
        }
    )

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [],
        _intent(declared=frozenset({"copilot"})),
    )

    assert reconciled.ledger.records == {}
    assert set(reconciled.removed) == {active, ghost}


@pytest.mark.parametrize(
    ("dropped_target", "surviving_target"),
    (
        ("claude", "cursor"),
        ("copilot", "cursor"),
    ),
)
def test_shared_agents_rows_contract_by_value_with_current_skill_claims(
    tmp_path: Path,
    dropped_target: str,
    surviving_target: str,
) -> None:
    """A generic shared skill row must yield to a current concrete target row."""
    del dropped_target  # Target identity must not change shared-root semantics.
    shared = ".agents/skills/shared/SKILL.md"
    beta_only = ".agents/skills/beta-only/SKILL.md"
    alpha_only = ".agents/skills/alpha-only/SKILL.md"
    generic_shared = _locator(shared, target="agents")
    generic_beta = _locator(beta_only, target="agents")
    generic_alpha = _locator(alpha_only, target="agents")
    previous = DeploymentLedger(
        records={
            generic_shared.key: _record(
                generic_shared,
                owners=("alpha", "beta"),
                active="beta",
            ),
            generic_beta.key: _record(generic_beta, owners=("beta",), active="beta"),
            generic_alpha.key: _record(generic_alpha, owners=("alpha",), active="alpha"),
        }
    )
    current_shared = _locator(shared, target=surviving_target)
    current_beta = _locator(beta_only, target=surviving_target)
    intent = _intent(
        active=frozenset({surviving_target}),
        declared=frozenset({surviving_target}),
        owners=frozenset({"beta"}),
    )
    intent = replace(
        intent,
        generic_governed_values=frozenset({shared, beta_only, alpha_only}),
    )
    results = [
        _materialization("beta", current_shared, content_hash="sha256:shared-current"),
        _materialization("beta", current_beta, content_hash="sha256:beta-current"),
    ]

    reconciled = _reconciler(tmp_path).reconcile(previous, results, intent)

    assert set(reconciled.ledger.records) == {current_shared.key, current_beta.key}
    assert reconciled.ledger.records[current_shared.key].owners == ("beta",)
    assert reconciled.ledger.records[current_shared.key].content_hash == "sha256:shared-current"
    assert reconciled.ledger.records[current_beta.key].owners == ("beta",)
    assert reconciled.ledger.records[current_beta.key].content_hash == "sha256:beta-current"
    assert reconciled.removed == (generic_alpha,)

    repeated = _reconciler(tmp_path).reconcile(reconciled.ledger, results, intent)
    assert repeated.ledger == reconciled.ledger
    assert repeated.changed is False
    assert repeated.removed == ()


def test_generic_agents_contraction_dry_run_and_failed_cleanup_preserve_row(tmp_path: Path) -> None:
    """No generic row is removed without a successful, non-dry-run cleanup proof."""
    alpha = _locator(".agents/skills/alpha-only/SKILL.md", target="agents")
    previous = DeploymentLedger(
        records={alpha.key: _record(alpha, owners=("alpha",), active="alpha")}
    )
    intent = replace(
        _intent(
            active=frozenset({"cursor"}),
            declared=frozenset({"cursor"}),
            owners=frozenset({"alpha"}),
        ),
        generic_governed_values=frozenset({alpha.value}),
    )
    reconciler = _reconciler(tmp_path)

    dry_run = reconciler.reconcile(previous, [], replace(intent, dry_run=True))
    failed = reconciler.reconcile(
        previous,
        [_materialization("alpha", alpha, status=MaterializationStatus.FAILED)],
        intent,
    )

    assert dry_run.ledger is previous
    assert dry_run.changed is False
    assert failed.ledger == previous
    assert failed.failed == (alpha,)


def test_unknown_declared_universe_preserves_sibling_target(tmp_path: Path) -> None:
    sibling = _locator(".claude/rules/kept.md", target="claude")
    prior = DeploymentLedger(records={sibling.key: _record(sibling)})

    reconciled = _reconciler(tmp_path).reconcile(prior, [], _intent())

    assert reconciled.ledger.records[sibling.key] == prior.records[sibling.key]


def test_failed_cleanup_retains_prior_row(tmp_path: Path) -> None:
    locator = _locator()
    prior = DeploymentLedger(records={locator.key: _record(locator)})

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [_materialization("old", locator, status=MaterializationStatus.FAILED)],
        _intent(),
    )

    assert reconciled.ledger == prior
    assert reconciled.failed == (locator,)


def test_invalid_native_payload_aborts_the_ledger_transition(tmp_path: Path) -> None:
    prior_locator = _locator("prior.md")
    new_locator = _locator("new.md")
    prior = DeploymentLedger(records={prior_locator.key: _record(prior_locator)})

    reconciled = _reconciler(tmp_path).reconcile(
        prior,
        [
            _materialization("new", new_locator),
            _materialization("bad", _locator("bad.md"), valid=False),
        ],
        _intent(),
    )

    assert reconciled.ledger is prior
    assert reconciled.changed is False


def test_locator_round_trips_project_external_uri_and_mcp(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    copilot = _target("copilot", ".github")
    cowork = _target("copilot-cowork", ".copilot", external)

    project_locator = DeploymentLedgerCodec.locator_for_path(
        project / ".github" / "agents" / "demo.agent.md",
        project_root=project,
        target=copilot,
        scope=InstallScope.PROJECT,
    )
    external_locator = DeploymentLedgerCodec.locator_for_path(
        external / "skills" / "demo" / "SKILL.md",
        project_root=project,
        target=cowork,
        scope=InstallScope.USER,
    )
    uri_locator = DeploymentLedgerCodec.locator_for_path(
        "copilot-app-db://workflows/demo",
        project_root=project,
        target=copilot,
        scope=InstallScope.PROJECT,
    )
    mcp_locator = DeploymentLedgerCodec.locator_for_path(
        "mcp://demo",
        project_root=project,
        target=_target("mcp", ".mcp"),
        runtime="vscode",
        scope=InstallScope.PROJECT,
    )

    assert project_locator.kind == LocatorKind.PROJECT_RELATIVE
    assert (
        DeploymentLedgerCodec.resolve_locator(project_locator, project_root=project, target=copilot)
        == (project / project_locator.value).resolve()
    )
    assert external_locator.kind == LocatorKind.TARGET_RELATIVE
    assert external_locator.value == "skills/demo/SKILL.md"
    assert (
        DeploymentLedgerCodec.resolve_locator(external_locator, project_root=project, target=cowork)
        == (external / external_locator.value).resolve()
    )
    assert (
        DeploymentLedgerCodec.resolve_locator(uri_locator, project_root=project, target=copilot)
        == "copilot-app-db://workflows/demo"
    )
    assert mcp_locator.runtime == "vscode"


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


def test_local_bundle_provenance_rejects_missing_or_malformed_hashes() -> None:
    bundled = ".agents/skills/bundled/SKILL.md"
    lockfile = LockFile()

    with pytest.raises(ValueError, match="requires a canonical sha256"):
        DeploymentLedgerCodec.record_local_bundle_files(lockfile, [bundled], {})

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
