"""Unit tests for the ``apm export-patch`` core logic.

The closed-loop contract (edit -> export -> ``git apply`` -> reinstall
-> clean drift) is proven end-to-end in
``tests/integration/test_export_patch_e2e.py``; these tests pin the
unit-level behaviors: reverse mapping by normalized content, raw-bytes
applicability gating, skip reasons, diff shape, filename collision
handling, and header sanitization.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.commands.export_patch import (
    ExportedEdit,
    _base_label,
    _index_source_tree,
    _patch_header,
    _resolve_diff_targets,
    _unified_diff,
    build_patch_export,
    export_patch,
    patch_filename,
    patch_filenames,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import CacheMissError, DriftFinding

_COMMIT = "a" * 40


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _lockfile_with_remote(deployed: list[str]) -> LockFile:
    lock = LockFile()
    dep = LockedDependency(
        repo_url="testorg/testpkg",
        resolved_commit=_COMMIT,
        resolved_ref="main",
        deployed_files=list(deployed),
    )
    lock.add_dependency(dep)
    return lock


def _remote_key(lock: LockFile) -> str:
    return next(iter(lock.dependencies))


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    """Project / scratch / package-source roots for a fabricated remote dep."""
    roots = {
        "project": tmp_path / "project",
        "scratch": tmp_path / "scratch",
        "source": tmp_path / "pkg-src",
    }
    for root in roots.values():
        root.mkdir()
    return roots


def _patch_materialize(monkeypatch: pytest.MonkeyPatch, source_root: Path) -> None:
    monkeypatch.setattr(
        "apm_cli.install.drift._materialize_install_path",
        lambda *args, **kwargs: source_root,
    )


# ---------------------------------------------------------------------------
# patch_filename / patch_filenames
# ---------------------------------------------------------------------------


def test_patch_filename_sanitizes_separators():
    assert patch_filename("testorg/testpkg") == "testorg-testpkg.patch"
    assert patch_filename("gitlab.com/group/sub/repo") == "gitlab.com-group-sub-repo.patch"


def test_patch_filename_never_empty():
    assert patch_filename("///") == "package.patch"


def test_patch_filename_ascii_only_and_reserved_stems():
    # Unicode letters must not survive: repo sanitizers are ASCII-only (#1217).
    assert all(ord(c) < 128 for c in patch_filename("café/päck"))
    # Windows reserved device names cannot be file stems.
    assert patch_filename("nul") == "pkg-nul.patch"
    assert patch_filename("CON") == "pkg-CON.patch"


def test_patch_filenames_disambiguates_collisions():
    # Both keys sanitize to 'org-pkg.patch'; the batch mapping must keep
    # them distinct or one package's patch silently overwrites the other.
    names = patch_filenames(["org/pkg", "org-pkg"])
    assert len(set(names.values())) == 2
    assert all(name.endswith(".patch") for name in names.values())


def test_patch_filenames_stable_without_collisions():
    names = patch_filenames(["testorg/testpkg"])
    assert names == {"testorg/testpkg": "testorg-testpkg.patch"}


# ---------------------------------------------------------------------------
# _unified_diff
# ---------------------------------------------------------------------------


def test_unified_diff_shape():
    diff = _unified_diff("line1\nline2\n", "line1\nline2 edited\n", ".apm/instructions/x.md")
    assert diff.startswith("--- a/.apm/instructions/x.md\n")
    assert "+++ b/.apm/instructions/x.md\n" in diff
    assert "-line2\n" in diff
    assert "+line2 edited\n" in diff


def test_unified_diff_marks_missing_trailing_newline():
    diff = _unified_diff("old\n", "new", "f.md")
    assert "+new\n\\ No newline at end of file\n" in diff


def test_unified_diff_uses_git_line_model_for_bare_cr():
    # str.splitlines would split on the bare CR and desync hunk counts
    # from git's \n-only line model, injecting a spurious no-newline
    # marker mid-hunk.
    diff = _unified_diff("x\rY\n", "z\rY\n", "f.md")
    assert "-x\rY\n" in diff
    assert "+z\rY\n" in diff
    assert "\\ No newline at end of file" not in diff
    assert "@@ -1 +1 @@" in diff


# ---------------------------------------------------------------------------
# _index_source_tree
# ---------------------------------------------------------------------------


def test_index_normalizes_line_endings(tmp_path: Path):
    import hashlib

    _write(tmp_path / "a.md", b"one\r\ntwo\r\n")
    _write(tmp_path / "b.md", b"unique\n")
    index = _index_source_tree(tmp_path)
    assert index.by_digest[hashlib.sha256(b"one\ntwo\n").hexdigest()] == ["a.md"]
    assert index.by_digest[hashlib.sha256(b"unique\n").hexdigest()] == ["b.md"]
    assert index.unindexed == ()


def test_index_excludes_cache_pin_marker(tmp_path: Path):
    from apm_cli.install.cache_pin import MARKER_FILENAME

    _write(tmp_path / MARKER_FILENAME, b'{"schema_version": 1}')
    index = _index_source_tree(tmp_path)
    assert index.by_digest == {}


def test_index_excludes_git_and_pycache_dirs(tmp_path: Path):
    # A .git blob byte-identical to a real source file must not make the
    # reverse mapping ambiguous (mirrors utils/content_hash exclusions).
    _write(tmp_path / ".apm" / "instructions" / "x.md", b"same\n")
    _write(tmp_path / ".git" / "objects" / "blob", b"same\n")
    _write(tmp_path / "__pycache__" / "x.md", b"same\n")
    index = _index_source_tree(tmp_path)
    assert list(index.by_digest.values()) == [[".apm/instructions/x.md"]]


def test_index_records_oversized_files_as_unindexed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("apm_cli.commands.export_patch._MAX_INDEXED_BYTES", 4)
    _write(tmp_path / "big.md", b"0123456789\n")
    index = _index_source_tree(tmp_path)
    assert index.by_digest == {}
    assert index.unindexed == ("big.md",)


def test_index_groups_identical_content(tmp_path: Path):
    _write(tmp_path / "one.md", b"same\n")
    _write(tmp_path / "sub" / "two.md", b"same\n")
    index = _index_source_tree(tmp_path)
    assert list(index.by_digest.values()) == [["one.md", "sub/two.md"]]


# ---------------------------------------------------------------------------
# header sanitization
# ---------------------------------------------------------------------------


def test_header_values_cannot_inject_diff_lines():
    # Registry-controlled fields must never break out of the '# ' comment
    # line: an embedded newline could smuggle attacker hunks that
    # 'git apply' would happily apply.
    dep = LockedDependency(
        repo_url="evil/pkg",
        source="registry",
        version="1.0.0",
        resolved_url="https://x.example\n--- a/.github/workflows/ci.yml\n+++ b/evil",
    )
    label = _base_label(dep)
    assert "\n" not in label
    header = _patch_header("evil/pkg", dep)
    # The security property: every header line stays a '#' comment, so no
    # injected content can start a line with diff syntax.
    for line in header.strip().splitlines():
        assert line.startswith("#"), line


# ---------------------------------------------------------------------------
# build_patch_export
# ---------------------------------------------------------------------------

_DEPLOYED = ".github/instructions/std.instructions.md"
_SOURCE_REL = ".apm/instructions/std.instructions.md"
_ORIGINAL = b"# Standard\n\nrule one\n"
_EDITED = b"# Standard\n\nrule one\nrule two\n"


def _exportable_setup(trees: dict[str, Path]) -> LockFile:
    _write(trees["source"] / _SOURCE_REL, _ORIGINAL)
    _write(trees["scratch"] / _DEPLOYED, _ORIGINAL)
    _write(trees["project"] / _DEPLOYED, _EDITED)
    return _lockfile_with_remote([_DEPLOYED])


def test_exports_verbatim_edit_as_source_diff(trees, monkeypatch):
    lock = _exportable_setup(trees)
    _patch_materialize(monkeypatch, trees["source"])
    key = _remote_key(lock)
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=key)]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.skipped == []
    assert result.exported == [ExportedEdit(_DEPLOYED, _SOURCE_REL, key)]
    patch = result.patches[key]
    assert f"# package: {key}" in patch
    assert f"# base: commit {_COMMIT} (main)" in patch
    assert f"--- a/{_SOURCE_REL}\n" in patch
    assert "+rule two\n" in patch


def test_crlf_source_is_skipped_not_exported(trees, monkeypatch):
    """The digest matches in normalized space, but a CRLF source would
    reject every hunk at git-apply time -- must skip, not export."""
    _write(trees["source"] / _SOURCE_REL, _ORIGINAL.replace(b"\n", b"\r\n"))
    _write(trees["scratch"] / _DEPLOYED, _ORIGINAL)
    _write(trees["project"] / _DEPLOYED, _EDITED)
    lock = _lockfile_with_remote([_DEPLOYED])
    _patch_materialize(monkeypatch, trees["source"])
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert result.exported == []
    assert len(result.skipped) == 1
    assert "CRLF" in result.skipped[0].reason


def test_identical_edits_to_two_deployed_copies_emit_one_diff(trees, monkeypatch):
    deployed2 = ".claude/commands/std.md"
    lock = _exportable_setup(trees)
    _write(trees["scratch"] / deployed2, _ORIGINAL)
    _write(trees["project"] / deployed2, _EDITED)
    _patch_materialize(monkeypatch, trees["source"])
    key = _remote_key(lock)
    findings = [
        DriftFinding(path=_DEPLOYED, kind="modified", package=key),
        DriftFinding(path=deployed2, kind="modified", package=key),
    ]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.skipped == []
    assert {e.deployed_path for e in result.exported} == {_DEPLOYED, deployed2}
    # One source change: the patch must contain the diff exactly once,
    # or git apply fails on the second identical hunk set.
    assert result.patches[key].count(f"--- a/{_SOURCE_REL}\n") == 1


def test_conflicting_edits_to_two_deployed_copies_are_skipped(trees, monkeypatch):
    deployed2 = ".claude/commands/std.md"
    lock = _exportable_setup(trees)
    _write(trees["scratch"] / deployed2, _ORIGINAL)
    _write(trees["project"] / deployed2, _ORIGINAL + b"different edit\n")
    _patch_materialize(monkeypatch, trees["source"])
    key = _remote_key(lock)
    findings = [
        DriftFinding(path=_DEPLOYED, kind="modified", package=key),
        DriftFinding(path=deployed2, kind="modified", package=key),
    ]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert result.exported == []
    assert len(result.skipped) == 2
    assert all("conflicting edits" in s.reason for s in result.skipped)


def test_transformed_content_is_skipped_with_reason(trees, monkeypatch):
    lock = _exportable_setup(trees)
    # Simulate a format-transformed deployment: the replayed content no
    # longer matches any source file byte-for-byte.
    _write(trees["scratch"] / _DEPLOYED, b"---\nglobs: '**'\n---\nrule one\n")
    _patch_materialize(monkeypatch, trees["source"])
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "transformed" in result.skipped[0].reason


def test_unmatched_finding_mentions_unindexed_sources(trees, monkeypatch):
    """A size-capped source must not be misreported as 'transformed'
    without a hint that indexing was incomplete."""
    monkeypatch.setattr("apm_cli.commands.export_patch._MAX_INDEXED_BYTES", 4)
    lock = _exportable_setup(trees)
    _patch_materialize(monkeypatch, trees["source"])
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "not indexed" in result.skipped[0].reason


def test_ambiguous_source_is_skipped(trees, monkeypatch):
    lock = _exportable_setup(trees)
    _write(trees["source"] / ".apm/instructions/copy.instructions.md", _ORIGINAL)
    _patch_materialize(monkeypatch, trees["source"])
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "ambiguous" in result.skipped[0].reason


def test_local_package_is_skipped(trees, monkeypatch):
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="_local/mypkg",
            source="local",
            local_path="./packages/mypkg",
            deployed_files=[_DEPLOYED],
        )
    )
    key = next(iter(lock.dependencies))
    _write(trees["scratch"] / _DEPLOYED, _ORIGINAL)
    _write(trees["project"] / _DEPLOYED, _EDITED)
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=key)]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "local package" in result.skipped[0].reason


def test_project_self_content_is_skipped(trees):
    lock = LockFile()
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=".")]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "project-local" in result.skipped[0].reason


def test_binary_content_is_skipped(trees, monkeypatch):
    lock = _exportable_setup(trees)
    blob = b"\xff\xfe\x00binary"
    _write(trees["source"] / ".apm/instructions/bin.dat", blob)
    _write(trees["scratch"] / _DEPLOYED, blob)
    _write(trees["project"] / _DEPLOYED, blob + b"\x01")
    _patch_materialize(monkeypatch, trees["source"])
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "binary" in result.skipped[0].reason


def test_cache_miss_is_skipped_with_reason(trees, monkeypatch):
    lock = _exportable_setup(trees)

    def _raise(*args, **kwargs):
        raise CacheMissError("cache miss for testorg/testpkg")

    monkeypatch.setattr("apm_cli.install.drift._materialize_install_path", _raise)
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package=_remote_key(lock))]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "cache unavailable" in result.skipped[0].reason


def test_non_modified_findings_are_ignored(trees, monkeypatch):
    lock = _exportable_setup(trees)
    _patch_materialize(monkeypatch, trees["source"])
    key = _remote_key(lock)
    findings = [
        DriftFinding(path=".github/instructions/gone.md", kind="unintegrated", package=key),
        DriftFinding(path=".github/instructions/extra.md", kind="orphaned", package=key),
    ]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert result.exported == []
    assert result.skipped == []


def test_untracked_finding_is_skipped(trees):
    lock = LockFile()
    findings = [DriftFinding(path=_DEPLOYED, kind="modified", package="")]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.patches == {}
    assert len(result.skipped) == 1
    assert "not tracked" in result.skipped[0].reason


def test_legacy_dir_tracked_finding_resolves_package(trees, monkeypatch):
    """Legacy lockfiles track skill dirs with a trailing slash; a modified
    file under such a dir must still resolve to the owning package."""
    deployed_dir = ".github/skills/helper/"
    deployed_file = ".github/skills/helper/SKILL.md"
    source_rel = ".apm/skills/helper/SKILL.md"
    lock = _lockfile_with_remote([deployed_dir])
    key = _remote_key(lock)
    _write(trees["source"] / source_rel, _ORIGINAL)
    _write(trees["scratch"] / deployed_file, _ORIGINAL)
    _write(trees["project"] / deployed_file, _EDITED)
    _patch_materialize(monkeypatch, trees["source"])
    # Diff engine could not attribute the file (only the dir is tracked).
    findings = [DriftFinding(path=deployed_file, kind="modified", package="")]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert result.exported == [ExportedEdit(deployed_file, source_rel, key)]
    assert key in result.patches


def test_legacy_dir_tracked_local_content_gets_local_reason(trees):
    """A legacy dir-tracked local_deployed_files entry must resolve to the
    self key, not fall through to 'not tracked'."""
    deployed_file = ".github/skills/mine/SKILL.md"
    lock = LockFile()
    lock.local_deployed_files = [".github/skills/mine/"]
    findings = [DriftFinding(path=deployed_file, kind="modified", package="")]

    result = build_patch_export(trees["project"], trees["scratch"], lock, findings)

    assert len(result.skipped) == 1
    assert "project-local" in result.skipped[0].reason


# ---------------------------------------------------------------------------
# CLI-level regression tests
# ---------------------------------------------------------------------------


def _make_cli_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "cliproj"
    project.mkdir()
    (project / "apm.yml").write_bytes(
        yaml.safe_dump({"name": "cliproj", "version": "1.0.0", "target": "copilot"}).encode()
    )
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="testorg/testpkg",
            resolved_commit=_COMMIT,
            deployed_files=[_DEPLOYED],
        )
    )
    from apm_cli.deps.lockfile import get_lockfile_path

    lock.write(get_lockfile_path(project))
    monkeypatch.chdir(project)
    return project


def test_cli_unexpected_replay_error_exits_1_with_message(tmp_path, monkeypatch):
    """Exceptions outside the anticipated replay surface must hit the
    outer net (exit 1 + message), not escape as a traceback."""
    _make_cli_project(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("apm_cli.install.drift.run_replay", _boom)
    result = CliRunner().invoke(export_patch, [], catch_exceptions=False)
    assert result.exit_code == 1
    assert "Error exporting patches: boom" in result.output


def test_cli_rejects_out_dir_inside_apm_modules(tmp_path, monkeypatch):
    _make_cli_project(tmp_path, monkeypatch)
    result = CliRunner().invoke(export_patch, ["-o", "apm_modules/evil"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "apm_modules" in result.output


def test_cli_reports_unparsable_lockfile_distinctly(tmp_path, monkeypatch):
    project = _make_cli_project(tmp_path, monkeypatch)
    from apm_cli.deps.lockfile import get_lockfile_path

    get_lockfile_path(project).write_text(":\nnot valid yaml [", encoding="utf-8")
    result = CliRunner().invoke(export_patch, [], catch_exceptions=False)
    assert result.exit_code == 1
    assert "could not be parsed" in result.output
    assert "No lockfile found" not in result.output


def test_cli_all_local_lockfile_short_circuits_without_replay(tmp_path, monkeypatch):
    project = _make_cli_project(tmp_path, monkeypatch)
    from apm_cli.deps.lockfile import get_lockfile_path

    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="_local/mypkg", source="local", local_path="./pkg")
    )
    lock.write(get_lockfile_path(project))

    def _fail(*args, **kwargs):
        raise AssertionError("replay must not run for an all-local lockfile")

    monkeypatch.setattr("apm_cli.install.drift.run_replay", _fail)
    result = CliRunner().invoke(export_patch, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "nothing to" in result.output.lower()


def test_resolve_diff_targets_honors_apm_yml_target(tmp_path, monkeypatch):
    """A declared target whose root dir does not exist must still be part
    of the diff target set, or its findings silently vanish (#1924)."""
    project = tmp_path / "tproj"
    project.mkdir()
    (project / "apm.yml").write_bytes(
        yaml.safe_dump({"name": "tproj", "version": "1.0.0", "target": "claude"}).encode()
    )
    monkeypatch.chdir(project)
    names = [t.name for t in _resolve_diff_targets(project)]
    assert "claude" in names
