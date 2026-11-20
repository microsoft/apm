"""Deletion requires deployment evidence, never a lockfile hash alone (req-lk-024).

Amendment 0.1.69 (issue #3701), adopted 2026-11-20. The oracles drive
the cleanup owner directly: a forged lockfile row whose hash matches a user
file keeps the file and names it; a local deployment receipt or byte
equality with the locked render authorizes the deletion; a file that
differs from its evidence is kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.spec_conformance._helpers import assert_spec_contains

pytestmark = pytest.mark.component


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
    )

    result, _messages = _cleanup(root, evidence, path)

    assert result.deleted == [_PATH]
    assert not path.exists()


@pytest.mark.req("req-lk-024")
def test_file_differing_from_its_evidence_is_kept(tmp_path: Path) -> None:
    path = _deployed(tmp_path)
    receipts = {path.resolve().as_posix(): compute_file_hash(path)}
    path.write_text("edited by the user\n", encoding="utf-8")
    evidence = DeploymentEvidence(deploy_root=tmp_path, receipts=receipts, render=None)

    result, messages = _cleanup(tmp_path, evidence, path)

    assert path.read_text(encoding="utf-8") == "edited by the user\n"
    assert result.skipped_user_edit == [_PATH]
    assert "edited since APM deployed it" in messages
