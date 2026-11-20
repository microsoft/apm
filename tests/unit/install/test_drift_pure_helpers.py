"""Drift helpers that need no project tree."""

from __future__ import annotations

import dataclasses

import pytest

from apm_cli.install.drift import (
    CheckLogger,
    DriftFinding,
    _governed_root_dirs,
    _normalize_line_endings,
    _strip_bom,
    _strip_build_id,
    render_drift_sarif,
    render_drift_text,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-drift-claim-membership")]

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def test_strip_build_id_removes_header_preserves_rest():
    src = b"# Title\n<!-- Build ID: abc123def456 -->\nbody line\n<!-- Build ID: 999 -->trailing\n"
    out = _strip_build_id(src)
    assert b"Build ID" not in out
    assert b"# Title\n" in out
    assert b"body line\n" in out
    assert b"trailing\n" in out


def test_normalize_line_endings_crlf_to_lf():
    assert _normalize_line_endings(b"a\r\nb\r\nc") == b"a\nb\nc"
    assert _normalize_line_endings(b"no-crlf") == b"no-crlf"


def test_strip_bom_at_start_only():
    assert _strip_bom(b"\xef\xbb\xbfhello") == b"hello"
    # BOM mid-stream must not be removed (not a real BOM there).
    mid = b"x\xef\xbb\xbfy"
    assert _strip_bom(mid) == mid


# ---------------------------------------------------------------------------
# Dataclass contracts
# ---------------------------------------------------------------------------


def test_drift_finding_is_frozen():
    f = DriftFinding(path=".apm/x.md", kind="modified")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.kind = "orphaned"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------


# Inverse-normalization regression suite: each guard MUST NOT mask a
# real content change that happens to coexist with a
# legitimately-ignorable difference. Without these tests, a future
# refactor that over-broadens normalization (e.g. stripping all HTML
# comments instead of just Build-ID, or normalizing all whitespace)
# would silently hide real drift.


def test_render_text_lists_unrecorded_group_and_lockfile_remedy():
    out = render_drift_text([DriftFinding(path=".claude/skills/x/SKILL.md", kind="unrecorded")])
    assert "[x] Drift detected: 1 file(s)" in out
    assert "unrecorded (1):" in out
    assert ".claude/skills/x/SKILL.md" in out
    assert "Run 'apm install' to re-sync deployed files" in out
    assert "Commit the regenerated apm.lock.yaml" in out


# ---------------------------------------------------------------------------
# Governed roots: per-primitive deploy_root walking (issue #1716)
# ---------------------------------------------------------------------------


def _target_with_skill_deploy_root(root_dir: str = ".github", deploy_root: str = ".agents"):
    """Use a real target profile for governed-root and collision routing."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    target = KNOWN_TARGETS["copilot"]
    return dataclasses.replace(
        target,
        root_dir=root_dir,
        primitives={
            "skills": dataclasses.replace(target.primitives["skills"], deploy_root=deploy_root)
        },
    )


def test_governed_root_dirs_includes_primitive_deploy_root():
    """The differ must walk per-primitive deploy roots (copilot skills ->
    .agents), not only the target's top-level root_dir -- otherwise committed
    .agents/skills/ content escapes drift detection (issue #1716)."""
    roots = _governed_root_dirs([_target_with_skill_deploy_root()])
    assert ".apm" in roots
    assert ".github" in roots
    assert ".agents" in roots


def test_governed_root_dirs_collapses_nested_deploy_root_to_first_segment():
    roots = _governed_root_dirs([_target_with_skill_deploy_root(deploy_root=".agents/nested")])
    assert ".agents" in roots
    assert ".agents/nested" not in roots


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_sarif_rule_id_prefix():
    findings = [
        DriftFinding(path="a.md", kind="modified", package="pkg-a"),
        DriftFinding(path="b.md", kind="orphaned", package="pkg-b"),
        DriftFinding(path="c.md", kind="unrecorded", package="pkg-c"),
    ]
    results = render_drift_sarif(findings)
    assert results[0]["ruleId"] == "apm/drift/modified"
    assert results[1]["ruleId"] == "apm/drift/orphaned"
    assert results[2]["ruleId"] == "apm/drift/unrecorded"
    assert results[2]["level"] == "error"
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "a.md"
    assert results[1]["properties"]["package"] == "pkg-b"


# ---------------------------------------------------------------------------
# CheckLogger -- stderr only
# ---------------------------------------------------------------------------


def test_check_logger_phases_to_stderr(capsys):
    logger = CheckLogger(verbose=False)
    logger.replay_start()
    logger.replay_complete(3)
    logger.diff_start()
    logger.findings(2)
    logger.clean()

    captured = capsys.readouterr()
    # Everything must be on stderr to keep stdout JSON-clean.
    assert captured.out == ""
    assert "Replaying install" in captured.err
    assert "Replayed 3 package(s)" in captured.err
    assert "Diffing scratch" in captured.err
    assert "Drift detected: 2 file(s)" in captured.err
    assert "No drift detected" in captured.err
    # ASCII-only status symbols.
    assert "[>]" in captured.err
    assert "[+]" in captured.err
    assert "[!]" in captured.err


# ---------------------------------------------------------------------------
# Supply-chain fail-closed: unpinned remote dep
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Defense-in-depth: _ReadOnlyProjectGuard wired into run_replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["modified", "unrecorded", "unmanaged-conflict", "orphaned"])
def test_render_text_advisory_drift_never_prints_error_header(kind):
    """Advisory drift does not fail the run, so it never renders [x] (#4191)."""
    finding = DriftFinding(path=".claude/skills/x/SKILL.md", kind=kind)
    out = render_drift_text([finding], advisory=True)
    assert out.splitlines()[0] == "[!] Drift detected: 1 file(s)"
    assert "[x]" not in out
