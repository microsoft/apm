"""Deletion requires deployment evidence, never a lockfile hash alone (req-lk-024).

Amendment 0.1.69 (issue #3701), adopted 2026-11-20. The oracles drive
the cleanup owner directly: a forged lockfile row whose hash matches a user
file keeps the file and names it; a local deployment receipt or byte
equality with the locked render authorizes the deletion; a file that
differs from its evidence is kept. Proposed amendment 0.1.77 (issue #3188):
a file no lock entry claims goes only on this installation's held receipt.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.spec_conformance._helpers import assert_spec_contains

_PATH = ".github/instructions/notes.instructions.md"


def _deployed(root: Path, text: str = "---\napplyTo: '**'\n---\n# Notes\n") -> Path:
    path = root / _PATH
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _cleanup(root: Path, evidence: DeploymentEvidence, path: Path):
    diagnostics = DiagnosticCollector()
    result = remove_stale_deployed_files(
        [_PATH],
        root,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes={_PATH: compute_file_hash(path)},
        receipt_retirements=[],
    )
    return result, " ".join(d.message for d in diagnostics._diagnostics)


@pytest.mark.req("req-lk-024")
def test_spec_states_hash_alone_never_authorizes_deletion() -> None:
    assert_spec_contains("**[req-lk-024]** When a conforming **consumer** implementation deletes a")
    assert_spec_contains("lockfile content hash alone MUST NOT authorize the deletion")


@pytest.mark.req("req-lk-024")
def test_forged_lockfile_hash_keeps_the_file_and_names_it(tmp_path: Path) -> None:
    path = _deployed(tmp_path)
    before = path.read_bytes()

    result, messages = _cleanup(tmp_path, DeploymentEvidence.none(tmp_path), path)

    assert path.read_bytes() == before
    assert result.deleted == [] and result.unproven == [_PATH]
    assert f"Kept {_PATH}" in messages and "Delete it manually" in messages


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("source", ["receipt", "locked-render"])
def test_receipt_or_locked_render_authorizes_deletion(tmp_path: Path, source: str) -> None:
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    (render / _PATH).parent.mkdir(parents=True)
    (render / _PATH).write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts=(
            {path.resolve().as_posix(): compute_file_hash(path)} if source == "receipt" else {}
        ),
        render=(lambda _owner: render) if source == "locked-render" else None,
        kept=(),
    )

    result, _messages = _cleanup(root, evidence, path)

    assert result.deleted == [_PATH]
    assert not path.exists()


@pytest.mark.req("req-lk-024")
def test_file_differing_from_its_evidence_is_kept(tmp_path: Path) -> None:
    path = _deployed(tmp_path)
    receipts = {path.resolve().as_posix(): compute_file_hash(path)}
    path.write_text("edited by the user\n", encoding="utf-8")
    evidence = DeploymentEvidence(deploy_root=tmp_path, receipts=receipts, render=None, kept=())

    result, messages = _cleanup(tmp_path, evidence, path)

    assert path.read_text(encoding="utf-8") == "edited by the user\n"
    assert result.skipped_user_edit == [_PATH]
    assert "edited since APM deployed it" in messages


_LOCK = """lockfile_version: '4'
apm_version: 0.37.0
dependencies:
- repo_url: _local/alpha
  name: alpha
  version: 0.1.0
  package_type: apm_package
  deployed_files:
  - .claude/skills/alpha
  - .claude/skills/alpha/SKILL.md
  source: local
  local_path: ./pkgs/alpha
"""


def _receipted_project(root: Path) -> dict[str, Path]:
    """Deploy and receipt alpha, beta and edited; add an unreceipted identical copy."""
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.targets import KNOWN_TARGETS

    paths = {
        name: root / ".claude" / "skills" / name / "SKILL.md"
        for name in ("alpha", "beta", "edited", "copy")
    }
    for path in paths.values():
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: skill\n---\nSkill.\n", encoding="utf-8")
    session = DeploymentMaterializationSession(root, InstallScope.PROJECT, None)
    for name in ("alpha", "beta", "edited"):
        session.record(
            {paths[name]: compute_file_hash(paths[name])},
            [KNOWN_TARGETS["claude"]],
            f"./pkgs/{name}",
        )
    session.flush()
    paths["edited"].write_text("edited by the user\n", encoding="utf-8")
    return paths


@pytest.mark.req("req-lk-024")
def test_spec_states_receipt_bound_deletion_of_unclaimed_files() -> None:
    assert_spec_contains(
        "A consumer MAY also delete a deployed file that no current lockfile entry"
    )
    assert_spec_contains("An audit MUST report such a file as orphaned without")


@pytest.mark.req("req-lk-024")
def test_only_this_installations_held_receipt_authorizes_deleting_an_unclaimed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.core.receipt_orphans import ReceiptOrphan, receipt_orphans, remove_receipt_orphans
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile

    monkeypatch.setenv("APM_HOME", str(tmp_path / "home" / ".apm"))
    root = tmp_path / "project"
    paths = _receipted_project(root)
    lock = LockFile.from_yaml(_LOCK)
    receipt = next((tmp_path / "home" / ".apm").rglob("*.json"))
    before = receipt.read_bytes()

    # Read-only selection (audit): claimed and unreceipted files are never candidates.
    assert receipt_orphans(root, InstallScope.PROJECT, lock) == (
        ReceiptOrphan(".claude/skills/beta/SKILL.md", held=True),
        ReceiptOrphan(".claude/skills/edited/SKILL.md", held=False),
    )
    assert receipt_orphans(root, InstallScope.PROJECT, None) == ()
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    assert receipt_orphans(moved, InstallScope.PROJECT, lock) == ()
    assert receipt.read_bytes() == before

    diagnostics = DiagnosticCollector()
    result = remove_receipt_orphans(root, InstallScope.PROJECT, lock, diagnostics=diagnostics)

    assert result.deleted == [".claude/skills/beta/SKILL.md"]
    assert not paths["beta"].parent.exists()
    assert paths["alpha"].is_file() and paths["copy"].is_file()
    assert paths["edited"].read_text(encoding="utf-8") == "edited by the user\n"
    messages = [d.message for d in diagnostics._diagnostics]
    assert len(messages) == 1
    assert ".claude/skills/edited/SKILL.md" in messages[0] and "manually" in messages[0]
    assert receipt_orphans(root, InstallScope.PROJECT, lock) == (
        ReceiptOrphan(".claude/skills/edited/SKILL.md", held=False),
    )
