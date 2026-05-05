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


# Inverse-normalization regression suite: each guard MUST NOT mask a
# real content change that happens to coexist with a
# legitimately-ignorable difference. Without these tests, a future
# refactor that over-broadens normalization (e.g. stripping all HTML
# comments instead of just Build-ID, or normalizing all whitespace)
# would silently hide real drift.


def test_normalization_does_not_mask_real_drift_under_build_id(tmp_path):
    """A real text change MUST be reported even when one side has a Build-ID header."""
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(
        scratch / ".apm" / "skills" / "x.md",
        b"<!-- Build ID: aaaa -->\nallowlisted line\n",
    )
    # Project: different Build-ID (ignorable) AND a real content change
    # ("BLOCKED line" instead of "allowlisted line").
    _write(
        project / ".apm" / "skills" / "x.md",
        b"<!-- Build ID: bbbb -->\nBLOCKED line\n",
    )

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "modified"
    assert findings[0].path == ".apm/skills/x.md"


def test_normalization_does_not_mask_real_drift_under_bom(tmp_path):
    """BOM stripping MUST NOT mask a real change in the rest of the file."""
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(scratch / ".apm" / "skills" / "x.md", b"hello world\n")
    # Project starts with a BOM (ignorable) but has different body content.
    _write(project / ".apm" / "skills" / "x.md", b"\xef\xbb\xbfhello WORLD\n")

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "modified"


def test_normalization_does_not_mask_real_drift_under_crlf(tmp_path):
    """CRLF normalization MUST NOT mask a real character change."""
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"
    _write(scratch / ".apm" / "skills" / "x.md", b"alpha\nbeta\ngamma\n")
    # Project uses CRLF (ignorable) AND has a different middle line.
    _write(
        project / ".apm" / "skills" / "x.md",
        b"alpha\r\nBETA-changed\r\ngamma\r\n",
    )

    findings = diff_scratch_against_project(scratch, project, _empty_lockfile(), targets=[])
    assert len(findings) == 1
    assert findings[0].kind == "modified"


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


def test_check_logger_scratch_root_emits_to_stderr_when_verbose(capsys, tmp_path):
    """Verbose-mode scratch root announcement stays on stderr (B4 follow-up).

    Stdout must remain a clean JSON/SARIF channel. The scratch tmpdir
    is diagnostic noise, not report payload, so it goes to stderr and
    is gated on ``verbose`` so the normal-mode user never sees it.
    """
    logger = CheckLogger(verbose=True)
    logger.scratch_root(tmp_path / "drift-scratch-xyz")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "drift scratch root:" in captured.err
    assert "drift-scratch-xyz" in captured.err
    assert "[i]" in captured.err


def test_check_logger_scratch_root_silent_when_not_verbose(capsys, tmp_path):
    """Non-verbose mode must NOT leak the scratch tmpdir."""
    logger = CheckLogger(verbose=False)
    logger.scratch_root(tmp_path / "drift-scratch-secret")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "drift-scratch-secret" not in captured.err
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Supply-chain fail-closed: unpinned remote dep
# ---------------------------------------------------------------------------


def test_materialize_unpinned_remote_dep_raises_cache_miss(tmp_path):
    """Remote dep with empty resolved_commit MUST fail-closed at audit.

    The cache could contain content from any commit on the referenced
    branch. Drift cannot prove freshness without a marker, and a marker
    cannot be written without a commit. Per supply-chain panel feedback
    (PR #1137), refuse to replay rather than silently trust the cache.
    """
    from apm_cli.install.drift import CacheMissError, _materialize_install_path

    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()
    dep = LockedDependency(
        repo_url="org/unpinned",
        host="github.com",
        resolved_commit=None,
    )

    with pytest.raises(CacheMissError) as exc_info:
        _materialize_install_path(dep, tmp_path, apm_modules, cache_only=True)

    msg = str(exc_info.value)
    assert "org/unpinned" in msg
    assert "no resolved_commit" in msg
    assert "apm install" in msg


def test_materialize_local_dep_without_commit_does_not_raise(tmp_path):
    """Local deps legitimately have no resolved_commit -- must not fail-closed."""
    from apm_cli.install.drift import _materialize_install_path

    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()
    local_pkg = tmp_path / "local-pkg"
    local_pkg.mkdir()
    dep = LockedDependency(
        repo_url="./local-pkg",
        source="local",
        local_path="./local-pkg",
        resolved_commit=None,
    )
    # Should not raise the unpinned-remote guard. (May raise a different
    # error depending on local-path resolution; we only assert the
    # specific supply-chain message is NOT what surfaced.)
    try:
        _materialize_install_path(dep, tmp_path, apm_modules, cache_only=True)
    except Exception as exc:  # pragma: no cover -- only inspecting message
        assert "no resolved_commit" not in str(exc), (
            "local deps must not trip the unpinned-remote guard"
        )


# ---------------------------------------------------------------------------
# Defense-in-depth: _ReadOnlyProjectGuard wired into run_replay
# ---------------------------------------------------------------------------


def test_run_replay_wraps_loop_with_readonly_guard(monkeypatch, tmp_path):
    """A monkeypatched integrator that writes to project_root MUST trip the guard.

    Defense-in-depth: even though every integrator should be redirected
    via ``scratch_root=scratch_root``, an accidental direct-path write
    (or a future regression) would silently corrupt the working tree.
    The guard catches it and raises ProtectedPathMutationError.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.drift import ReplayConfig, run_replay
    from apm_cli.utils.guards import ProtectedPathMutationError

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".apm").mkdir()
    (project_root / ".apm" / "tracked.md").write_text("baseline\n", encoding="utf-8")

    lockfile_path = project_root / "apm.lock.yaml"
    # Seed local_deployed_files so LockFile.read() synthesizes the self-entry
    # and the replay loop iterates at least once.
    lock = LockFile()
    lock.local_deployed_files = [".apm/tracked.md"]
    lock.write(lockfile_path)

    # Monkey-patch integrate_package_primitives to write into project_root.
    def _bad_integrator(*args, **kwargs):
        # Simulate a leaky integrator that writes to project tree, not scratch.
        (project_root / ".apm" / "leaked.md").write_text("oops\n", encoding="utf-8")

    monkeypatch.setattr(
        "apm_cli.install.services.integrate_package_primitives",
        _bad_integrator,
    )

    config = ReplayConfig(
        project_root=project_root,
        lockfile_path=lockfile_path,
        targets=None,
        cache_only=True,
    )
    logger = CheckLogger(verbose=False)

    with pytest.raises(ProtectedPathMutationError) as exc_info:
        run_replay(config, logger)

    assert "leaked.md" in str(exc_info.value) or "created:" in str(exc_info.value)
