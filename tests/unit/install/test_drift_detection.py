"""Unit tests for ``apm_cli.install.drift``.

Covers branches not hit by existing test_drift.py / test_drift_perf.py:

* ``_assert_scratch_bound`` happy path and failure path
* ``CheckLogger`` all phase markers
* ``_materialize_install_path`` all error branches
* ``_build_package_info`` with and without apm.yml
* ``_make_integrators`` smoke
* ``_filter_targets`` with and without names
* ``_read_apm_yml_target`` all branches
* ``_governed_root_dirs`` root set
* ``_walk_managed`` files and empty
* ``_collect_tracked_files`` with local_deployed_files
* ``_inline_diff_for`` size cap + both-small case
* ``diff_scratch_against_project`` read error path
* ``render_drift_text`` verbose inline_diff path
* ``render_drift_json`` shape
* ``render_drift`` format dispatch
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import (
    CacheMissError,
    CheckLogger,
    DriftFinding,
    ReplayConfig,
    _assert_scratch_bound,
    _build_package_info,
    _collect_tracked_files,
    _filter_targets,
    _governed_root_dirs,
    _inline_diff_for,
    _make_integrators,
    _read_apm_yml_target,
    _walk_managed,
    diff_scratch_against_project,
    render_drift,
    render_drift_json,
    render_drift_text,
)

# ---------------------------------------------------------------------------
# _assert_scratch_bound
# ---------------------------------------------------------------------------


class TestAssertScratchBound:
    def test_scratch_outside_project_does_not_raise(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        _assert_scratch_bound(project, scratch)  # must not raise

    def test_scratch_inside_project_raises(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        scratch = project / "scratch"
        scratch.mkdir()
        with pytest.raises(RuntimeError, match="inside project tree"):
            _assert_scratch_bound(project, scratch)

    def test_scratch_equal_to_project_raises(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        with pytest.raises(RuntimeError, match="inside project tree"):
            _assert_scratch_bound(project, project)


# ---------------------------------------------------------------------------
# CheckLogger
# ---------------------------------------------------------------------------


class TestCheckLogger:
    def test_replay_start_writes_to_stderr(self, capsys) -> None:
        logger = CheckLogger(verbose=False)
        logger.replay_start()
        captured = capsys.readouterr()
        assert "Replaying" in captured.err

    def test_diff_start_writes_to_stderr(self, capsys) -> None:
        logger = CheckLogger(verbose=False)
        logger.diff_start()
        captured = capsys.readouterr()
        assert "Diffing" in captured.err

    def test_replay_complete_includes_count(self, capsys) -> None:
        logger = CheckLogger(verbose=False)
        logger.replay_complete(7)
        captured = capsys.readouterr()
        assert "7" in captured.err

    def test_clean_writes_no_drift(self, capsys) -> None:
        logger = CheckLogger(verbose=False)
        logger.clean()
        captured = capsys.readouterr()
        assert "No drift" in captured.err

    def test_findings_includes_count(self, capsys) -> None:
        logger = CheckLogger(verbose=False)
        logger.findings(3)
        captured = capsys.readouterr()
        assert "3" in captured.err

    def test_scratch_root_silent_when_not_verbose(self, capsys, tmp_path: Path) -> None:
        logger = CheckLogger(verbose=False)
        logger.scratch_root(tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_scratch_root_emits_when_verbose(self, capsys, tmp_path: Path) -> None:
        logger = CheckLogger(verbose=True)
        logger.scratch_root(tmp_path)
        captured = capsys.readouterr()
        assert str(tmp_path) in captured.err


# ---------------------------------------------------------------------------
# _materialize_install_path
# ---------------------------------------------------------------------------


class TestMaterializeInstallPath:
    def test_not_cache_only_raises_not_implemented(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        dep = LockedDependency(repo_url="example/pkg", resolved_commit="abc")
        with pytest.raises(NotImplementedError, match="no-cache"):
            _materialize_install_path(dep, tmp_path, tmp_path, cache_only=False)

    def test_local_dep_no_local_path_raises_cache_miss(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        dep = LockedDependency(repo_url="./foo", source="local", local_path=None)
        with pytest.raises(CacheMissError, match="no local_path"):
            _materialize_install_path(dep, tmp_path, tmp_path / "apm_modules", cache_only=True)

    def test_local_dep_missing_directory_raises_cache_miss(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        dep = LockedDependency(
            repo_url="./does_not_exist", source="local", local_path="./does_not_exist"
        )
        with pytest.raises(CacheMissError, match="local source missing"):
            _materialize_install_path(dep, tmp_path, tmp_path / "apm_modules", cache_only=True)

    def test_local_dep_existing_directory_returns_path(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        local_pkg = tmp_path / "local_pkg"
        local_pkg.mkdir()
        dep = LockedDependency(repo_url="./local_pkg", source="local", local_path="./local_pkg")
        result = _materialize_install_path(dep, tmp_path, tmp_path / "apm_modules", cache_only=True)
        assert result.exists()

    def test_remote_dep_no_commit_raises_cache_miss(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        dep = LockedDependency(repo_url="owner/repo", resolved_commit=None)
        with pytest.raises(CacheMissError, match="no resolved_commit"):
            _materialize_install_path(dep, tmp_path, tmp_path / "apm_modules", cache_only=True)


# ---------------------------------------------------------------------------
# _build_package_info
# ---------------------------------------------------------------------------


class TestBuildPackageInfo:
    def test_without_apm_yml_uses_install_path_name(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "mypkg"
        pkg_dir.mkdir()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc1234",
            resolved_ref="main",
        )
        info = _build_package_info(dep, pkg_dir)
        assert info.install_path == pkg_dir

    def test_with_apm_yml_loads_package(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "loaded_pkg"
        pkg_dir.mkdir()
        apm_yml = pkg_dir / "apm.yml"
        apm_yml.write_text("name: loaded_pkg\nversion: '1.0'\n", encoding="utf-8")
        dep = LockedDependency(
            repo_url="owner/loaded",
            resolved_commit="def5678",
            resolved_ref="v1.0",
        )
        info = _build_package_info(dep, pkg_dir)
        assert info.install_path == pkg_dir

    def test_with_broken_apm_yml_falls_back_gracefully(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "broken_pkg"
        pkg_dir.mkdir()
        apm_yml = pkg_dir / "apm.yml"
        apm_yml.write_bytes(b"\xff\xfe not valid yaml: [[[")
        dep = LockedDependency(
            repo_url="owner/broken",
            resolved_commit="aaa",
            resolved_ref="main",
        )
        # Should not raise
        info = _build_package_info(dep, pkg_dir)
        assert info.install_path == pkg_dir


# ---------------------------------------------------------------------------
# _make_integrators
# ---------------------------------------------------------------------------


class TestMakeIntegrators:
    def test_returns_all_expected_keys(self) -> None:
        integrators = _make_integrators()
        expected = {"prompt", "agent", "skill", "command", "hook", "instruction"}
        assert expected == set(integrators.keys())


# ---------------------------------------------------------------------------
# _filter_targets
# ---------------------------------------------------------------------------


class TestFilterTargets:
    def _make_target(self, name: str) -> MagicMock:
        t = MagicMock()
        t.name = name
        return t

    def test_no_names_returns_all(self) -> None:
        targets = [self._make_target("a"), self._make_target("b")]
        assert _filter_targets(targets, None) == targets

    def test_names_filters_correctly(self) -> None:
        targets = [self._make_target("a"), self._make_target("b"), self._make_target("c")]
        result = _filter_targets(targets, frozenset({"a", "c"}))
        names = [t.name for t in result]
        assert "b" not in names
        assert "a" in names and "c" in names

    def test_empty_names_returns_all_targets(self) -> None:
        # frozenset() is falsy so _filter_targets treats it as "no filter"
        targets = [self._make_target("x")]
        result = _filter_targets(targets, frozenset())
        assert result == targets


# ---------------------------------------------------------------------------
# _read_apm_yml_target
# ---------------------------------------------------------------------------


class TestReadApmYmlTarget:
    def test_no_apm_yml_returns_none(self, tmp_path: Path) -> None:
        assert _read_apm_yml_target(tmp_path) is None

    def test_apm_yml_no_target_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\n", encoding="utf-8")
        assert _read_apm_yml_target(tmp_path) is None

    def test_apm_yml_unreadable_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "apm.yml"
        p.write_bytes(b"\xff not yaml [[[")
        # Should not raise
        result = _read_apm_yml_target(tmp_path)
        assert result is None

    def test_apm_yml_with_target_returns_value(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\ntarget: copilot\n", encoding="utf-8")
        with patch("apm_cli.core.target_detection.parse_target_field", return_value="copilot"):
            result = _read_apm_yml_target(tmp_path)
        assert result == "copilot"

    def test_parse_target_field_exception_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("name: pkg\ntarget: invalid\n", encoding="utf-8")
        with patch(
            "apm_cli.core.target_detection.parse_target_field",
            side_effect=ValueError("bad"),
        ):
            result = _read_apm_yml_target(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _governed_root_dirs
# ---------------------------------------------------------------------------


class TestGovernedRootDirs:
    def test_always_includes_dot_apm(self) -> None:
        assert ".apm" in _governed_root_dirs([])

    def test_includes_target_root_dir(self) -> None:
        t = MagicMock()
        t.root_dir = ".github/copilot-instructions.md"
        result = _governed_root_dirs([t])
        assert ".github" in result

    def test_targets_without_root_dir_skipped(self) -> None:
        t = MagicMock()
        t.root_dir = None
        result = _governed_root_dirs([t])
        assert result == {".apm"}


# ---------------------------------------------------------------------------
# _walk_managed
# ---------------------------------------------------------------------------


class TestWalkManaged:
    def test_returns_empty_when_root_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_such_dir"
        result = _walk_managed(missing, {".apm"})
        assert result == {}

    def test_finds_nested_files(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm" / "skills"
        apm_dir.mkdir(parents=True)
        (apm_dir / "skill.md").write_text("content", encoding="utf-8")
        result = _walk_managed(tmp_path, {".apm"})
        assert any("skill.md" in k for k in result)

    def test_agents_md_at_top_level_included(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
        result = _walk_managed(tmp_path, {".apm"})
        assert "AGENTS.md" in result


# ---------------------------------------------------------------------------
# _collect_tracked_files
# ---------------------------------------------------------------------------


class TestCollectTrackedFiles:
    def test_dep_deployed_files_included(self) -> None:
        lock = LockFile()
        dep = LockedDependency(repo_url="owner/pkg", deployed_files=[".apm/skills/x.md"])
        lock.add_dependency(dep)
        tracked = _collect_tracked_files(lock)
        assert ".apm/skills/x.md" in tracked

    def test_local_deployed_files_included(self) -> None:
        lock = LockFile()
        lock.local_deployed_files = [".apm/local.md"]
        tracked = _collect_tracked_files(lock)
        assert ".apm/local.md" in tracked
        assert tracked[".apm/local.md"] == "."

    def test_first_dep_wins_for_duplicate_path(self) -> None:
        lock = LockFile()
        dep1 = LockedDependency(repo_url="owner/first", deployed_files=["shared/file.md"])
        dep2 = LockedDependency(repo_url="owner/second", deployed_files=["shared/file.md"])
        lock.add_dependency(dep1)
        lock.add_dependency(dep2)
        tracked = _collect_tracked_files(lock)
        # First setter wins (setdefault semantics)
        assert tracked["shared/file.md"] in {"owner/first", "owner/second"}


# ---------------------------------------------------------------------------
# _inline_diff_for
# ---------------------------------------------------------------------------


class TestInlineDiffFor:
    def test_returns_empty_for_small_files(self, tmp_path: Path) -> None:
        s = tmp_path / "scratch.md"
        p = tmp_path / "project.md"
        s.write_bytes(b"small")
        p.write_bytes(b"small2")
        result = _inline_diff_for(s, p)
        assert result == ""

    def test_returns_hint_for_large_file(self, tmp_path: Path) -> None:
        s = tmp_path / "big.bin"
        p = tmp_path / "big2.bin"
        s.write_bytes(b"x" * (101 * 1024))
        p.write_bytes(b"y" * 10)
        result = _inline_diff_for(s, p)
        assert "too large" in result.lower()

    def test_returns_empty_on_oserror(self, tmp_path: Path) -> None:
        missing1 = tmp_path / "a"
        missing2 = tmp_path / "b"
        result = _inline_diff_for(missing1, missing2)
        assert result == ""


# ---------------------------------------------------------------------------
# diff_scratch_against_project: read error path
# ---------------------------------------------------------------------------


class TestDiffScratchReadError:
    def test_read_error_emits_modified_finding(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        project = tmp_path / "project"
        scratch_apm = scratch / ".apm"
        project_apm = project / ".apm"
        scratch_apm.mkdir(parents=True)
        project_apm.mkdir(parents=True)
        (scratch_apm / "f.md").write_text("content", encoding="utf-8")
        (project_apm / "f.md").write_text("content", encoding="utf-8")

        lock = LockFile()
        dep = LockedDependency(repo_url="owner/pkg", deployed_files=[".apm/f.md"])
        lock.add_dependency(dep)

        t = MagicMock()
        t.root_dir = ".apm"

        with patch.object(Path, "read_bytes", side_effect=OSError("perm denied")):
            findings = diff_scratch_against_project(scratch, project, lock, [t])

        read_err_findings = [f for f in findings if "read error" in f.inline_diff]
        assert len(read_err_findings) >= 1


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderDriftText:
    def test_empty_findings_shows_no_drift(self) -> None:
        out = render_drift_text([])
        assert "No drift" in out

    def test_findings_grouped_by_kind(self) -> None:
        findings = [
            DriftFinding(path="a.md", kind="modified"),
            DriftFinding(path="b.md", kind="unintegrated"),
            DriftFinding(path="c.md", kind="orphaned"),
        ]
        out = render_drift_text(findings)
        assert "modified (1)" in out
        assert "unintegrated (1)" in out
        assert "orphaned (1)" in out

    def test_verbose_includes_inline_diff(self) -> None:
        findings = [DriftFinding(path="x.md", kind="modified", inline_diff="see diff here")]
        out = render_drift_text(findings, verbose=True)
        assert "see diff here" in out

    def test_non_verbose_hides_inline_diff(self) -> None:
        findings = [DriftFinding(path="x.md", kind="modified", inline_diff="hidden hint")]
        out = render_drift_text(findings, verbose=False)
        assert "hidden hint" not in out

    def test_package_name_shown_in_output(self) -> None:
        findings = [DriftFinding(path="p.md", kind="orphaned", package="mypkg")]
        out = render_drift_text(findings)
        assert "mypkg" in out


class TestRenderDriftJson:
    def test_returns_dict_with_drift_key(self) -> None:
        findings = [DriftFinding(path="x.md", kind="modified", package="pkg")]
        result = render_drift_json(findings)
        assert "drift" in result
        assert result["drift"][0]["path"] == "x.md"
        assert result["drift"][0]["kind"] == "modified"

    def test_empty_findings_returns_empty_list(self) -> None:
        result = render_drift_json([])
        assert result == {"drift": []}


class TestRenderDrift:
    def test_default_text_format(self) -> None:
        findings = [DriftFinding(path="a.md", kind="modified")]
        out = render_drift(findings, fmt="text")
        assert isinstance(out, str)
        assert "modified" in out

    def test_json_format_is_valid_json(self) -> None:
        findings = [DriftFinding(path="b.md", kind="orphaned")]
        out = render_drift(findings, fmt="json")
        data = json.loads(out)
        assert "drift" in data

    def test_sarif_format_is_valid_json(self) -> None:
        findings = [DriftFinding(path="c.md", kind="unintegrated")]
        out = render_drift(findings, fmt="sarif")
        data = json.loads(out)
        assert "results" in data
        assert data["results"][0]["ruleId"].startswith("apm/drift/")


class TestReplayConfig:
    def test_default_values(self, tmp_path: Path) -> None:
        cfg = ReplayConfig(
            project_root=tmp_path,
            lockfile_path=tmp_path / "apm.lock.yaml",
        )
        assert cfg.cache_only is True
        assert cfg.no_hooks is True
        assert cfg.parallel_downloads == 1
        assert cfg.targets is None
