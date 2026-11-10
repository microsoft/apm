"""Local receipts prove rendered writes, never ownership or source selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec, DeploymentMaterializationSession
from apm_cli.core.deployment_state import DeploymentLedger, DeploymentRecord
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component

TARGET = KNOWN_TARGETS["claude"]


def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, target: TargetProfile = TARGET
) -> tuple[Path, Path, LockFile]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "consumer"
    path = target.skills_deploy_path(root) / "demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("rendered old\n", encoding="utf-8")
    locator = DeploymentLedgerCodec.locator_for_path(
        path, project_root=root, target=target, scope=InstallScope.PROJECT
    )
    record = DeploymentRecord(locator, ("pkg",), "pkg", compute_file_hash(path))
    lock = LockFile()
    lock.dependencies["pkg"] = LockedDependency(repo_url="pkg")
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger({locator.key: record}), lock, write_legacy_views=False
    )
    return root, path, lock


def _session(root: Path, lock: LockFile) -> DeploymentMaterializationSession:
    return DeploymentMaterializationSession(root, InstallScope.PROJECT, lock)


def test_receipt_outlives_checked_out_hash_without_adopting_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    old_hash = compute_file_hash(path)
    session = _session(root, lock)
    assert session.baseline(path, [TARGET]) is None
    session.record({path: old_hash}, [TARGET], "pkg")
    session.flush()
    row = next(iter(lock.deployment_ledger.records.values()))
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            {
                row.locator.key: DeploymentRecord(
                    row.locator, row.owners, "pkg", "sha256:" + "b" * 64
                )
            }
        ),
        lock,
        write_legacy_views=False,
    )
    assert _session(root, lock).baseline(path, [TARGET]) == old_hash
    path.write_text("user edit\n", encoding="utf-8")
    assert _session(root, lock).baseline(path, [TARGET]) == old_hash
    assert compute_file_hash(path) != old_hash


@pytest.mark.parametrize("target_name", ["claude", "hermes", "copilot-cowork"])
@pytest.mark.parametrize("prior_claims", [False, True])
def test_external_user_receipt_uses_the_deployed_compatibility_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    prior_claims: bool,
) -> None:
    from dataclasses import replace
    from urllib.parse import urlparse

    from apm_cli.install.deployed_paths import deployed_path_entry

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    external_root = tmp_path / "external-target"
    if target_name == "copilot-cowork":
        target = replace(KNOWN_TARGETS[target_name], resolved_deploy_root=external_root)
    else:
        scoped_target = KNOWN_TARGETS[target_name].for_scope(user_scope=True)
        assert scoped_target is not None
        target = replace(scoped_target, root_dir=external_root.as_posix())
    path = target.skills_deploy_path(home) / "demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("rendered old\n", encoding="utf-8")
    content_hash = compute_file_hash(path)
    value = deployed_path_entry(path, home, [target])
    if target_name == "copilot-cowork":
        parsed = urlparse(value)
        assert (parsed.scheme, parsed.netloc, parsed.path) == (
            "cowork",
            "skills",
            "/demo/SKILL.md",
        )
    else:
        assert value == path.as_posix()
    lock = LockFile()
    lock.dependencies["pkg"] = LockedDependency(
        repo_url="pkg", deployed_files=[value], deployed_file_hashes={value: content_hash}
    )
    session = DeploymentMaterializationSession(
        home, InstallScope.USER, lock if prior_claims else None
    )
    session.record({path: content_hash}, [target], "pkg")
    session.flush()
    if prior_claims:
        assert (
            DeploymentMaterializationSession(home, InstallScope.USER, lock).baseline(path, [target])
            == content_hash
        )
    session.bind_committed_claims(lock)
    assert (
        DeploymentMaterializationSession(home, InstallScope.USER, lock).baseline(path, [target])
        == content_hash
    )
    moved_root = tmp_path / "other-external-target"
    moved_target = (
        replace(target, resolved_deploy_root=moved_root)
        if target.resolved_deploy_root is not None
        else replace(target, root_dir=moved_root.as_posix())
    )
    reloaded = DeploymentMaterializationSession(home, InstallScope.USER, lock)
    assert reloaded.baseline(path, [moved_target]) is None
    assert (
        reloaded.baseline(moved_target.skills_deploy_path(home) / "demo/SKILL.md", [moved_target])
        is None
    )
    lock.dependencies["foreign"] = lock.dependencies.pop("pkg")
    assert (
        DeploymentMaterializationSession(home, InstallScope.USER, lock).baseline(path, [target])
        is None
    )


@pytest.mark.parametrize(
    "condition", ["valid", "foreign-writer", "unbound", "mismatched-root", "changed-claims"]
)
def test_target_handoff_requires_authenticated_same_writer_without_promoting_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    from dataclasses import replace

    previous = KNOWN_TARGETS["copilot"]
    selected = KNOWN_TARGETS["codex"]
    root, path, lock = _fixture(tmp_path, monkeypatch, target=previous)
    session = _session(root, LockFile() if condition == "unbound" else lock)
    owner = "foreign" if condition == "foreign-writer" else "pkg"
    session.record({path: compute_file_hash(path)}, [previous], owner)
    session.flush()
    before = session.state_path.read_bytes()
    if condition == "mismatched-root":
        selected = replace(selected, resolved_deploy_root=tmp_path / "other-root")
    elif condition == "changed-claims":
        row = next(iter(lock.deployment_ledger.records.values()))
        changed = DeploymentRecord(row.locator, ("foreign",), "foreign", row.content_hash)
        lock.dependencies["foreign"] = LockedDependency(repo_url="foreign")
        DeploymentLedgerCodec.apply_to_lockfile(
            DeploymentLedger({row.locator.key: changed}), lock, write_legacy_views=False
        )
    reloaded = _session(root, lock)
    assert reloaded.requires_target_write(path, [previous], "pkg") is False
    assert reloaded.requires_target_write(path, [selected], "pkg") is (condition == "valid")
    reloaded.bind_committed_claims(lock)
    assert reloaded.state_path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "corrupt",
        "owner",
        "scope",
        "identity",
        "destination",
        "member",
        "target",
        "duplicate",
        "binding",
    ],
)
def test_missing_or_mismatched_evidence_never_falls_back_to_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    if damage == "missing":
        session.state_path.unlink()
    elif damage == "corrupt":
        session.state_path.write_text("{", encoding="utf-8")
    else:
        data = json.loads(session.state_path.read_text(encoding="utf-8"))
        if damage == "owner":
            data["records"][0]["owners"] = ["foreign"]
            data["records"][0]["active_owner"] = "foreign"
        elif damage == "scope":
            data["scope"] = "user"
        elif damage == "identity":
            data["installation"] = "0" * 32
        elif damage == "destination":
            key = next(iter(data["destinations"]))
            data["destinations"][key] = (root / "foreign/SKILL.md").as_posix()
        elif damage == "duplicate":
            data["records"].append(data["records"][0])
        elif damage == "binding":
            key = next(iter(data["bindings"]))
            data["bindings"][key] = [".", "[]"]
        else:
            data["records"][0]["value" if damage == "member" else "target"] = "foreign"
        session.state_path.write_text(json.dumps(data), encoding="utf-8")
    if damage in {"corrupt", "scope", "identity", "member", "target", "duplicate"}:
        with pytest.raises(ValueError, match="materialization evidence"):
            _session(root, lock)
    else:
        assert _session(root, lock).baseline(path, [TARGET]) is None


@pytest.mark.parametrize("copy_identity", [False, True])
def test_recreated_checkout_cannot_reuse_old_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy_identity: bool
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    root.rename(tmp_path / "old-consumer")
    path.parent.mkdir(parents=True)
    path.write_text("rendered old\n", encoding="utf-8")
    if copy_identity:
        identity = root / ".apm-local/installation"
        identity.parent.mkdir()
        identity.write_bytes((tmp_path / "old-consumer/.apm-local/installation").read_bytes())
        with pytest.raises(ValueError, match="installation binding does not match"):
            _session(root, lock)
    else:
        assert _session(root, lock).baseline(path, [TARGET]) is None


def test_atomic_failure_cannot_publish_new_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.utils import atomic_io

    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    before = session.state_path.read_bytes()
    write = atomic_io.atomic_write_text

    def refuse(destination: Path, text: str, **kwargs) -> None:
        if destination == session.state_path:
            raise OSError("injected receipt refusal")
        write(destination, text, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", refuse)
    path.write_text("new rendered\n", encoding="utf-8")
    with pytest.raises(OSError, match="receipt refusal"):
        session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
        session.flush()
    assert session.state_path.read_bytes() == before


def test_identity_creation_preserves_existing_foreign_ignore_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.identity_path.parent.mkdir()
    ignore = session.identity_path.parent / ".gitignore"
    original = b"# Existing local tooling\n/private-state/\n"
    ignore.write_bytes(original)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    with pytest.raises(ValueError, match="Refusing to replace existing installation identity"):
        session.flush()
    assert ignore.read_bytes() == original
    assert not session.identity_path.exists()
    assert not session.state_path.exists()


def test_unclaimed_or_other_target_receipt_cannot_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    assert _session(root, LockFile()).baseline(path, [TARGET]) is None
    assert _session(root, lock).baseline(path, [KNOWN_TARGETS["codex"]]) is None


def test_cleanup_retires_only_successfully_removed_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.integration.cleanup import remove_stale_deployed_files
    from apm_cli.utils.diagnostics import DiagnosticCollector

    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    entry = path.relative_to(root).as_posix()
    original_hash = compute_file_hash(path)
    path.write_text("edited\n", encoding="utf-8")
    result = remove_stale_deployed_files(
        [entry],
        root,
        dep_key="pkg",
        targets=[TARGET],
        diagnostics=DiagnosticCollector(),
        recorded_hashes={entry: original_hash},
    )
    assert result.skipped_user_edit == [entry]
    assert _session(root, lock).baseline(path, [TARGET]) == original_hash
    path.write_text("rendered old\n", encoding="utf-8")
    result = remove_stale_deployed_files(
        [entry],
        root,
        dep_key="pkg",
        targets=[TARGET],
        diagnostics=DiagnosticCollector(),
        recorded_hashes={entry: original_hash},
    )
    assert result.deleted == [entry]
    path.write_text("rendered old\n", encoding="utf-8")
    session.bind_committed_claims(lock)
    assert _session(root, lock).baseline(path, [TARGET]) is None


@pytest.mark.parametrize("managed", [True, False], ids=["recorded", "legacy"])
def test_sync_reports_only_successful_removals_for_receipt_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: bool
) -> None:
    import shutil
    from types import SimpleNamespace

    from apm_cli.integration.skill_integrator import SkillIntegrator

    root, path, lock = _fixture(tmp_path, monkeypatch)
    blocked = path.parent.parent / "blocked/SKILL.md"
    blocked.parent.mkdir()
    blocked.write_bytes(path.read_bytes())
    locator = DeploymentLedgerCodec.locator_for_path(
        blocked, project_root=root, target=TARGET, scope=InstallScope.PROJECT
    )
    records = dict(lock.deployment_ledger.records)
    records[locator.key] = DeploymentRecord(locator, ("pkg",), "pkg", compute_file_hash(blocked))
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(records), lock, write_legacy_views=False
    )
    session = _session(root, lock)
    session.record(
        {member: compute_file_hash(member) for member in (path, blocked)}, [TARGET], "pkg"
    )
    session.flush()
    rmtree = shutil.rmtree

    def remove(directory: Path, *args, **kwargs) -> None:
        if directory == blocked.parent:
            raise PermissionError("injected failed skill removal")
        rmtree(directory, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove)
    removed: list[Path] = []
    result = SkillIntegrator().sync_integration(
        SimpleNamespace(get_apm_dependencies=lambda: []),
        root,
        managed_files=(
            {member.parent.relative_to(root).as_posix() for member in (path, blocked)}
            if managed
            else None
        ),
        targets=[TARGET],
        removed_paths=removed,
    )
    assert result == {"files_removed": 1, "errors": 1}
    assert removed == [path.parent]
    DeploymentMaterializationSession.retire_removed(root, removed, user_scope=False)
    path.parent.mkdir()
    path.write_bytes(blocked.read_bytes())
    reloaded = _session(root, lock)
    assert reloaded.baseline(path, [TARGET]) is None
    assert reloaded.baseline(blocked, [TARGET]) == compute_file_hash(blocked)


def test_uninstall_retires_before_recreating_and_binding_survivor_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.cli import _persist_uninstall_lockfile
    from apm_cli.commands.uninstall.engine import _sync_integrations_after_uninstall
    from apm_cli.commands.uninstall.lockfile_state import reconcile_uninstall_deployment_state
    from apm_cli.models.apm_package import APMPackage, DependencyReference, PackageInfo

    root, path, lock = _fixture(tmp_path, monkeypatch)
    owner = "acme/keeper"
    row = next(iter(lock.deployment_ledger.records.values()))
    lock.dependencies = {owner: LockedDependency(repo_url=owner)}
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            {row.locator.key: DeploymentRecord(row.locator, (owner,), owner, row.content_hash)}
        ),
        lock,
    )
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], owner)
    session.flush()
    package = root / "apm_modules/acme/keeper"
    source = package / ".apm/skills/demo/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: demo\ndescription: survivor\n---\nNew survivor bytes.\n", encoding="utf-8"
    )
    (package / "apm.yml").write_text("name: keeper\nversion: 1.0.0\n", encoding="utf-8")
    (root / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: [{owner}]\n",
        encoding="utf-8",
    )
    dep = DependencyReference.parse(owner)
    info = PackageInfo(APMPackage.from_apm_yml(package / "apm.yml"), package, dependency_ref=dep)
    entry = path.relative_to(root).as_posix()
    retire = DeploymentMaterializationSession.retire_removed
    observed = []

    def retire_before_copy(
        project_root: Path, paths: list[Path], *, user_scope: bool = False
    ) -> None:
        if paths:
            assert paths == [path]
            assert not path.exists()
        retire(project_root, paths, user_scope=user_scope)
        if paths:
            assert (
                path.as_posix()
                not in json.loads(session.state_path.read_text())["destinations"].values()
            )
            observed.append("retired")

    monkeypatch.setattr(DeploymentMaterializationSession, "retire_removed", retire_before_copy)
    outcome = _sync_integrations_after_uninstall(
        APMPackage.from_apm_yml(root / "apm.yml"),
        root,
        {entry},
        MagicMock(),
        lockfile=lock,
        modules_dir=root / "apm_modules",
        survivor_plan=[(dep, info)],
    )
    assert observed == ["retired"]
    assert outcome.counts["skills"] == 1
    assert path.read_bytes() == source.read_bytes()
    reconcile_uninstall_deployment_state(
        lock,
        deploy_root=root,
        all_deployed_files={entry},
        surviving_deployed_files=outcome.deployed_files,
    )
    lock_path = root / "apm.lock.yaml"
    _persist_uninstall_lockfile(lock, lock_path, MagicMock(), outcome)
    final = _session(root, LockFile.read(lock_path))
    assert final.baseline(path, [TARGET]) == compute_file_hash(source)


@pytest.mark.windows_compat
def test_scope_isolation_and_atomic_lf_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    raw = session.state_path.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    user = DeploymentMaterializationSession(root, InstallScope.USER, lock)
    assert user.state_path != session.state_path
    assert user.baseline(path, [TARGET]) is None


def test_changed_canonical_claims_cannot_rebind_without_actual_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    before = session.state_path.read_bytes()
    row = next(iter(lock.deployment_ledger.records.values()))
    lock.dependencies["other"] = LockedDependency(repo_url="other")
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            {row.locator.key: DeploymentRecord(row.locator, ("other",), "other", row.content_hash)}
        ),
        lock,
        write_legacy_views=False,
    )
    new_session = _session(root, lock)
    assert new_session.baseline(path, [TARGET]) is None
    new_session.bind_committed_claims(lock)
    assert session.state_path.read_bytes() == before
    assert _session(root, lock).baseline(path, [TARGET]) is None


@pytest.mark.parametrize("binding", ["identity", "state", "ignore"])
@pytest.mark.parametrize("outside", [False, True], ids=["contained", "escaping"])
def test_symlinked_evidence_is_rejected_without_touching_referent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str, outside: bool
) -> None:
    from apm_cli.utils.path_security import PathTraversalError

    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    session.flush()
    selected = {
        "identity": session.identity_path,
        "state": session.state_path,
        "ignore": session.identity_path.parent / ".gitignore",
    }[binding]
    referent = (tmp_path if outside else selected.parent) / f"saved-{binding}"
    selected.rename(referent)
    original = referent.read_bytes()
    selected.symlink_to(referent)
    expected = "outside the allowed base" if outside else "symlinked materialization evidence"
    with pytest.raises(PathTraversalError, match=expected):
        _session(root, lock)
    assert referent.read_bytes() == original


@pytest.mark.parametrize("binding", ["identity", "state", "ignore"])
def test_nonregular_control_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    root, _, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    path = {
        "identity": session.identity_path,
        "state": session.state_path,
        "ignore": session.identity_path.parent / ".gitignore",
    }[binding]
    path.mkdir(parents=True)
    sentinel = path / "foreign.txt"
    sentinel.write_bytes(b"foreign directory contents\n")
    with pytest.raises(ValueError, match="non-regular materialization evidence"):
        _session(root, lock)
    assert sentinel.read_bytes() == b"foreign directory contents\n"


@pytest.mark.parametrize("binding", ["identity", "state", "ignore"])
def test_control_file_appearing_during_creation_is_never_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str
) -> None:
    from apm_cli.utils import atomic_io

    root, path, lock = _fixture(tmp_path, monkeypatch)
    session = _session(root, lock)
    selected = {
        "identity": session.identity_path,
        "state": session.state_path,
        "ignore": session.identity_path.parent / ".gitignore",
    }[binding]
    link = atomic_io.os.link

    def competing_write(source: str, destination: Path) -> None:
        if destination == selected:
            destination.write_bytes(b"foreign control bytes\n")
        link(source, destination)

    monkeypatch.setattr(atomic_io.os, "link", competing_write)
    session.record({path: compute_file_hash(path)}, [TARGET], "pkg")
    with pytest.raises(FileExistsError):
        session.flush()
    assert selected.read_bytes() == b"foreign control bytes\n"
    if binding != "state":
        assert not session.state_path.exists()
    assert list(selected.parent.glob("apm-atomic-*")) == []


@pytest.mark.parametrize("stage", ["copy", "rewrite", "hash", "persist"])
def test_failed_rendering_never_promotes_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import shutil

    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.utils import atomic_io, content_hash
    from tests.unit.integration.test_skill_materialization_completion import (
        _fixture as skill_fixture,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    package, root, plan = skill_fixture(tmp_path)
    session = _session(root, LockFile())
    integrator = SkillIntegrator()

    def fail(*args, **kwargs):
        raise OSError(f"injected {stage} failure")

    if stage == "copy":
        monkeypatch.setattr(shutil, "copy2", fail)
    elif stage == "rewrite":
        monkeypatch.setattr(integrator, "_resolve_markdown_links_in_skill_bundle", fail)
    elif stage == "hash":
        monkeypatch.setattr(content_hash, "compute_file_hash", fail)
    else:
        monkeypatch.setattr(atomic_io, "atomic_write_text", fail)
    with (
        pytest.raises(OSError, match=f"injected {stage} failure"),
        integrator.ownership_snapshot(materialization_session=session),
    ):
        integrator.integrate_package_skill(
            package, root, targets=[TARGET], source_plan=plan, managed_files=set(), skip_bin=True
        )
    assert not session.state_path.exists()


def test_identical_unowned_members_do_not_create_local_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from apm_cli.integration.skill_integrator import SkillIntegrator
    from tests.unit.integration.test_skill_materialization_completion import (
        _fixture as skill_fixture,
    )

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    package, root, plan = skill_fixture(tmp_path)
    shutil.copytree(package.install_path / "skills/s", root / ".claude/skills/s")
    session = _session(root, LockFile())
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot(materialization_session=session):
        result = integrator.integrate_package_skill(
            package, root, targets=[TARGET], source_plan=plan, managed_files=set(), skip_bin=True
        )
    assert result.deployed_files
    assert not session.state_path.exists()
