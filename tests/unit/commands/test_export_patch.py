"""Unit tests for the ``apm export-patch`` core logic.

The closed-loop contract (edit -> export -> ``git apply`` -> reinstall
-> clean drift) is proven end-to-end in
``tests/integration/test_export_patch_e2e.py``; these tests pin the
unit-level behaviors: reverse mapping by normalized content, skip
reasons, diff shape, and filename sanitization.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.commands.export_patch import (
    ExportedEdit,
    _index_source_tree,
    _unified_diff,
    build_patch_export,
    patch_filename,
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
# patch_filename
# ---------------------------------------------------------------------------


def test_patch_filename_sanitizes_separators():
    assert patch_filename("testorg/testpkg") == "testorg-testpkg.patch"
    assert patch_filename("gitlab.com/group/sub/repo") == "gitlab.com-group-sub-repo.patch"


def test_patch_filename_never_empty():
    assert patch_filename("///") == "package.patch"


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


# ---------------------------------------------------------------------------
# _index_source_tree
# ---------------------------------------------------------------------------


def test_index_normalizes_line_endings(tmp_path: Path):
    _write(tmp_path / "a.md", b"one\r\ntwo\r\n")
    _write(tmp_path / "b.md", b"unique\n")
    index = _index_source_tree(tmp_path)
    import hashlib

    lf_digest = hashlib.sha256(b"one\ntwo\n").hexdigest()
    assert index[lf_digest] == ["a.md"]


def test_index_excludes_cache_pin_marker(tmp_path: Path):
    from apm_cli.install.cache_pin import MARKER_FILENAME

    _write(tmp_path / MARKER_FILENAME, b'{"schema_version": 1}')
    index = _index_source_tree(tmp_path)
    assert index == {}


def test_index_groups_identical_content(tmp_path: Path):
    _write(tmp_path / "one.md", b"same\n")
    _write(tmp_path / "sub" / "two.md", b"same\n")
    index = _index_source_tree(tmp_path)
    assert list(index.values()) == [["one.md", "sub/two.md"]]


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
    assert "transform" in result.skipped[0].reason


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
