"""Deletion requires deployment evidence; a lockfile hash alone authorizes it only under consent.

req-lk-024.

Amendment 0.1.69 (issue #3701), adopted 2026-11-20, and the proposed 0.1.76
consent exception (issue #4295). The oracles drive
the cleanup owner directly: a forged lockfile row whose hash matches a user
file keeps the file and names it; a local deployment receipt or byte
equality with the locked render authorizes the deletion; a file that
differs from its evidence is kept.
"""

from __future__ import annotations

import os
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
    assert_spec_contains(
        "under the consent exception below, a lockfile content hash alone MUST NOT"
    )


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


def _consent_evidence(
    root: Path, path: Path, *, render, recorded=None, receipts=None, readable: bool = True
) -> DeploymentEvidence:
    return DeploymentEvidence(
        deploy_root=root,
        receipts=receipts or {},
        render=render,
        owners={_PATH: ("owner/pkg",)},
        kept=(),
        recorded_hashes={
            _PATH: frozenset({compute_file_hash(path)} if recorded is None else recorded)
        },
        receipts_readable=readable,
    )


@pytest.mark.req("req-lk-024")
def test_spec_states_the_per_invocation_consent_exception() -> None:
    """Proposed amendment 0.1.76 (issue #4295)."""
    assert_spec_contains("One consent exception applies, only to an uninstall the user explicitly")
    assert_spec_contains("MUST NOT infer consent from another option,")
    assert_spec_contains(
        "compared per [req-lk-016](#req-lk-016) (a bare hexadecimal value reads as"
    )


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("consent", [False, True])
def test_consent_deletes_only_unevidenced_bytes_equal_to_the_single_recorded_hash(
    tmp_path: Path, consent: bool
) -> None:
    path = _deployed(tmp_path)
    evidence = _consent_evidence(tmp_path, path, render=lambda _owner: None)
    if consent:
        evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(tmp_path, evidence, path)

    assert path.exists() is not consent
    assert result.deleted == ([_PATH] if consent else [])
    assert evidence.consented_deletions() == ([_PATH] if consent else [])
    # Each deletion is named exactly once, at whichever boundary reports first.
    assert evidence.take_consented_deletions() == ([_PATH] if consent else [])
    assert evidence.take_consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_never_overrides_a_render_that_lacks_the_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    render.mkdir()
    evidence = _consent_evidence(root, path, render=lambda _owner: render)
    evidence.consent_to_lockfile_hashes()

    result, messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []
    assert f"Kept {_PATH}" in messages


_OTHER = "sha256:" + "0" * 64


def _no_render(_owner: str) -> None:
    return None


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize(
    "case",
    [
        "no-recorded-hash",
        "two-distinct-hashes",
        "other-algorithm",
        "unreadable-receipts",
        "differing-receipt",
        "edited-after-preview",
        "symbolic-link",
    ],
)
def test_consent_keeps_every_file_the_exception_does_not_cover(tmp_path: Path, case: str) -> None:
    root = tmp_path / "project"
    path = _deployed(root)
    digest = compute_file_hash(path)
    options: dict[str, object] = {}
    if case == "no-recorded-hash":
        options["recorded"] = frozenset()
    elif case == "two-distinct-hashes":
        options["recorded"] = frozenset({digest, _OTHER})
    elif case == "other-algorithm":
        options["recorded"] = frozenset({digest.replace("sha256:", "sha512:")})
    elif case == "unreadable-receipts":
        options["readable"] = False
    elif case == "differing-receipt":
        options["receipts"] = {path.resolve().as_posix(): _OTHER}
    elif case == "symbolic-link":
        target = tmp_path / "elsewhere.md"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are unavailable")
    evidence = _consent_evidence(root, path, render=_no_render, **options)
    evidence.consent_to_lockfile_hashes()
    if case == "edited-after-preview":
        assert evidence.consent_preview([_PATH]) == frozenset({_PATH})
        path.write_text("edited after the preview\n", encoding="utf-8")

    result, _messages = _cleanup(root, evidence, path)

    assert os.path.lexists(path) and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_reads_a_bare_hex_recorded_hash_as_sha256(tmp_path: Path) -> None:
    path = _deployed(tmp_path)
    bare = compute_file_hash(path).removeprefix("sha256:").upper()
    evidence = _consent_evidence(tmp_path, path, render=_no_render, recorded=frozenset({bare}))
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(tmp_path, evidence, path)

    assert result.deleted == [_PATH] and not path.exists()
    assert evidence.consented_deletions() == [_PATH]


@pytest.mark.req("req-lk-024")
def test_consent_records_a_file_edited_after_the_preview_as_kept(tmp_path: Path) -> None:
    """The deletion recheck, not the preview, decides what consent kept."""
    path = _deployed(tmp_path)
    evidence = _consent_evidence(tmp_path, path, render=_no_render)
    evidence.consent_to_lockfile_hashes()
    assert evidence.consent_preview([_PATH]) == frozenset({_PATH})
    path.write_text("edited after the preview\n", encoding="utf-8")

    result, _messages = _cleanup(tmp_path, evidence, path)

    assert path.read_text(encoding="utf-8") == "edited after the preview\n"
    assert result.deleted == [] and evidence.kept_under_consent() == frozenset({_PATH})


@pytest.mark.req("req-lk-024")
def test_consent_deletes_each_eligible_member_of_a_skill_directory_it_keeps(
    tmp_path: Path,
) -> None:
    """A legacy lock lists the directory; one edited member keeps only itself."""
    skill = ".agents/skills/alpha"
    kept, gone = f"{skill}/SKILL.md", f"{skill}/notes.md"
    for member in (kept, gone):
        (tmp_path / member).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / member).write_text(f"{member}\n", encoding="utf-8")
    hashes = {member: compute_file_hash(tmp_path / member) for member in (kept, gone)}
    (tmp_path / kept).write_text("my edit\n", encoding="utf-8")
    evidence = DeploymentEvidence(
        deploy_root=tmp_path,
        receipts={},
        render=_no_render,
        owners={skill: ("owner/pkg",)},
        kept=(),
        recorded_hashes={member: frozenset({value}) for member, value in hashes.items()},
    )
    evidence.consent_to_lockfile_hashes()
    diagnostics = DiagnosticCollector()

    result = remove_stale_deployed_files(
        [skill],
        tmp_path,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=hashes,
        receipt_retirements=[],
    )

    assert not (tmp_path / gone).exists() and gone in result.deleted
    assert (tmp_path / kept).read_text(encoding="utf-8") == "my edit\n"
    assert evidence.consented_deletions() == [gone]
    assert evidence.kept_under_consent() == frozenset({kept})


def _skill_world(root: Path, members: dict[str, str]) -> tuple[dict[str, str], DeploymentEvidence]:
    """A legacy skill directory whose members match their recorded hashes, under consent."""
    for member, text in members.items():
        (root / member).parent.mkdir(parents=True, exist_ok=True)
        (root / member).write_text(text, encoding="utf-8")
    hashes = {member: compute_file_hash(root / member) for member in members}
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=_no_render,
        owners={".agents/skills/alpha": ("owner/pkg",)},
        kept=(),
        recorded_hashes={member: frozenset({value}) for member, value in hashes.items()},
    )
    evidence.consent_to_lockfile_hashes()
    return hashes, evidence


@pytest.mark.req("req-lk-024")
def test_consent_never_deletes_a_member_a_remaining_dependency_still_deploys(
    tmp_path: Path,
) -> None:
    """A survivor's member inside the departing directory is outside the exception."""
    skill = ".agents/skills/alpha"
    edited, gone, shared = f"{skill}/SKILL.md", f"{skill}/notes.md", f"{skill}/shared.md"
    hashes, evidence = _skill_world(
        tmp_path, {edited: "skill\n", gone: "notes\n", shared: "shared\n"}
    )
    (tmp_path / edited).write_text("my edit\n", encoding="utf-8")

    result = remove_stale_deployed_files(
        [skill],
        tmp_path,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes=hashes,
        receipt_retirements=[],
        live_paths=[shared],
    )

    assert (tmp_path / shared).read_text(encoding="utf-8") == "shared\n"
    assert shared not in result.deleted
    assert not (tmp_path / gone).exists()
    assert evidence.consented_deletions() == [gone]


@pytest.mark.req("req-lk-024")
def test_consent_rechecks_each_member_immediately_before_deleting_it(tmp_path: Path) -> None:
    """A member edited after the directory scan proved it is kept, not deleted."""
    skill = ".agents/skills/alpha"
    edited, late = f"{skill}/SKILL.md", f"{skill}/notes.md"
    hashes, evidence = _skill_world(tmp_path, {edited: "skill\n", late: "notes\n"})
    (tmp_path / edited).write_text("my edit\n", encoding="utf-8")
    proven = evidence.verdict

    def scan_then_user_edits(claim: str, physical: Path, **kwargs) -> str:
        verdict = proven(claim, physical, **kwargs)
        if claim == late and physical.read_text(encoding="utf-8") == "notes\n":
            physical.write_text("edited during the scan\n", encoding="utf-8")
        return verdict

    evidence.verdict = scan_then_user_edits  # the user's edit lands right after the scan

    result = remove_stale_deployed_files(
        [skill],
        tmp_path,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes=hashes,
        receipt_retirements=[],
    )

    assert (tmp_path / late).read_text(encoding="utf-8") == "edited during the scan\n"
    assert late not in result.deleted and late in result.unproven
    assert evidence.consented_deletions() == []
    assert evidence.kept_under_consent() == frozenset({edited, late})


@pytest.mark.req("req-lk-024")
def test_consent_classifies_a_standalone_symlinked_file_as_kept(tmp_path: Path) -> None:
    """A tracked file replaced by a link is outside the exception: kept, never a failure."""
    target = tmp_path / "elsewhere.md"
    target.write_text("outside\n", encoding="utf-8")
    link = tmp_path / _PATH
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    evidence = _consent_evidence(tmp_path, target, render=_no_render)
    evidence.consent_to_lockfile_hashes()

    assert [claim for claim, _verdict in evidence.refusals([_PATH])] == [_PATH]
    assert evidence.kept_under_consent() == frozenset({_PATH})
    assert target.read_text(encoding="utf-8") == "outside\n"
