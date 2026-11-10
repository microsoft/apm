"""Conservative metadata-only retirement after complete dependency reintegration."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from os import readlink, stat_result
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    CompletedReintegration,
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
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.context import InstallContext
from apm_cli.install.helpers.absence_observation import verified_absent_dependency_values
from apm_cli.install.manifest_reconcile import reconcile_deployed_block, union_preserving
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.install.sources import CachedDependencySource, Materialization
from apm_cli.install.template import run_integration_template
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, PrimitiveMapping
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component

_OWNER = "example/dependency"
_MARKER = ".agents/skills/demo/SKILL.md"
_OBSOLETE = ".agents/skills/demo/extra.md"
_HASH = "sha256:" + "a" * 64


def _record(
    *,
    value: str = _OBSOLETE,
    owner: str = _OWNER,
    target: str = "legacy",
) -> DeploymentRecord:
    return DeploymentRecord(
        locator=DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target=target,
            value=value,
            runtime=None,
            scope="project",
        ),
        owners=(owner,),
        active_owner=owner,
        content_hash=_HASH,
    )


def _completed() -> CompletedReintegration:
    return CompletedReintegration(
        owner=_OWNER,
        targets=frozenset({"codex"}),
        current_values=frozenset({_MARKER}),
        skill_values=frozenset({_MARKER}),
    )


def _marker_result() -> MaterializationResult:
    return MaterializationResult(
        locator=_record(value=_MARKER, target="codex").locator,
        owners=frozenset({_OWNER}),
        status=MaterializationStatus.UNCHANGED,
        content_hash=_HASH,
        validation=NativePayloadValidation(valid=True, contract="file"),
    )


def _block(tmp_path: Path, **overrides: object) -> tuple:
    prior = _record()
    arguments = {
        "project_root": tmp_path,
        "dep_key": _OWNER,
        "owner": _OWNER,
        "current_files": [_MARKER],
        "current_hashes": {_MARKER: _HASH},
        "prior_files": [_OBSOLETE],
        "prior_hashes": {_OBSOLETE: _HASH},
        "prior_ledger": DeploymentLedger({prior.locator.key: prior}),
        "active_targets": [KNOWN_TARGETS["codex"]],
        "declared_targets": [KNOWN_TARGETS["codex"]],
        "diagnostics": DiagnosticCollector(),
        "completed_reintegration": _completed(),
        "include_ledger": True,
    }
    arguments.update(overrides)
    return reconcile_deployed_block(**arguments)


@pytest.mark.parametrize("recorded_hash", [None, _HASH], ids=["no-hash", "hashed"])
def test_complete_missing_claim_retires_without_disk_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_hash: str | None
) -> None:
    """No-hash compatibility does not grant deletion through this new path."""
    from apm_cli.integration import cleanup

    def unexpected_cleanup(*args: object, **kwargs: object) -> None:
        pytest.fail("Metadata-only absence retirement called physical cleanup")

    monkeypatch.setattr(cleanup, "remove_stale_deployed_files", unexpected_cleanup)
    prior = replace(_record(), content_hash=recorded_hash)
    files, hashes, ledger = _block(
        tmp_path,
        prior_ledger=DeploymentLedger({prior.locator.key: prior}),
        prior_hashes={_OBSOLETE: recorded_hash} if recorded_hash else {},
    )

    assert files == [_MARKER]
    assert hashes == {_MARKER: _HASH}
    assert tuple(ledger.records.values()) == (_record(value=_MARKER, target="codex"),)
    assert not (tmp_path / _OBSOLETE).exists()


@pytest.mark.parametrize("mismatch", ["active-targets", "declared-targets"])
def test_absent_claim_intent_must_match_supplied_target_profiles(mismatch: str) -> None:
    intent = DeploymentIntent(
        active_targets=frozenset({"copilot" if mismatch == "active-targets" else "codex"}),
        declared_targets=frozenset({"copilot" if mismatch == "declared-targets" else "codex"}),
        desired_owners=None,
        authoritative_targets=True,
        completed_reintegration=_completed(),
        verified_absent_values=frozenset({_OBSOLETE}),
    )

    with pytest.raises(ValueError, match="intent must match the supplied target profiles"):
        union_preserving(
            [_MARKER],
            {_MARKER: _HASH},
            [_OBSOLETE],
            {_OBSOLETE: _HASH},
            [KNOWN_TARGETS["codex"]],
            declared_targets=[KNOWN_TARGETS["codex"]],
            owner=_OWNER,
            intent=intent,
        )


@pytest.mark.parametrize("authoritative", [False, True], ids=["failed-owner", "successful-owner"])
def test_absent_claim_typed_intent_preserves_unprocessed_shared_target(
    authoritative: bool,
) -> None:
    prior = _record(target="copilot")
    files, hashes, ledger = union_preserving(
        [],
        {},
        [_OBSOLETE],
        {_OBSOLETE: _HASH},
        [KNOWN_TARGETS["codex"]],
        declared_targets=[KNOWN_TARGETS["codex"], KNOWN_TARGETS["copilot"]],
        prior_ledger=DeploymentLedger({prior.locator.key: prior}),
        owner=_OWNER,
        include_ledger=True,
        intent=DeploymentIntent(
            active_targets=frozenset({"codex"}),
            declared_targets=frozenset({"codex", "copilot"}),
            desired_owners=None,
            authoritative_targets=authoritative,
        ),
    )

    assert files == [_OBSOLETE]
    assert hashes == {_OBSOLETE: _HASH}
    assert dict(ledger.records) == {prior.locator.key: prior}


@pytest.mark.parametrize(
    "protection",
    [
        "no-completion",
        "wrong-owner",
        "project-self",
        "not-authoritative",
        "empty-targets",
        "unprocessed-target",
        "unprocessed-value",
        "still-desired",
        "not-observed-absent",
        "failed",
        "skipped",
        "foreign-owner",
        "foreign-target",
        "unknown-target",
        "multiple-owners",
        "unsupported-locator",
        "dry-run",
    ],
)
def test_absence_alone_cannot_retire_a_claim(tmp_path: Path, protection: str) -> None:
    prior = _record()
    completed = _completed()
    intent = DeploymentIntent(
        active_targets=frozenset({"codex"}),
        declared_targets=frozenset({"codex", "copilot"}),
        desired_owners=None,
        authoritative_targets=True,
        completed_reintegration=completed,
        verified_absent_values=frozenset({_OBSOLETE}),
    )
    records = {prior.locator.key: prior}
    results = [_marker_result()]
    if protection == "no-completion":
        intent = replace(intent, completed_reintegration=None)
    elif protection in {"wrong-owner", "project-self"}:
        intent = replace(
            intent,
            completed_reintegration=replace(
                completed, owner="other/dependency" if protection == "wrong-owner" else "."
            ),
        )
    elif protection == "not-authoritative":
        intent = replace(intent, authoritative_targets=False)
    elif protection in {"empty-targets", "unprocessed-target"}:
        intent = replace(
            intent,
            completed_reintegration=replace(
                completed,
                targets=frozenset() if protection == "empty-targets" else frozenset({"copilot"}),
            ),
        )
    elif protection == "unprocessed-value":
        intent = replace(intent, unprocessed_claims=frozenset({_OBSOLETE}))
    elif protection == "still-desired":
        intent = replace(
            intent,
            completed_reintegration=replace(completed, current_values=frozenset({_OBSOLETE})),
        )
    elif protection == "not-observed-absent":
        intent = replace(intent, verified_absent_values=frozenset())
    elif protection in {"failed", "skipped"}:
        results.append(
            MaterializationResult(
                locator=prior.locator,
                owners=frozenset({_OWNER}),
                status=MaterializationStatus(protection),
                content_hash=None,
                validation=NativePayloadValidation(valid=True, contract="file"),
            )
        )
    elif protection in {"foreign-owner", "foreign-target", "unknown-target"}:
        other = _record(
            owner="other/dependency" if protection == "foreign-owner" else _OWNER,
            target="retired-unknown-target" if protection == "unknown-target" else "copilot",
        )
        records[other.locator.key] = other
    elif protection == "multiple-owners":
        records[prior.locator.key] = replace(prior, owners=(_OWNER, "other/dependency"))
    elif protection == "unsupported-locator":
        locator = replace(prior.locator, kind=LocatorKind.URI)
        records[prior.locator.key] = replace(prior, locator=locator)
    elif protection == "dry-run":
        intent = replace(intent, dry_run=True)
    previous = DeploymentLedger(records)

    result = DeploymentReconciler(
        tmp_path, KNOWN_TARGETS, diagnostics=DiagnosticCollector()
    ).reconcile(previous, results, intent)

    expected = dict(records)
    if protection != "dry-run":
        marker = _record(value=_MARKER, target="codex")
        expected[marker.locator.key] = marker
    assert dict(result.ledger.records) == expected
    assert result.removed == ()


@pytest.mark.parametrize(
    "missing_evidence",
    [
        "skill-members",
        "current-values",
        "successful-marker",
        "canonical-marker",
        "marker-owner",
        "marker-target",
        "overlapping-primitive",
        "different-skill-root",
    ],
)
def test_absent_claim_requires_proven_nonoverlapping_skill_domain(
    tmp_path: Path, missing_evidence: str
) -> None:
    completed = _completed()
    results = [_marker_result()]
    profiles = dict(KNOWN_TARGETS)
    prior = _record()
    if missing_evidence == "skill-members":
        completed = replace(completed, skill_values=frozenset())
    elif missing_evidence == "current-values":
        completed = replace(completed, current_values=frozenset())
    elif missing_evidence == "successful-marker":
        results = []
    elif missing_evidence == "canonical-marker":
        completed = replace(
            completed,
            skill_values=frozenset({_OBSOLETE}),
            current_values=frozenset({_OBSOLETE}),
        )
        prior = _record(value=".agents/skills/demo/removed.md")
        results = [replace(results[0], locator=_record(target="codex").locator)]
    elif missing_evidence == "marker-owner":
        results = [replace(results[0], owners=frozenset({"example/other"}))]
    elif missing_evidence == "marker-target":
        results = [replace(results[0], locator=replace(results[0].locator, target="copilot"))]
    elif missing_evidence == "overlapping-primitive":
        codex = profiles["codex"]
        profiles["codex"] = replace(
            codex,
            primitives={
                **codex.primitives,
                "agents": PrimitiveMapping("skills", ".md", "claude_agent", deploy_root=".agents"),
            },
        )
    elif missing_evidence == "different-skill-root":
        prior = _record(value=".agents/skills/unproven/extra.md")
    diagnostics = DiagnosticCollector()

    result = DeploymentReconciler(tmp_path, profiles, diagnostics=diagnostics).reconcile(
        DeploymentLedger({prior.locator.key: prior}),
        results,
        DeploymentIntent(
            active_targets=frozenset({"codex"}),
            declared_targets=frozenset({"codex", "copilot"}),
            desired_owners=None,
            authoritative_targets=True,
            completed_reintegration=completed,
            verified_absent_values=frozenset({prior.locator.value}),
        ),
    )

    assert result.ledger.records[prior.locator.key] == prior
    assert result.removed == ()
    warnings = diagnostics.by_category()["warning"]
    assert len(warnings) == 1
    assert "APM cannot confirm these paths belong to a current skill" in warnings[0].message
    assert prior.locator.value in warnings[0].detail


@pytest.mark.parametrize(
    "value",
    [
        ".codex/agents/reviewer.toml",
        ".github/instructions/testing.instructions.md",
        ".github/prompts/review.prompt.md",
        ".codex/hooks.json",
        ".github/extensions/board/index.js",
        ".github/plugins/native/plugin.json",
    ],
    ids=["agent", "instruction", "prompt", "hook", "canvas", "plugin"],
)
def test_absent_other_primitive_claim_is_not_authorized_by_skill_completion(
    tmp_path: Path, value: str
) -> None:
    prior = _record(value=value)
    diagnostics = DiagnosticCollector()
    files, hashes, ledger = _block(
        tmp_path,
        active_targets=[KNOWN_TARGETS["codex"], KNOWN_TARGETS["copilot"]],
        declared_targets=[KNOWN_TARGETS["codex"], KNOWN_TARGETS["copilot"]],
        completed_reintegration=replace(_completed(), targets=frozenset({"codex", "copilot"})),
        prior_files=[value],
        prior_hashes={value: _HASH},
        prior_ledger=DeploymentLedger({prior.locator.key: prior}),
        diagnostics=diagnostics,
    )

    assert files == [_MARKER, value]
    assert hashes == {_MARKER: _HASH, value: _HASH}
    assert ledger.records[prior.locator.key] == prior
    assert not (tmp_path / value).exists()
    warnings = diagnostics.by_category()["warning"]
    assert len(warnings) == 1
    assert "APM cannot confirm these paths belong to a current skill" in warnings[0].message
    assert value in warnings[0].detail


@pytest.mark.parametrize(
    "content",
    [
        "edited",
        "untracked",
        "directory",
        pytest.param("symlink", marks=pytest.mark.windows_compat),
        pytest.param("relative-symlink", marks=pytest.mark.windows_compat),
    ],
)
def test_present_ambiguous_content_retains_every_view(tmp_path: Path, content: str) -> None:
    path = tmp_path / _OBSOLETE
    path.parent.mkdir(parents=True)
    if content == "directory":
        path.mkdir()
    elif content in {"symlink", "relative-symlink"}:
        destination = path.parent / "missing-destination"
        path.symlink_to(destination.name if content == "relative-symlink" else destination)
    else:
        path.write_text(content, encoding="ascii")
    link_target = readlink(path) if path.is_symlink() else None

    files, hashes, ledger = _block(tmp_path)

    assert files == [_MARKER, _OBSOLETE]
    assert hashes == {_MARKER: _HASH, _OBSOLETE: _HASH}
    assert ledger.records[_record().locator.key] == _record()
    assert len(ledger.records) == 2
    if content == "directory":
        assert path.is_dir()
    elif content in {"symlink", "relative-symlink"}:
        assert path.is_symlink()
        assert readlink(path) == link_target
    else:
        assert path.read_text(encoding="ascii") == content


def test_absence_probe_error_retains_claim_with_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.lstat
    diagnostics = DiagnosticCollector()
    path = tmp_path / _OBSOLETE

    def refuse(candidate: Path, *args: object, **kwargs: object) -> stat_result:
        if candidate == path:
            raise PermissionError("fixture permission refusal")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", refuse)
    files, hashes, ledger = _block(tmp_path, diagnostics=diagnostics)

    assert files == [_MARKER, _OBSOLETE]
    assert hashes == {_MARKER: _HASH, _OBSOLETE: _HASH}
    assert ledger.records[_record().locator.key] == _record()
    assert len(ledger.records) == 2
    assert diagnostics.count_for_package(_OWNER, "warning") == 1


@pytest.mark.parametrize("guard", ["uri", "absolute", "rejected"])
def test_absence_observation_rejects_non_deploy_paths_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    value = {
        "uri": "cowork://skills/demo/extra.md",
        "absolute": (tmp_path / "outside-deploy" / "extra.md").as_posix(),
        "rejected": _OBSOLETE,
    }[guard]
    # Isolate URI/absolute exclusions from the canonical validator's own policy.
    validate = Mock(return_value=guard != "rejected")
    probe = Mock(side_effect=FileNotFoundError)
    monkeypatch.setattr(BaseIntegrator, "validate_deploy_path", validate)
    monkeypatch.setattr(Path, "lstat", probe)

    observed = verified_absent_dependency_values(
        project_root=tmp_path,
        completed=_completed(),
        prior_files=[value],
        current_files=[_MARKER],
        targets=[KNOWN_TARGETS["codex"]],
        cleanup_retained_hashes=None,
        diagnostics=DiagnosticCollector(),
        user_scope=False,
    )

    assert observed == frozenset()
    probe.assert_not_called()
    if guard == "rejected":
        validate.assert_called_once_with(
            value, tmp_path, targets=[KNOWN_TARGETS["codex"]], user_scope=False
        )
    else:
        validate.assert_not_called()


def test_refused_absence_observation_preserves_all_lock_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BaseIntegrator, "validate_deploy_path", Mock(return_value=False))

    files, hashes, ledger = _block(tmp_path)

    assert files == [_MARKER, _OBSOLETE]
    assert hashes == {_MARKER: _HASH, _OBSOLETE: _HASH}
    assert ledger.records[_record().locator.key] == _record()
    assert len(ledger.records) == 2


@pytest.mark.parametrize("receipt", ["matching", "wrong-owner", "missing", "none", "wrong-type"])
def test_template_accepts_only_typed_owner_matched_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt: str
) -> None:
    """Fixture receipts prove consumer intake, not an available production producer."""
    from apm_cli.install import template

    completed = _completed()
    other = replace(completed, owner="example/other")
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path / ".apm",
        targets=[KNOWN_TARGETS["codex"]],
        diagnostics=DiagnosticCollector(),
        integrators={
            name: Mock() for name in ("prompt", "agent", "skill", "instruction", "command", "hook")
        },
        package_completed_reintegrations={other.owner: other},
    )
    package = PackageInfo(
        package=APMPackage(name="dependency", version="1.0.0"),
        install_path=tmp_path / "dependency",
    )
    source = CachedDependencySource(
        ctx,
        DependencyReference(repo_url=_OWNER),
        package.install_path,
        _OWNER,
        resolved_ref=None,
        dep_locked_chk=None,
    )
    integration_result: dict[str, object] = dict.fromkeys(
        (
            "prompts",
            "agents",
            "skills",
            "sub_skills",
            "instructions",
            "commands",
            "hooks",
            "canvases",
            "links_resolved",
        ),
        0,
    )
    integration_result.update(skills=1, deployed_files=[_MARKER])
    if receipt != "missing":
        integration_result["completed_reintegration"] = {
            "matching": completed,
            "wrong-owner": other,
            "none": None,
            "wrong-type": SimpleNamespace(owner=_OWNER),
        }[receipt]
    integrate = Mock(return_value=integration_result)
    monkeypatch.setattr(template, "integrate_package_primitives", integrate)
    monkeypatch.setattr(template, "_effective_allow", lambda _ctx: None)

    deltas = run_integration_template(
        source,
        materialization=Materialization(package, package.install_path, _OWNER),
    )

    integrate.assert_called_once()
    assert deltas["skills"] == 1
    assert deltas["installed"] == 1
    assert ctx.diagnostics.error_count == 0
    assert ctx.package_deployed_files == {_OWNER: [_MARKER]}
    assert ctx.package_completed_reintegrations == (
        {other.owner: other, _OWNER: completed} if receipt == "matching" else {other.owner: other}
    )


@pytest.mark.parametrize(
    "guard", ["lockfile-only", "frozen", "unprocessed", "complete-empty", "complete-with-marker"]
)
def test_skill_result_reconciles_only_proven_claims_and_preserves_other_owners(
    tmp_path: Path, guard: str
) -> None:
    other = _record(value=".claude/skills/other/SKILL.md", owner="example/other", target="claude")
    prior = _record()
    existing = LockFile()
    current = LockFile()
    for owner in (_OWNER, "example/other"):
        existing.add_dependency(LockedDependency(repo_url=owner))
        current.add_dependency(LockedDependency(repo_url=owner))
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger({record.locator.key: record for record in (prior, other)}), existing
    )
    marker_path = tmp_path / _MARKER
    marker_bytes = b"---\nname: demo\ndescription: Proven skill\n---\nCurrent instructions.\n"
    marker_hash = "sha256:" + sha256(marker_bytes).hexdigest()
    with_marker = guard == "complete-with-marker"
    if with_marker:
        marker_path.parent.mkdir(parents=True)
        marker_path.write_bytes(marker_bytes)
    completed = (
        _completed()
        if with_marker
        else replace(_completed(), current_values=frozenset(), skill_values=frozenset())
    )
    ctx = SimpleNamespace(
        existing_lockfile=existing,
        package_deployed_files={_OWNER: [_MARKER] if with_marker else []},
        package_completed_reintegrations=({} if guard == "unprocessed" else {_OWNER: completed}),
        targets=[KNOWN_TARGETS["codex"]],
        project_root=tmp_path,
        diagnostics=DiagnosticCollector(),
        lockfile_only=guard == "lockfile-only",
        frozen=guard == "frozen",
    )

    LockfileBuilder(ctx)._attach_deployed_files(current)
    DeploymentLedgerCodec.merge_dependencies(
        existing, current, project_root=tmp_path, diagnostics=ctx.diagnostics
    )

    for lockfile in (current, existing):
        dependency = lockfile.get_dependency(_OWNER)
        assert dependency.deployed_files == ([_MARKER] if with_marker else [_OBSOLETE])
        assert dependency.deployed_file_hashes == (
            {_MARKER: marker_hash} if with_marker else {_OBSOLETE: _HASH}
        )
        assert lockfile.get_dependency("example/other").deployed_files == [other.locator.value]
        assert lockfile.get_dependency("example/other").deployed_file_hashes == {
            other.locator.value: _HASH
        }
        records = DeploymentLedgerCodec.from_lockfile(lockfile).records
        assert records[other.locator.key] == other
        assert (prior.locator.key in records) is not with_marker
        if not with_marker:
            assert records[prior.locator.key] == prior
    assert not (tmp_path / _OBSOLETE).exists()
    if with_marker:
        assert marker_path.read_bytes() == marker_bytes


def test_skill_receipt_scope_does_not_restrict_install_target_contraction(
    tmp_path: Path,
) -> None:
    """Concrete target cleanup stays broader than absent historical-skill proof."""
    marker_bytes = b"---\nname: demo\ndescription: Scope fixture\n---\nSkill body.\n"
    digest = "sha256:" + sha256(marker_bytes).hexdigest()
    claude_marker = ".claude/skills/demo/SKILL.md"
    for value in (_MARKER, claude_marker):
        path = tmp_path / value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(marker_bytes)
    codex = replace(_record(value=_MARKER, target="codex"), content_hash=digest)
    historical = _record()
    existing, current = LockFile(), LockFile()
    for lockfile in (existing, current):
        lockfile.add_dependency(LockedDependency(repo_url=_OWNER))
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger({row.locator.key: row for row in (codex, historical)}), existing
    )
    ctx = SimpleNamespace(
        existing_lockfile=existing,
        package_deployed_files={_OWNER: [claude_marker]},
        package_completed_reintegrations={
            _OWNER: CompletedReintegration(
                owner=_OWNER,
                targets=frozenset({"claude"}),
                current_values=frozenset({claude_marker}),
                skill_values=frozenset({claude_marker}),
            )
        },
        targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]],
        project_root=tmp_path,
        diagnostics=DiagnosticCollector(),
    )

    LockfileBuilder(ctx)._attach_deployed_files(current)

    dependency = current.get_dependency(_OWNER)
    assert set(dependency.deployed_files) == {claude_marker, _OBSOLETE}
    assert dependency.deployed_file_hashes == {claude_marker: digest, _OBSOLETE: _HASH}
    records = DeploymentLedgerCodec.from_lockfile(current).records
    assert codex.locator.key not in records
    assert records[historical.locator.key] == historical
    assert len(records) == 2
    assert not (tmp_path / _MARKER).exists()
    assert (tmp_path / claude_marker).read_bytes() == marker_bytes


@pytest.mark.parametrize("empty_table", [False, True], ids=["dormant", "empty"])
def test_empty_builder_persists_replacement_intent_without_absence_authority(
    tmp_path: Path, empty_table: bool
) -> None:
    """Changed rules must persist without treating unprocessed claims as obsolete."""
    from apm_cli.models.dependency.replacement import parse_replace_block

    rule = {
        "for": {"git": "https://github.com/unused-replacement-fixture/base.git"},
        "with": {"path": "../intentionally-absent"},
    }
    declared = [] if empty_table else [rule]
    existing = LockFile(replacements=[rule] if empty_table else [])
    existing.add_dependency(LockedDependency(repo_url=_OWNER))
    prior = _record()
    DeploymentLedgerCodec.apply_to_lockfile(DeploymentLedger({prior.locator.key: prior}), existing)
    lock_path = tmp_path / "apm.lock.yaml"
    existing.write(lock_path)
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        existing_lockfile=existing,
        intended_dep_keys={_OWNER},
        apm_package=APMPackage(
            name="consumer", version="1.0.0", replace_rules=parse_replace_block(declared)
        ),
        targets=[KNOWN_TARGETS["codex"]],
        diagnostics=DiagnosticCollector(),
    )

    LockfileBuilder(ctx).build_and_save()

    written = LockFile.read(lock_path)
    assert written is not None
    assert written.replacements == declared
    dependency = written.get_dependency(_OWNER)
    assert dependency.deployed_files == [_OBSOLETE]
    assert dependency.deployed_file_hashes == {_OBSOLETE: _HASH}
    assert DeploymentLedgerCodec.from_lockfile(written).records == {prior.locator.key: prior}
    assert not (tmp_path / _OBSOLETE).exists()
    assert ctx.package_completed_reintegrations == {}
