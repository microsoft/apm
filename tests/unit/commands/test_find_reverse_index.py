"""Unit tests for the reverse index builder in apm find."""

from __future__ import annotations

from apm_cli.commands.find import _lookup_in_index, build_reverse_index
from apm_cli.deps.lockfile import LockedDependency, LockFile


def _make_lockfile(*deps: LockedDependency) -> LockFile:
    lf = LockFile()
    for dep in deps:
        lf.add_dependency(dep)
    return lf


def _make_dep(
    repo_url: str,
    deployed_files: list[str] | None = None,
    source: str | None = None,
    local_path: str | None = None,
    resolved_url: str | None = None,
    resolved_ref: str | None = None,
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        deployed_files=deployed_files or [],
        source=source,
        local_path=local_path,
        resolved_url=resolved_url,
        resolved_ref=resolved_ref,
    )


class TestBuildReverseIndex:
    def test_single_dep_single_file(self):
        dep = _make_dep("owner/repo", deployed_files=[".github/skills/foo/"])
        lf = _make_lockfile(dep)
        idx = build_reverse_index(lf)
        assert ".github/skills/foo/" in idx
        assert "owner/repo" in idx[".github/skills/foo/"]

    def test_multi_contributor_shared_file(self):
        dep1 = _make_dep("owner/pkg-a", deployed_files=["AGENTS.md"])
        dep2 = _make_dep("owner/pkg-b", deployed_files=["AGENTS.md"])
        lf = _make_lockfile(dep1, dep2)
        idx = build_reverse_index(lf)
        assert "AGENTS.md" in idx
        owners = idx["AGENTS.md"]
        assert "owner/pkg-a" in owners
        assert "owner/pkg-b" in owners

    def test_empty_deployed_files_skipped(self):
        dep = _make_dep("owner/repo", deployed_files=[])
        lf = _make_lockfile(dep)
        idx = build_reverse_index(lf)
        assert idx == {}

    def test_multiple_files_mapped_to_same_dep(self):
        dep = _make_dep(
            "owner/repo",
            deployed_files=[
                ".github/instructions/foo.md",
                ".claude/skills/bar/",
            ],
        )
        lf = _make_lockfile(dep)
        idx = build_reverse_index(lf)
        assert ".github/instructions/foo.md" in idx
        assert ".claude/skills/bar/" in idx
        assert idx[".github/instructions/foo.md"] == ["owner/repo"]
        assert idx[".claude/skills/bar/"] == ["owner/repo"]

    def test_local_deployed_files_are_indexed_under_workspace(self):
        lf = LockFile()
        lf.local_deployed_files = [".github/instructions/workspace.instructions.md"]
        idx = build_reverse_index(lf)
        assert ".github/instructions/workspace.instructions.md" in idx
        assert idx[".github/instructions/workspace.instructions.md"] == ["."]

    def test_claude_md_multi_contributor(self):
        dep1 = _make_dep("owner/pkg-a", deployed_files=["CLAUDE.md"])
        dep2 = _make_dep("owner/pkg-b", deployed_files=["CLAUDE.md"])
        lf = _make_lockfile(dep1, dep2)
        idx = build_reverse_index(lf)
        owners = idx["CLAUDE.md"]
        assert "owner/pkg-a" in owners
        assert "owner/pkg-b" in owners
        assert len(owners) == 2


class TestLookupInIndex:
    def test_backslash_normalized_to_forward_slash(self):
        """Windows-style backslash paths are normalized to forward slash."""
        idx = {".github/skills/foo/": ["owner/repo"]}
        result = _lookup_in_index(".github\\skills\\foo\\bar.md", idx)
        assert result == ["owner/repo"]
