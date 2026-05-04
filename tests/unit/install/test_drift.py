"""Unit tests for the drift-detection replay engine.

Covers:
* Normalization helpers (build-id strip, line endings, BOM).
* Public dataclass immutability contracts.
* Diff engine kinds (modified, unintegrated, orphaned, ignored).
* Inline-diff size cap.
* SARIF rule ID prefix.
* CheckLogger phase markers go to stderr.
"""

from __future__ import annotations

import dataclasses

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import (
    CheckLogger,
    DriftFinding,
    ReplayConfig,
    _normalize_line_endings,
    _strip_bom,
    _strip_build_id,
    diff_scratch_against_project,
    render_drift_sarif,
)

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


def test_replay_config_is_frozen(tmp_path):
    cfg = ReplayConfig(
        project_root=tmp_path,
        lockfile_path=tmp_path / "apm.lock.yaml",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.cache_only = False  # type: ignore[misc]


def test_drift_finding_is_frozen():
    f = DriftFinding(path=".apm/x.md", kind="modified")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.kind = "orphaned"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------


def _empty_lockfile() -> LockFile:
    return LockFile()


def _lockfile_with_tracked(paths: list[str]) -> LockFile:
    lock = LockFile()
    dep = LockedDependency(repo_url="example/pkg", deployed_files=list(paths))
    lock.add_dependency(dep)
    return lock


def _write(path, content: bytes = b"hello\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_diff_engine_modified_kind(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(scratch / ".apm" / "skills" / "x.md", b"new content\n")
    _write(project / ".apm" / "skills" / "x.md", b"old content\n")

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "modified"
    assert findings[0].path == ".apm/skills/x.md"


def test_diff_engine_modified_ignored_after_normalization(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(scratch / ".apm" / "skills" / "x.md", b"line1\nline2\n")
    # Same logical content but CRLF + BOM + spurious build id header.
    _write(
        project / ".apm" / "skills" / "x.md",
        b"\xef\xbb\xbf<!-- Build ID: deadbeef -->\r\nline1\r\nline2\r\n",
    )

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert findings == []


def test_diff_engine_unintegrated_kind(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(scratch / ".apm" / "skills" / "missing.md", b"x\n")
    project.mkdir()

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "unintegrated"
    assert findings[0].path == ".apm/skills/missing.md"


def test_diff_engine_orphaned_kind(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    scratch.mkdir()
    _write(project / ".apm" / "skills" / "old.md", b"stale\n")

    lock = _lockfile_with_tracked([".apm/skills/old.md"])

    findings = diff_scratch_against_project(scratch, project, lock, targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "orphaned"
    assert findings[0].path == ".apm/skills/old.md"


def test_diff_engine_ignores_untracked_governed_file(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    scratch.mkdir()
    # User-authored extra file in a governed dir, NOT tracked in lockfile.
    _write(project / ".apm" / "skills" / "user-wrote-this.md", b"hand-edited\n")

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert findings == []


def test_diff_engine_100kb_inline_cap(tmp_path):
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    big_a = b"a" * (200 * 1024)
    big_b = b"b" * (200 * 1024)
    _write(scratch / ".apm" / "skills" / "huge.md", big_a)
    _write(project / ".apm" / "skills" / "huge.md", big_b)

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "modified"
    assert "too large for inline diff" in findings[0].inline_diff


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_sarif_rule_id_prefix():
    findings = [
        DriftFinding(path="a.md", kind="modified", package="pkg-a"),
        DriftFinding(path="b.md", kind="orphaned", package="pkg-b"),
    ]
    results = render_drift_sarif(findings)
    assert results[0]["ruleId"] == "apm/drift/modified"
    assert results[1]["ruleId"] == "apm/drift/orphaned"
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
