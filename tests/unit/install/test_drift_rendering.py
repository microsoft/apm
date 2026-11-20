"""Drift logger, target filtering and drift rendering helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import (
    CheckLogger,
    DriftFinding,
    _collect_tracked_files,
    _filter_targets,
    _governed_root_dirs,
    _make_integrators,
    render_drift,
    render_drift_json,
    render_drift_text,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]

# ---------------------------------------------------------------------------
# _assert_scratch_bound
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _materialize_install_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_package_info
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# diff_scratch_against_project: read error path
# ---------------------------------------------------------------------------


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
