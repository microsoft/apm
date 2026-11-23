"""Deletion requires deployment evidence; a lockfile hash alone authorizes it only under consent.

req-lk-024.

Amendment 0.1.69 (issue #3701), adopted 2026-11-20, and the proposed 0.1.77
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
    """Proposed amendment 0.1.77 (issue #4295)."""
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

    assert (path.is_symlink() or path.exists()) and result.deleted == []
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


@pytest.mark.req("req-lk-024")
def test_consent_rechecks_an_all_matching_skill_directory_before_removing_it(
    tmp_path: Path,
) -> None:
    """Every member matched at the scan; one edited before its unlink is kept."""
    skill = ".agents/skills/alpha"
    first, late = f"{skill}/SKILL.md", f"{skill}/notes.md"
    hashes, evidence = _skill_world(tmp_path, {first: "skill\n", late: "notes\n"})
    scanned = evidence.verdict
    seen: dict[str, int] = {}

    def scan_then_user_edits(claim: str, physical: Path, **kwargs) -> str:
        verdict = scanned(claim, physical, **kwargs)
        seen[claim] = seen.get(claim, 0) + 1
        if claim == late and seen[claim] == 1:
            physical.write_text("edited after the scan\n", encoding="utf-8")
        return verdict

    evidence.verdict = scan_then_user_edits

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

    assert (tmp_path / late).read_text(encoding="utf-8") == "edited after the scan\n"
    assert not (tmp_path / first).exists()
    assert skill not in result.deleted and late not in result.deleted
    assert evidence.consented_deletions() == [first]


@pytest.mark.req("req-lk-024")
def test_consent_reads_recorded_hashes_under_one_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicting hash recorded under a backslash key still blocks consent."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    path = _deployed(tmp_path)
    native = _PATH.replace("/", "\\")
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="owner/pkg",
            deployed_files=[_PATH],
            deployed_file_hashes={_PATH: compute_file_hash(path)},
        )
    )
    lock.add_dependency(
        LockedDependency(
            repo_url="owner/other",
            deployed_files=[_PATH],
            deployed_file_hashes={native: _OTHER},
        )
    )

    def source_gone(*_args, **_kwargs):
        raise OSError("source unreachable")

    monkeypatch.setattr("apm_cli.install.drift.run_replay", source_gone)
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    evidence = DeploymentEvidence.collect(
        tmp_path,
        user_scope=False,
        lockfile=lock,
        lockfile_root=tmp_path,
        diagnostics=DiagnosticCollector(),
        kept=(),
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(tmp_path, evidence, path)

    assert path.exists() and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_keeps_a_skill_directory_that_gained_a_link_after_the_scan(
    tmp_path: Path,
) -> None:
    """A dangling link planted after the member scan blocks removing the directory."""
    skill = ".agents/skills/alpha"
    first, second = f"{skill}/SKILL.md", f"{skill}/notes.md"
    hashes, evidence = _skill_world(tmp_path, {first: "skill\n", second: "notes\n"})
    scanned = evidence.verdict
    planted = tmp_path / skill / "planted.md"

    def scan_then_plant(claim: str, physical: Path, **kwargs) -> str:
        verdict = scanned(claim, physical, **kwargs)
        if claim == second and not os.path.lexists(planted):
            try:
                os.symlink(tmp_path / "missing-target.md", planted)
            except (OSError, NotImplementedError):
                pytest.skip("symbolic links are unavailable")
        return verdict

    evidence.verdict = scan_then_plant

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

    assert planted.is_symlink()
    assert skill not in result.deleted


@pytest.mark.req("req-lk-024")
def test_consent_keeps_a_cowork_final_symlink_and_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Cowork claim whose final component is a link is never consented through it."""
    cowork = tmp_path / "cowork-skills"
    target = cowork / "untracked.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Notes\n", encoding="utf-8")
    link = cowork / "alpha" / "SKILL.md"
    link.parent.mkdir()
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(cowork))
    claim = "cowork://skills/alpha/SKILL.md"
    evidence = DeploymentEvidence(
        deploy_root=tmp_path / "project",
        receipts={},
        render=_no_render,
        owners={claim: ("owner/pkg",)},
        kept=(),
        recorded_hashes={claim: frozenset({compute_file_hash(target)})},
    )
    evidence.consent_to_lockfile_hashes()

    assert evidence.consent_preview([claim]) == frozenset()
    result = remove_stale_deployed_files(
        [claim],
        tmp_path / "project",
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes={claim: compute_file_hash(target)},
        receipt_retirements=[],
        allow_final_symlink=True,
    )

    assert link.is_symlink() and result.deleted == []
    assert target.read_text(encoding="utf-8") == "# Notes\n"


@pytest.mark.req("req-lk-024")
def test_consent_never_unlinks_a_link_it_classified_as_kept(tmp_path: Path) -> None:
    """The final-symlink rule does not override consent's kept classification."""
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
    assert [claim for claim, _ in evidence.refusals([_PATH])] == [_PATH]

    result = remove_stale_deployed_files(
        [_PATH],
        tmp_path,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes={},
        receipt_retirements=[],
        allow_final_symlink=True,
    )

    assert link.is_symlink() and result.deleted == []
    assert target.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.req("req-lk-024")
def test_consent_finds_an_owner_recorded_under_a_backslash_spelling(tmp_path: Path) -> None:
    """An owner render that lacks the path keeps the file, whatever the lock spelling."""
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    render.mkdir()
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda owner: None if owner is None else render,
        owners={_PATH.replace("/", "\\"): ("owner/pkg",)},
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
def test_consent_pools_owners_across_path_spellings(tmp_path: Path) -> None:
    """Two spellings of one path keep every owner: an offline one never hides a render."""
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    render.mkdir()
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda owner: render if owner == "owner/pkg" else None,
        owners={_PATH.replace("/", "\\"): ("owner/pkg",), _PATH: ("offline/pkg",)},
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
def test_consent_checks_the_enclosing_directory_owner_too(tmp_path: Path) -> None:
    """A member claim's offline owner never hides its skill directory owner's render."""
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    render.mkdir()
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda owner: render if owner == "owner/pkg" else None,
        owners={_PATH: ("offline/pkg",), _PATH.rpartition("/")[0]: ("owner/pkg",)},
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("consent", [False, True])
def test_an_enclosing_owner_render_never_overrides_the_files_own_offline_owner(
    tmp_path: Path, consent: bool
) -> None:
    """Without the flag the verdict reads the file's own owner, as before #4295.

    Consent only widens the check for renders that would block it, so an
    enclosing directory owner's matching render never authorizes the delete.
    """
    root = tmp_path / "project"
    path = _deployed(root)
    render = tmp_path / "render"
    (render / _PATH).parent.mkdir(parents=True)
    (render / _PATH).write_bytes(path.read_bytes())
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda owner: render if owner == "owner/pkg" else None,
        owners={_PATH: ("offline/pkg",), _PATH.rpartition("/")[0]: ("owner/pkg",)},
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    if consent:
        evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_keeps_a_user_file_created_after_the_final_directory_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consent removes only an emptied skill directory: a late user file survives."""
    skill = ".agents/skills/alpha"
    first = f"{skill}/SKILL.md"
    late = tmp_path / skill / "mine.md"
    hashes, evidence = _skill_world(tmp_path, {first: "skill\n"})
    real_rglob = Path.rglob

    def scan_then_user_writes(self: Path, pattern: str, *args, **kwargs):
        yield from real_rglob(self, pattern, *args, **kwargs)
        if self == tmp_path / skill and not (tmp_path / first).exists() and not late.exists():
            late.write_text("user\n", encoding="utf-8")

    monkeypatch.setattr(Path, "rglob", scan_then_user_writes)

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

    assert late.read_text(encoding="utf-8") == "user\n"
    assert not (tmp_path / first).exists()
    assert skill not in result.deleted


@pytest.mark.req("req-lk-024")
def test_consent_reads_an_absolute_spelling_under_the_root_as_one_file(tmp_path: Path) -> None:
    """A conflicting hash recorded under the absolute user-scope spelling blocks consent."""
    root = tmp_path / "home"
    path = _deployed(root)
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=_no_render,
        kept=(),
        recorded_hashes={
            _PATH: frozenset({compute_file_hash(path)}),
            str(root / _PATH): frozenset({_OTHER}),
        },
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_finds_an_owner_recorded_under_an_absolute_spelling(tmp_path: Path) -> None:
    """An owner render lacking the file keeps it, whichever spelling names the owner."""
    root = tmp_path / "home"
    path = _deployed(root)
    render = tmp_path / "render"
    render.mkdir()
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=lambda owner: None if owner is None else render,
        owners={str(root / _PATH): ("owner/pkg",)},
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_reads_a_cowork_spelling_of_a_root_file_as_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Cowork root inside the deploy root: a conflicting hash under the URI blocks consent."""
    root = tmp_path / "project"
    claim = ".agents/skills/alpha/notes.md"
    path = root / claim
    path.parent.mkdir(parents=True)
    path.write_text("# Notes\n", encoding="utf-8")
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(root / ".agents/skills"))
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=_no_render,
        kept=(),
        recorded_hashes={
            claim: frozenset({compute_file_hash(path)}),
            "cowork://skills/alpha/notes.md": frozenset({_OTHER}),
        },
    )
    evidence.consent_to_lockfile_hashes()

    assert evidence.lockfile_consentable(claim, path) is False
    assert path.exists()


@pytest.mark.req("req-lk-024")
def test_consent_honours_a_receipt_recorded_under_another_spelling_of_the_file(
    tmp_path: Path,
) -> None:
    """A differing receipt under a case-variant spelling of the same file blocks consent."""
    root = tmp_path / "project"
    path = _deployed(root)
    variant = root / _PATH.replace("notes.", "NOTES.")
    if not variant.exists():
        # A case-sensitive filesystem models the alias with a hard link.
        os.link(path, variant)
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={variant.resolve().as_posix(): _OTHER},
        render=_no_render,
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)})},
    )
    evidence.consent_to_lockfile_hashes()

    assert evidence.lockfile_consentable(_PATH, path) is False
    assert path.exists()


@pytest.mark.req("req-lk-024")
def test_consent_reads_dot_segment_spellings_as_one_file(tmp_path: Path) -> None:
    """A conflicting hash recorded under a ``./`` alias still blocks consent."""
    root = tmp_path / "project"
    path = _deployed(root)
    head, _, name = _PATH.rpartition("/")
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=_no_render,
        kept=(),
        recorded_hashes={
            _PATH: frozenset({compute_file_hash(path)}),
            f"{head}/./{name}": frozenset({_OTHER}),
        },
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
def test_consent_tries_an_owner_only_the_canonical_rows_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment row's extra owner is rendered even when its legacy list omits the path."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    path = _deployed(tmp_path)
    digest = compute_file_hash(path)
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="owner/pkg", deployed_files=[_PATH], deployed_file_hashes={_PATH: digest}
        )
    )
    lock.add_dependency(LockedDependency(repo_url="owner/second"))
    first, second = lock.dependencies
    row = {"kind": "project-relative", "target": "copilot", "value": _PATH, "runtime": None}
    row |= {"scope": "project", "owners": [first, second], "active_owner": first}
    lock.deployment_ledger = DeploymentLedgerCodec.from_rows([{**row, "content_hash": digest}])

    def replay(config, _logger):
        rendered = LockFile.read(config.lockfile_path)
        if rendered is None or set(rendered.dependencies) != {second}:
            raise OSError("source unreachable")
        out = config.scratch_root / "second-render"
        out.mkdir()
        return out

    monkeypatch.setattr("apm_cli.install.drift.run_replay", replay)
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    evidence = DeploymentEvidence.collect(
        tmp_path,
        user_scope=False,
        lockfile=lock,
        lockfile_root=tmp_path,
        diagnostics=DiagnosticCollector(),
        kept=(),
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(tmp_path, evidence, path)

    assert path.exists() and result.deleted == []
    assert evidence.consented_deletions() == []


@pytest.mark.req("req-lk-024")
def test_consent_reads_case_variant_spellings_as_one_file(tmp_path: Path) -> None:
    """On a case-insensitive filesystem a conflicting hash under ``NOTES`` blocks ``notes``."""
    root = tmp_path / "project"
    path = _deployed(root)
    variant = _PATH.replace("notes.", "NOTES.")
    if not (root / variant).exists():
        pytest.skip("case-sensitive filesystem: the spellings name different files")
    evidence = DeploymentEvidence(
        deploy_root=root,
        receipts={},
        render=_no_render,
        kept=(),
        recorded_hashes={_PATH: frozenset({compute_file_hash(path)}), variant: frozenset({_OTHER})},
    )
    evidence.consent_to_lockfile_hashes()

    result, _messages = _cleanup(root, evidence, path)

    assert path.exists() and result.deleted == []


@pytest.mark.req("req-lk-024")
def test_consent_reports_one_deletion_per_file_across_spellings(tmp_path: Path) -> None:
    """``a`` and ``./a`` consented in one run report one deletion, not two."""
    path = _deployed(tmp_path)
    digest = compute_file_hash(path)
    evidence = DeploymentEvidence(
        deploy_root=tmp_path,
        receipts={},
        render=_no_render,
        kept=(),
        recorded_hashes={_PATH: frozenset({digest})},
    )
    evidence.consent_to_lockfile_hashes()
    head, _, name = _PATH.rpartition("/")
    for claim in (_PATH, f"{head}/./{name}"):
        evidence.verdict(claim, tmp_path / _PATH)
    path.unlink()

    assert not path.exists()
    assert evidence.take_consented_deletions() == [_PATH]


_HOOK = ".github/hooks/pkg-apm.json"


def _hook(root: Path) -> Path:
    path = root / _HOOK
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks": {}}\n', encoding="utf-8")
    return path


def _sync_hooks(root: Path, evidence: DeploymentEvidence) -> dict:
    from apm_cli.integration.hook_integrator import HookIntegrator

    return HookIntegrator().sync_integration(
        None, root, managed_files={_HOOK}, managed_file_hashes={}, evidence=evidence
    )


@pytest.mark.req("req-lk-024")
def test_consent_kept_dot_segment_hook_is_not_a_sync_error(tmp_path: Path) -> None:
    """A hook claim spelled with ``./`` that consent keeps is no failure (#4295)."""
    from apm_cli.integration.hook_integrator import HookIntegrator

    path = _hook(tmp_path)
    dotted = _HOOK.replace("hooks/", "hooks/./")
    evidence = DeploymentEvidence(deploy_root=tmp_path, receipts={}, render=_no_render, kept=())
    evidence.consent_to_lockfile_hashes()

    stats = HookIntegrator().sync_integration(
        None, tmp_path, managed_files={dotted}, managed_file_hashes={}, evidence=evidence
    )

    assert path.exists()
    assert stats["errors"] == 0


@pytest.mark.req("req-lk-024")
def test_uninstall_waives_a_consent_kept_dot_segment_claim(tmp_path: Path) -> None:
    """Retained ``./`` spellings match consent's canonical keys, so uninstall completes."""
    from types import SimpleNamespace

    from apm_cli.commands.uninstall.cli import _abort_if_retained_target_cleanup_paths

    lock_path = tmp_path / "apm.lock.yaml"
    outcome = SimpleNamespace(retained_paths={_PATH.replace("/notes", "/./notes")})
    logger = SimpleNamespace(render_summary=lambda: None, error=lambda *_a, **_k: None)

    _abort_if_retained_target_cleanup_paths(
        outcome, None, lock_path, logger, consent_kept=frozenset({_PATH})
    )

    assert outcome.retained_paths == set()
    assert not lock_path.exists()


@pytest.mark.req("req-lk-024")
def test_consent_kept_hook_is_not_reported_as_a_path_safety_failure(tmp_path: Path) -> None:
    path = _hook(tmp_path)
    evidence = DeploymentEvidence(deploy_root=tmp_path, receipts={}, render=_no_render, kept=())
    evidence.consent_to_lockfile_hashes()

    stats = _sync_hooks(tmp_path, evidence)

    assert path.exists()
    assert stats["errors"] == 0
    assert _HOOK not in stats.get("unsafe_paths", [])


@pytest.mark.req("req-lk-024")
@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs a POSIX directory that denies unlink",
)
def test_consent_never_hides_a_failed_deletion_of_a_hook_it_kept_earlier(tmp_path: Path) -> None:
    """A claim kept in preflight, then authorized and failing to unlink, is a failure."""
    path = _hook(tmp_path)
    original = path.read_bytes()
    receipts = {path.resolve().as_posix(): compute_file_hash(path)}
    evidence = DeploymentEvidence(
        deploy_root=tmp_path, receipts=receipts, render=_no_render, kept=()
    )
    evidence.consent_to_lockfile_hashes()
    path.write_text("edited\n", encoding="utf-8")
    assert [claim for claim, _ in evidence.refusals([_HOOK])] == [_HOOK]
    path.write_bytes(original)
    path.parent.chmod(0o555)
    try:
        stats = _sync_hooks(tmp_path, evidence)
    finally:
        path.parent.chmod(0o755)

    assert path.exists()
    assert stats["errors"] >= 1
    assert _HOOK in stats.get("failed_paths", [])


def _kept_then_relinked(tmp_path: Path) -> tuple[Path, Path, DeploymentEvidence]:
    """Keep a hook at preflight, then swap its parent for a symlink before cleanup."""
    root = tmp_path / "project"
    path = _hook(root)
    receipts = {path.resolve().as_posix(): compute_file_hash(path)}
    evidence = DeploymentEvidence(deploy_root=root, receipts=receipts, render=_no_render, kept=())
    evidence.consent_to_lockfile_hashes()
    path.write_text("edited\n", encoding="utf-8")
    assert [claim for claim, _ in evidence.refusals([_HOOK])] == [_HOOK]
    outside = tmp_path / "outside"
    path.parent.rename(outside)
    try:
        os.symlink(outside, path.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    return root, outside / path.name, evidence


def _remove_one(root: Path, claim: str, evidence: DeploymentEvidence, **kwargs):
    return remove_stale_deployed_files(
        [claim],
        root,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        recorded_hashes={},
        receipt_retirements=[],
        **kwargs,
    )


@pytest.mark.req("req-lk-024")
def test_consent_reports_a_safety_rejection_after_preflight_as_a_failure(tmp_path: Path) -> None:
    root, outside, evidence = _kept_then_relinked(tmp_path)

    stats = _sync_hooks(root, evidence)

    assert outside.read_text(encoding="utf-8") == "edited\n"
    assert stats["errors"] >= 1
    assert _HOOK in stats.get("failed_paths", [])


@pytest.mark.req("req-lk-024")
def test_selected_cleanup_never_releases_a_path_a_safety_gate_rejected(tmp_path: Path) -> None:
    """The rejection stops the uninstall; the lock still records the file (#4815)."""
    from apm_cli.commands.uninstall.cli import _abort_if_retained_target_cleanup_paths
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.selected_cleanup import SelectedCleanupOutcome

    root, outside, evidence = _kept_then_relinked(tmp_path)
    dependency = LockedDependency(repo_url="owner/pkg", deployed_files=[_HOOK])
    lock = LockFile()
    lock.add_dependency(dependency)
    cleanup = _remove_one(root, _HOOK, evidence)
    outcome = SelectedCleanupOutcome()
    SelectedCleanupOutcome.observer(outcome, InstallLogger(), "owner/pkg")(cleanup)
    outcome.record("owner/pkg", dependency, cleanup.retained, {}, survivors=set(), preserved=set())

    assert _HOOK in outcome.rejected_paths
    with pytest.raises(SystemExit) as stopped:
        _abort_if_retained_target_cleanup_paths(
            outcome, lock, root / "apm.lock.yaml", InstallLogger(), evidence=evidence
        )

    assert stopped.value.code == 1
    assert outside.read_text(encoding="utf-8") == "edited\n"
    written = LockFile.read(root / "apm.lock.yaml")
    assert written is not None
    assert _HOOK in written.dependencies["owner/pkg"].deployed_files


@pytest.mark.req("req-lk-024")
def test_consent_keeps_a_file_replaced_by_a_link_after_preflight(tmp_path: Path) -> None:
    """A file authorized at preflight, then swapped for a link, is kept as a link (#4815)."""
    path = _deployed(tmp_path)
    receipts = {path.resolve().as_posix(): compute_file_hash(path)}
    evidence = DeploymentEvidence(
        deploy_root=tmp_path, receipts=receipts, render=_no_render, kept=()
    )
    evidence.consent_to_lockfile_hashes()
    assert evidence.refusals([_PATH]) == []
    target = tmp_path / "elsewhere.md"
    target.write_text("outside\n", encoding="utf-8")
    path.unlink()
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    result = _remove_one(tmp_path, _PATH, evidence, allow_final_symlink=True)

    assert path.is_symlink() and result.deleted == []
    assert target.read_text(encoding="utf-8") == "outside\n"
    assert _PATH in evidence.kept_under_consent()


@pytest.mark.req("req-lk-024")
def test_consent_names_every_kept_member_of_a_skill_directory(tmp_path: Path) -> None:
    skill = ".agents/skills/alpha"
    members = {f"{skill}/m{index}.md": f"member {index}\n" for index in range(7)}
    hashes, evidence = _skill_world(tmp_path, members)
    for member in members:
        (tmp_path / member).write_text("my edit\n", encoding="utf-8")
    diagnostics = DiagnosticCollector()

    remove_stale_deployed_files(
        [skill],
        tmp_path,
        dep_key="owner/pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=hashes,
        receipt_retirements=[],
    )

    text = " ".join(d.message for d in diagnostics._diagnostics)
    assert all(member in text for member in members), text
    assert "more)" not in text, text


@pytest.mark.req("req-lk-024")
def test_selected_cleanup_keeps_an_unchanged_consent_kept_link_without_failing(
    tmp_path: Path,
) -> None:
    """A final link kept at preflight is retention, not a safety failure (#4815)."""
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.install.selected_cleanup import SelectedCleanupOutcome

    root = tmp_path / "project"
    target = tmp_path / "elsewhere.md"
    target.write_text("outside\n", encoding="utf-8")
    link = root / _PATH
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    evidence = DeploymentEvidence(deploy_root=root, receipts={}, render=_no_render, kept=())
    evidence.consent_to_lockfile_hashes()
    assert [claim for claim, _ in evidence.refusals([_PATH])] == [_PATH]
    cleanup = _remove_one(root, _PATH, evidence)
    outcome = SelectedCleanupOutcome()

    SelectedCleanupOutcome.observer(outcome, InstallLogger(), "owner/pkg")(cleanup)

    assert link.is_symlink() and target.read_text(encoding="utf-8") == "outside\n"
    assert outcome.failed_paths == set() and outcome.rejected_paths == {}
    assert cleanup.rejected == {}
