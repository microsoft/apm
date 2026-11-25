"""Unit tests for ``apm_cli.integration.cleanup.remove_stale_deployed_files``.

The helper is the single safety gate guarding APM's intra-package and
local-package stale-file deletion. These tests pin its invariants:

* path validation rejects unmanaged prefixes
* directory entries are refused (defeats poisoned-lockfile rmtree)
* bytes differing from the deployment receipt skip deletion (user-edited)
* a claim without deployment evidence is kept (#3663, #3701), even when
  the lockfile records a matching hash
* unlink failures are retained for retry on next install
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.command_logger import CommandLogger
from apm_cli.integration.cleanup import (
    CleanupResult,
    remove_stale_deployed_files,
)
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.utils.deployment_evidence import no_evidence, receipted_evidence


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


@pytest.fixture
def diagnostics():
    return DiagnosticCollector(verbose=False)


@pytest.fixture
def logger():
    return CommandLogger("install", verbose=False)


def _make_managed_file(project_root: Path, rel: str, content: str = "hi\n") -> Path:
    p = project_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _recorded(project_root: Path, *rels: str) -> dict[str, str]:
    return {rel: compute_file_hash(project_root / rel) for rel in rels}


def test_happy_path_deletes_under_known_prefix(project_root, diagnostics, logger):
    target = _make_managed_file(project_root, ".github/prompts/old.prompt.md")
    result = remove_stale_deployed_files(
        [".github/prompts/old.prompt.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
        recorded_hashes=_recorded(project_root, ".github/prompts/old.prompt.md"),
    )
    assert result.deleted == [".github/prompts/old.prompt.md"]
    assert not result.failed
    assert not result.skipped_unmanaged
    assert not target.exists()


def test_path_traversal_rejected(project_root, diagnostics, logger):
    """validate_deploy_path rejects '..' segments."""
    result = remove_stale_deployed_files(
        ["../escape.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=no_evidence(project_root),
    )
    assert result.deleted == []
    assert result.skipped_unmanaged == ["../escape.md"]


def test_unmanaged_prefix_rejected(project_root, diagnostics, logger):
    """A file outside any integration prefix is refused."""
    rel = "src/main.py"
    _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
    )
    assert result.deleted == []
    assert rel in result.skipped_unmanaged
    assert (project_root / rel).exists()


def test_directory_entry_refused(project_root, diagnostics, logger):
    """A lockfile entry that resolves to a directory is refused outright.

    This is the lockfile-poisoning blocker: an attacker writes
    '.github/instructions/' (a directory under a known prefix) into the
    lockfile and expects the next install to rmtree the user's whole
    instructions folder. APM only deploys individual files, so it must
    only delete individual files.
    """
    (project_root / ".github" / "instructions").mkdir(parents=True)
    (project_root / ".github" / "instructions" / "user.md").write_text(
        "user-authored",
        encoding="utf-8",
    )
    result = remove_stale_deployed_files(
        [".github/instructions"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
    )
    assert result.deleted == []
    assert ".github/instructions" in result.skipped_unmanaged
    # Subtree intact.
    assert (project_root / ".github" / "instructions" / "user.md").exists()
    # Diagnostic recorded so user knows.
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("Refused to remove directory entry" in m for m in msgs)


def test_missing_file_treated_as_already_clean(project_root, diagnostics, logger):
    result = remove_stale_deployed_files(
        [".github/prompts/gone.prompt.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=no_evidence(project_root),
    )
    assert result.deleted == []
    assert result.failed == []
    assert result.skipped_unmanaged == []  # missing != unmanaged


def test_hash_mismatch_skips_user_edited_file(project_root, diagnostics, logger):
    rel = ".github/prompts/edited.prompt.md"
    target = _make_managed_file(project_root, rel, "as deployed\n")
    recorded = _recorded(project_root, rel)
    evidence = receipted_evidence(project_root)
    # The user edits the file after APM deployed it.
    target.write_text("user has edited this\n", encoding="utf-8")
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=recorded,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert (project_root / rel).exists()
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("edited" in m.lower() for m in msgs)


def test_hash_match_deletes_file(project_root, diagnostics, logger):
    rel = ".github/prompts/match.prompt.md"
    target = _make_managed_file(project_root, rel, "untouched\n")
    recorded = {rel: compute_file_hash(target)}
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
        recorded_hashes=recorded,
    )
    assert result.deleted == [rel]
    assert not target.exists()


def test_claim_without_recorded_hash_is_kept(project_root, diagnostics, logger):
    """A lockfile row alone never authorizes deleting a file (#3663)."""
    rel = ".github/prompts/legacy.prompt.md"
    target = _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=no_evidence(project_root),
        recorded_hashes=None,
    )
    assert result.deleted == []
    assert result.skipped_unmanaged == [rel]
    assert rel in result.retained
    assert target.exists()
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any(f"Kept {rel}" in m and "no local deployment receipt" in m for m in msgs)


def test_matching_lockfile_hash_without_evidence_is_kept(project_root, diagnostics, logger):
    """A matching lockfile hash proves content, not deployment (#3701)."""
    rel = ".github/prompts/cloned.prompt.md"
    target = _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=no_evidence(project_root),
        recorded_hashes=_recorded(project_root, rel),
    )
    assert result.deleted == []
    assert result.skipped_unmanaged == [rel]
    assert target.exists()


def test_hash_read_failure_fails_closed(project_root, diagnostics, logger, monkeypatch):
    """Provenance gate must fail CLOSED on hash-read errors.

    Regression test for PR #762 review feedback: previously a
    ``compute_file_hash`` exception (e.g. ``PermissionError``) was
    swallowed and ``actual_hash`` became ``None``, allowing the file
    to be deleted even though a hash was recorded. With a hash
    recorded but unreadable the helper cannot prove the file is
    unmodified, so it must skip deletion.
    """
    rel = ".github/prompts/unreadable.prompt.md"
    target = _make_managed_file(project_root, rel, "could be edited, can't tell\n")
    recorded = {rel: "sha256:" + "1" * 64}
    evidence = receipted_evidence(project_root)

    def _boom(_path):
        raise PermissionError("simulated EACCES")

    # The helper imports compute_file_hash lazily inside the gate, so
    # patch the source module.
    monkeypatch.setattr("apm_cli.utils.content_hash.compute_file_hash", _boom)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=recorded,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert target.exists()
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("could not verify" in m.lower() for m in msgs), (
        "Expected fail-closed warning mentioning the verification failure."
    )


def test_unlink_failure_is_retained_for_retry(project_root, diagnostics, logger, monkeypatch):
    rel = ".github/prompts/cant-delete.prompt.md"
    _make_managed_file(project_root, rel)
    evidence = receipted_evidence(project_root)

    def _raise(*_a, **_kw):
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "unlink", _raise)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=_recorded(project_root, rel),
    )
    assert result.deleted == []
    assert result.failed == [rel]
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("retry on next" in m.lower() for m in msgs)


def test_orphan_failure_message_does_not_promise_retry(
    project_root, diagnostics, logger, monkeypatch
):
    """failed_path_retained=False rewords the failure diagnostic.

    Orphan cleanup runs against a package that is no longer in the
    manifest, so the lockfile entry is being dropped entirely and a
    failed deletion can't be retried by APM. The user must remove the
    file manually -- the diagnostic must say so instead of promising
    a retry that will never happen.
    """
    rel = ".github/prompts/orphan-cant-delete.prompt.md"
    _make_managed_file(project_root, rel)
    evidence = receipted_evidence(project_root)
    monkeypatch.setattr(
        Path, "unlink", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("nope"))
    )
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="some/orphan-pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=_recorded(project_root, rel),
        failed_path_retained=False,
    )
    assert result.failed == [rel]
    msgs = [d.message for d in diagnostics._diagnostics]
    assert not any("will retry" in m.lower() for m in msgs)
    assert any("delete the file manually" in m.lower() for m in msgs)


def test_orphan_path_honours_hash_gate(project_root, diagnostics, logger):
    """Orphan cleanup must skip user-edited files just like stale cleanup.

    Regression guard for the security review of the #666 follow-up:
    earlier the orphan path bypassed the helper entirely and would have
    silently deleted a file the user edited after APM deployed it.
    """
    rel = ".github/prompts/edited-orphan.prompt.md"
    target = _make_managed_file(project_root, rel, "as deployed\n")
    recorded = _recorded(project_root, rel)
    evidence = receipted_evidence(project_root)
    target.write_text("user has edited this\n", encoding="utf-8")
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="orphan-pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence,
        recorded_hashes=recorded,
        failed_path_retained=False,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert target.exists()


def test_helper_signature_does_not_accept_logger():
    """Logger kwarg was dropped -- helper output goes through diagnostics
    plus caller-side InstallLogger methods (cleanup_skipped_user_edit /
    stale_cleanup / orphan_cleanup). Pin the SoC."""
    import inspect

    sig = inspect.signature(remove_stale_deployed_files)
    assert "logger" not in sig.parameters


def test_orphan_loop_uses_manifest_intent_not_integration_outcome():
    """Regression guard: the orphan-cleanup loop in install.py must derive
    'still-declared' from intended_dep_keys (manifest intent), NOT from
    package_deployed_files (integration outcome).

    Bug reproduced if the membership test reads package_deployed_files: a
    transient integration failure for a still-declared package leaves its
    key absent from package_deployed_files; the orphan loop then deletes
    that package's previously deployed files even though the package is
    still in apm.yml. Detected by the security re-review on commit 4b64c27.
    """
    import inspect

    from apm_cli.install.phases import cleanup as cleanup_mod

    # The orphan block asks the owner with the intent _run_cleanup builds.
    owner_call = inspect.getsource(cleanup_mod._orphan_decision)
    assert "intended=desired_owners" in owner_call
    src = inspect.getsource(cleanup_mod._run_cleanup)
    code_only = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    # Must consult manifest intent.
    assert "frozenset(intended_dep_keys" in code_only, (
        "Orphan loop must use intended_dep_keys (manifest intent). "
        "Using package_deployed_files.keys() (integration outcome) re-introduces "
        "silent deletion of files for still-declared packages on transient errors."
    )
    # Must NOT regress to the outcome set.
    assert "package_deployed_files.keys()" not in code_only, (
        "Orphan loop must not derive membership from package_deployed_files.keys() -- "
        "see test_orphan_loop_uses_manifest_intent_not_integration_outcome docstring."
    )


def test_hash_deployed_is_module_level_and_works(tmp_path):
    """Regression test for PR #762 review feedback.

    Previously ``_hash_deployed`` was an inner closure of
    ``_install_apm_dependencies`` but was *referenced* from
    ``_integrate_local_content`` (a sibling module-level function),
    which would raise ``NameError`` at runtime whenever the local
    ``.apm/`` persist path executed. Promoted to module scope so both
    call sites share one implementation. This test pins:

    1. The helper is module-importable (no NameError at import time).
    2. It accepts the new ``(rel_paths, project_root)`` signature.
    3. It returns ``{rel: "sha256:<hex>"}`` for regular files and
       silently omits symlinks / missing paths.
    """
    from apm_cli.commands.install import _hash_deployed

    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "missing.txt")  # never created
    out = _hash_deployed(["a.txt", "missing.txt"], tmp_path)
    assert "a.txt" in out
    assert out["a.txt"].startswith("sha256:")
    assert "missing.txt" not in out
    # Empty input is safe.
    assert _hash_deployed([], tmp_path) == {}
    assert _hash_deployed(None, tmp_path) == {}


def test_result_dataclass_defaults():
    r = CleanupResult()
    assert r.deleted == []
    assert r.failed == []
    assert r.skipped_user_edit == []
    assert r.skipped_unmanaged == []
    assert r.deleted_targets == []


# ---------------------------------------------------------------------------
# Cowork cleanup tests (PR #926 -- remove_stale_deployed_files)
# ---------------------------------------------------------------------------


def test_cowork_stale_entry_deletes_real_file(tmp_path, diagnostics):
    """Happy path: cowork:// stale entry with a real file in a temp cowork
    root -> file deleted, lockfile entry removed (in result.deleted)."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()
    skill_md = cowork_root / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# My Skill\n", encoding="ascii")
    assert skill_md.exists()

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=cowork_root,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=receipted_evidence(cowork_root, deploy_root=project_root),
        )

    assert not skill_md.exists(), "File should have been deleted"
    assert "cowork://skills/my-skill/SKILL.md" in result.deleted
    assert not result.failed
    assert not result.skipped_unmanaged


def test_cowork_stale_entry_resolver_returns_none(tmp_path, diagnostics):
    """Resolver returns None -> file NOT deleted, lockfile entry retained
    in result.failed, one-time warning emitted."""
    from unittest.mock import patch

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=None,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=no_evidence(project_root),
        )

    assert result.deleted == []
    assert "cowork://skills/my-skill/SKILL.md" in result.failed
    # One-time warning about missing OneDrive path.
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("OneDrive path not detected" in m for m in msgs)
    assert any("APM_COPILOT_COWORK_SKILLS_DIR" in m for m in msgs)


def test_cowork_stale_entry_file_already_gone(tmp_path, diagnostics):
    """Cowork root resolves but file is already missing -> lockfile entry
    is removed (not in failed), no error. Idempotent cleanup."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()
    # Do NOT create the skill file -- it is already gone.

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=cowork_root,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=no_evidence(project_root),
        )

    # Not in deleted (nothing was on disk), not in failed (no error),
    # not in skipped_unmanaged (path was valid).
    assert result.deleted == []
    assert result.failed == []
    assert result.skipped_unmanaged == []


def test_cowork_stale_entry_from_lockfile_error_retains_in_failed(tmp_path, diagnostics):
    """from_lockfile_path raises after validation passes -> entry
    retained in result.failed, warning emitted."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Use a valid-looking cowork path so it passes validate_deploy_path.
    stale = "cowork://skills/bad-skill/SKILL.md"

    def _boom(_path, _root):
        raise ValueError("simulated resolution failure")

    with (
        patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ),
        patch(
            "apm_cli.integration.copilot_cowork_paths.from_lockfile_path",
            side_effect=_boom,
        ),
    ):
        result = remove_stale_deployed_files(
            [stale],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=no_evidence(project_root),
        )

    assert result.deleted == []
    assert stale in result.failed
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("failed path resolution" in m for m in msgs)


def _cowork_link(tmp_path, name: str, target: Path, *, linked_ancestor: bool = False):
    """A cowork:// claim whose file is a link to *target*; optionally under a linked dir."""
    cowork = tmp_path / "cowork-skills"
    skill = cowork / "alpha"
    if linked_ancestor:
        real = tmp_path / "cowork-skills" / "real-alpha"
        real.mkdir(parents=True)
        try:
            skill.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are unavailable")
    else:
        skill.mkdir(parents=True)
    try:
        (skill / name).symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    return cowork, f"cowork://skills/alpha/{name}"


@pytest.mark.parametrize("linked_ancestor", [False, True], ids=["dangling", "linked-ancestor"])
def test_cowork_outward_final_link_stays_retained_as_a_failure(
    tmp_path, diagnostics, monkeypatch, linked_ancestor
):
    """Only the ancestor-checked gate allows an outward final link (#5022); cleanup's own
    decode keeps full containment, so the entry and its lock row are retained."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.md"
    if linked_ancestor:
        outside.write_text("outside\n", encoding="utf-8")
    cowork, stale = _cowork_link(tmp_path, "notes.md", outside, linked_ancestor=linked_ancestor)
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(cowork))

    result = remove_stale_deployed_files(
        [stale],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=no_evidence(project_root),
    )

    assert stale in result.failed and result.deleted == [], result
    assert (cowork / "alpha" / "notes.md").is_symlink()


def test_case_only_rename_keeps_live_file_and_adopts_its_spelling(project_root, diagnostics):
    """A stale spelling that is the live file's directory entry is renamed, never deleted."""
    rules = project_root / ".github" / "instructions"
    rules.mkdir(parents=True)
    old = rules / "Guide.instructions.md"
    old.write_text("v1")
    recorded = {".github/instructions/Guide.instructions.md": compute_file_hash(old)}
    case_insensitive = (rules / "guide.instructions.md").exists()
    if case_insensitive:
        (rules / "guide.instructions.md").write_text("v2")  # writer keeps old spelling

    result = remove_stale_deployed_files(
        [".github/instructions/Guide.instructions.md"],
        project_root,
        dep_key="acme/pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
        recorded_hashes=recorded,
        live_paths=[".github/instructions/guide.instructions.md"],
    )

    if case_insensitive:
        assert result.aliased == [".github/instructions/Guide.instructions.md"]
        assert result.deleted == [] and result.retained == []
        assert sorted(p.name for p in rules.iterdir()) == ["guide.instructions.md"]
        assert (rules / "guide.instructions.md").read_text() == "v2"
    else:
        assert result.deleted == [".github/instructions/Guide.instructions.md"]
        assert result.aliased == []


def test_alias_index_is_built_once_per_cleanup(project_root, diagnostics, monkeypatch):
    """Existing stale files cost O(stale + live) spelling keys, not O(stale x live)."""
    from apm_cli.utils import paths

    rules = project_root / ".github" / "instructions"
    rules.mkdir(parents=True)
    stale = []
    for i in range(30):
        (rules / f"old{i}.instructions.md").write_text("x")
        stale.append(f".github/instructions/old{i}.instructions.md")
    live = [f".github/instructions/new{i}.instructions.md" for i in range(300)]
    recorded = {rel: compute_file_hash(project_root / rel) for rel in stale}
    calls = {"key": 0}
    real_key = paths._spelling_key
    monkeypatch.setattr(
        paths, "_spelling_key", lambda p: calls.__setitem__("key", calls["key"] + 1) or real_key(p)
    )

    result = remove_stale_deployed_files(
        stale,
        project_root,
        dep_key="acme/pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=receipted_evidence(project_root),
        recorded_hashes=recorded,
        live_paths=live,
    )

    assert sorted(result.deleted) == sorted(stale)
    assert calls["key"] <= len(stale) + len(live)


@pytest.mark.parametrize("user_scope", [False, True])
def test_synced_removal_retires_receipts_in_the_evidence_scope(
    project_root, monkeypatch, user_scope
):
    """User-scope syncs retire receipts in the user store, not the project one (#3701)."""
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.integration.cleanup import DeploymentEvidence, remove_synced_files

    rel = ".github/prompts/old.prompt.md"
    target = _make_managed_file(project_root, rel, "deployed\n")
    retired: list[bool] = []
    monkeypatch.setattr(
        DeploymentMaterializationSession,
        "retire_removed",
        classmethod(lambda cls, root, deleted, *, user_scope: retired.append(user_scope)),
    )
    evidence = DeploymentEvidence(
        deploy_root=project_root,
        receipts={target.resolve().as_posix(): compute_file_hash(target)},
        render=None,
        user_scope=user_scope,
        kept=(),
    )

    result = remove_synced_files(
        {rel},
        project_root,
        prefixes=(".github/prompts/",),
        dep_key="pkg",
        targets=None,
        evidence=evidence,
    )

    assert result.deleted == [rel]
    assert retired == [user_scope]


@pytest.mark.parametrize("stale", ["member", "directory"])
def test_member_this_install_kept_is_retained_despite_evidence(
    project_root, diagnostics, stale: str
) -> None:
    """#4230: a declared member the integrator left unwritten is never cleaned as stale."""
    from apm_cli.integration.cleanup import DeploymentEvidence

    member = ".agents/skills/x/references/a.md"
    target = _make_managed_file(project_root, member, "reference a\n")
    _make_managed_file(project_root, ".agents/skills/x/SKILL.md", "x\n")
    receipts = {target.resolve().as_posix(): compute_file_hash(target)}
    paths = [member] if stale == "member" else [".agents/skills/x/", member]

    def evidence(kept: tuple[str, ...]) -> DeploymentEvidence:
        return DeploymentEvidence(
            deploy_root=project_root, receipts=receipts, render=None, kept=kept
        )

    result = remove_stale_deployed_files(
        paths,
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence((member,)),
    )
    assert result.deleted == []
    assert sorted(result.retained) == sorted(paths)
    assert target.read_text(encoding="utf-8") == "reference a\n"

    control = remove_stale_deployed_files(
        [member],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        evidence=evidence(()),
    )
    assert control.deleted == [member]


def test_refusals_name_a_symlinked_member_of_a_claimed_directory(project_root):
    """Cleanup keeps a directory holding a symlink, so uninstall refuses first (#4451).

    A managed final symlink outside any claimed directory keeps its own
    unlink rule and is not refused.
    """
    from apm_cli.integration.cleanup import SYMLINKED

    skill = project_root / ".agents" / "skills" / "kit"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(project_root / "apm.yml")
    final = project_root / ".github" / "agents" / "linked.agent.md"
    final.parent.mkdir(parents=True)
    final.symlink_to(project_root / "apm.yml")
    (project_root / "apm.yml").write_text("name: p\n", encoding="utf-8")
    evidence = receipted_evidence(project_root)

    refused = evidence.refusals(
        [
            ".agents/skills/kit",
            ".agents/skills/kit/SKILL.md",
            ".github/agents/linked.agent.md",
        ]
    )

    assert refused == [(".agents/skills/kit/SKILL.md", SYMLINKED)]
    assert (skill / "SKILL.md").is_symlink() and final.is_symlink()


@pytest.mark.parametrize(
    ("recorded", "receipts_readable", "consentable"),
    [
        ("match", True, True),
        ("conflict", True, False),
        ("none", True, False),
        ("match", False, False),
    ],
)
def test_trust_lockfile_consent_needs_one_matching_recorded_hash(
    tmp_path: Path, recorded: str, receipts_readable: bool, consentable: bool
) -> None:
    """#4295: consent fills missing evidence only from a single matching lock hash."""
    from apm_cli.integration.cleanup import AUTHORIZED, UNPROVEN, DeploymentEvidence

    rel = ".github/instructions/a.instructions.md"
    target = _make_managed_file(tmp_path, rel)
    hashes = {
        "match": frozenset({compute_file_hash(target)}),
        "conflict": frozenset({compute_file_hash(target), "sha256:" + "0" * 64}),
        "none": frozenset(),
    }[recorded]
    evidence = DeploymentEvidence(
        deploy_root=tmp_path,
        receipts={},
        render=lambda _owner: None,
        owners={rel: ("pkg",)},
        kept=(),
        recorded_hashes={rel: hashes},
        receipts_readable=receipts_readable,
    )

    assert evidence.verdict(rel, target) == UNPROVEN
    evidence.consent_to_lockfile_hashes()

    assert (evidence.verdict(rel, target) == AUTHORIZED) is consentable
    target.write_text("edited after the decision\n", encoding="utf-8")
    assert evidence.verdict(rel, target) == UNPROVEN


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_safety_rejection_of_a_consent_kept_file_is_always_a_failure(
    tmp_path: Path, diagnostics
) -> None:
    """#4815: consent kept it earlier, but a current path-safety rejection still fails."""
    from apm_cli.integration.cleanup import DeploymentEvidence, consent_outcome

    rel = ".github/hooks/kept.json"
    target = _make_managed_file(tmp_path, rel)
    evidence = DeploymentEvidence(
        deploy_root=tmp_path,
        receipts={},
        render=lambda _owner: None,
        owners={rel: ("pkg",)},
        kept=(),
        recorded_hashes={rel: frozenset({"sha256:" + "0" * 64})},
    )
    evidence.consent_to_lockfile_hashes()
    assert evidence.refusals([rel]) and evidence.kept_under_consent() == frozenset({rel})
    outside = tmp_path / "outside-hooks"
    target.parent.rename(outside)
    target.parent.symlink_to(outside, target_is_directory=True)

    result = remove_stale_deployed_files(
        [rel], tmp_path, dep_key="pkg", targets=None, diagnostics=diagnostics, evidence=evidence
    )
    failures, unsafe = consent_outcome(result, evidence)

    assert rel in result.rejected and rel in failures and rel in unsafe
    assert (outside / "kept.json").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
@pytest.mark.parametrize("consent", [True, False], ids=["trust-lockfile", "plain"])
def test_hook_sync_unlinks_agrees_with_the_hook_sync_under_consent(
    tmp_path: Path, consent: bool
) -> None:
    """#5139: the read-only hook link decision keeps what the consented hook sync keeps."""
    from apm_cli.integration.cleanup import (
        DeploymentEvidence,
        hook_sync_unlinks,
        managed_hook_prefixes,
        remove_synced_files,
    )
    from apm_cli.integration.targets import KNOWN_TARGETS

    rel = ".github/hooks/pkg-hooks.json"
    real = _make_managed_file(tmp_path, "elsewhere/hooks.json")
    link = tmp_path / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    evidence = DeploymentEvidence(
        deploy_root=tmp_path, receipts={}, render=lambda _owner: None, kept=()
    )
    if consent:
        evidence.consent_to_lockfile_hashes()

    predicted = hook_sync_unlinks(rel, tmp_path, recorded_hash=None, lockfile_consent=consent)
    result = remove_synced_files(
        {rel},
        tmp_path,
        prefixes=managed_hook_prefixes(),
        dep_key="<uninstall hooks>",
        targets=list(KNOWN_TARGETS.values()),
        evidence=evidence,
        recorded_hashes={},
        allow_final_symlink=True,
    )

    assert predicted is (rel in result.deleted)
    assert predicted is not consent
    assert link.is_symlink() is consent


def test_file_rewritten_during_sharing_retry_is_kept(project_root, diagnostics, logger):
    """A held file saved with new bytes while the retry waits is not deleted (#5441)."""
    rel = ".github/prompts/held.prompt.md"
    target = _make_managed_file(project_root, rel, "as deployed\n")
    recorded = _recorded(project_root, rel)
    calls = []

    def held_then_edited(path, *_args, **_kwargs):
        if path != target:
            return None
        calls.append(path)
        path.write_text("user saved while held\n", encoding="utf-8")
        exc = OSError(13, "in use", str(path))
        exc.winerror = 32
        raise exc

    with (
        patch("sys.platform", "win32"),
        patch("time.sleep"),
        patch.object(Path, "unlink", autospec=True, side_effect=held_then_edited),
    ):
        result = remove_stale_deployed_files(
            [rel],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=receipted_evidence(project_root),
            recorded_hashes=recorded,
        )

    assert len(calls) == 1
    assert result.deleted == []
    assert result.failed == []
    assert target.read_text(encoding="utf-8") == "user saved while held\n"


def _held_once_then_real(real_unlink, held_path):
    """A Path.unlink double: *held_path* is held (winerror 32) once, then removed."""
    calls = []

    def unlink(path, *args, **kwargs):
        if path != held_path:
            return real_unlink(path, *args, **kwargs)
        calls.append(path)
        if len(calls) == 1:
            exc = OSError(13, "in use", str(path))
            exc.winerror = 32
            raise exc
        return real_unlink(path, *args, **kwargs)

    return unlink, calls


def test_cowork_target_held_once_then_released_is_removed(tmp_path, diagnostics):
    """A managed-root target outside the project gets the retry too (#5441)."""
    cowork_root = tmp_path / "cowork-skills"
    skill_md = cowork_root / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# My Skill\n", encoding="ascii")
    project_root = tmp_path / "project"
    project_root.mkdir()
    unlink, calls = _held_once_then_real(Path.unlink, skill_md)

    with (
        patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ),
        patch("sys.platform", "win32"),
        patch("time.sleep"),
        patch.object(Path, "unlink", autospec=True, side_effect=unlink),
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
            evidence=receipted_evidence(cowork_root, deploy_root=project_root),
        )

    assert len(calls) == 2
    assert not skill_md.exists()
    assert result.deleted == ["cowork://skills/my-skill/SKILL.md"]
