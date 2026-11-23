"""Consent reads case-variant spellings of one file as one file (req-lk-024, #4295).

Only a case-insensitive filesystem (Windows, default macOS) can exercise this
cell, so it lives in the windows_compat family the Windows lane selects; the
Ubuntu spec shards would only skip it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
]

_PATH = ".github/instructions/notes.instructions.md"
_OTHER = "sha256:" + "0" * 64


@pytest.mark.req("req-lk-024")
def test_consent_reads_case_variant_spellings_as_one_file(tmp_path: Path) -> None:
    """On a case-insensitive filesystem a conflicting hash under ``NOTES`` blocks ``notes``."""
    root = tmp_path / "project"
    path = root / _PATH
    path.parent.mkdir(parents=True)
    path.write_text("---\napplyTo: '**'\n---\n# Notes\n", encoding="utf-8")
    variant = _PATH.replace("notes.", "NOTES.")
    if not (root / variant).exists():
        pytest.skip("case-sensitive filesystem: the spellings name different files")
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda _owner: None,
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)}), variant: frozenset({_OTHER})},
    )
    evidence.consent_to_lockfile_hashes()

    result = remove_stale_deployed_files(
        [_PATH],
        root,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes={_PATH: compute_file_hash(path)},
        receipt_retirements=[],
    )

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
def test_consent_reads_case_variant_cowork_spellings_as_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under an external Cowork root a conflicting hash under ``Alpha`` blocks ``alpha`` (#4815)."""
    cowork = tmp_path / "cowork-skills"
    path = cowork / "alpha" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Notes\n", encoding="utf-8")
    variant = cowork / "Alpha" / "notes.md"
    if not variant.exists():
        # A case-sensitive filesystem: a hard link makes both spellings one file.
        variant.parent.mkdir()
        try:
            os.link(path, variant)
        except OSError:
            pytest.skip("hard links are unavailable")
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(cowork))
    claim = "cowork://skills/alpha/notes.md"
    evidence = DeploymentEvidence(
        deploy_root=tmp_path / "project",
        receipts={},
        render=lambda _owner: None,
        kept=(),
        recorded_hashes={
            claim: frozenset({compute_file_hash(path)}),
            "cowork://skills/Alpha/notes.md": frozenset({_OTHER}),
        },
    )
    evidence.consent_to_lockfile_hashes()

    assert evidence.lockfile_consentable(claim, path) is False
    assert evidence.refusals([claim])
