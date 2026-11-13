"""Tests for the 'apm export-patch' hint surfaced by audit and install (#2118).

Pins three contracts:

* ``count_local_edits`` counts only managed files of remote packages whose
  current bytes differ from the hash recorded at deployment, read-only.
* ``render_drift_text`` adds the hint when ``modified`` findings exist.
* ``run_install_pipeline`` shows the hint once, only for project-scope
  installs that will re-sync files (not dry-run, not lockfile-only).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.drift import DriftFinding, render_drift_text
from apm_cli.install.local_edits import (
    count_exportable_findings,
    count_local_edits,
    is_upstream_package,
    local_edit_hint,
    rendering_change_hint,
    unedited_modified_paths,
)
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component

_ORIGINAL = b"# Standard\n\nrule one\n"
_EDITED = b"# Standard\n\nrule one\nrule two\n"


def _deploy(project: Path, rel: str, recorded: bytes, on_disk: bytes | None) -> str:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(recorded)
    content_hash = compute_file_hash(path)
    if on_disk is None:
        path.unlink()
    else:
        path.write_bytes(on_disk)
    return content_hash


def _lockfile(project: Path, **files: tuple[bytes, bytes | None]) -> LockFile:
    lock = LockFile()
    hashes = {rel: _deploy(project, rel, rec, disk) for rel, (rec, disk) in files.items()}
    lock.add_dependency(
        LockedDependency(
            repo_url="org/pkg",
            resolved_commit="c" * 40,
            deployed_files=sorted(hashes),
            deployed_file_hashes=hashes,
        )
    )
    return lock


class TestLocalEditHint:
    def test_audit_wording_points_at_export_patch(self) -> None:
        text = local_edit_hint(2)
        assert text.startswith("2 local edit(s) to managed files;")
        assert "run 'apm export-patch'" in text
        assert "before the next install overwrites them" in text
        assert text.isascii()

    def test_install_wording_names_this_install(self) -> None:
        text = local_edit_hint(1, installing=True)
        assert text.startswith("1 local edit(s) to managed files may be overwritten")
        assert "this install" in text
        assert "next time" in text
        assert "run 'apm export-patch'" in text
        assert text.isascii()


class TestCountLocalEdits:
    def test_counts_only_edited_existing_files(self, tmp_path: Path) -> None:
        lock = _lockfile(
            tmp_path,
            **{
                ".github/instructions/a.instructions.md": (_ORIGINAL, _EDITED),
                ".github/instructions/b.instructions.md": (_ORIGINAL, _ORIGINAL),
                ".github/instructions/c.instructions.md": (_ORIGINAL, None),
            },
        )
        key = next(iter(lock.dependencies))
        assert count_local_edits(tmp_path, lock, [key]) == 1

    def test_is_read_only(self, tmp_path: Path) -> None:
        rel = ".github/instructions/a.instructions.md"
        lock = _lockfile(tmp_path, **{rel: (_ORIGINAL, _EDITED)})
        count_local_edits(tmp_path, lock, list(lock.dependencies))
        assert (tmp_path / rel).read_bytes() == _EDITED

    def test_ignores_packages_not_being_installed(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        assert count_local_edits(tmp_path, lock, ["other/pkg"]) == 0

    def test_ignores_local_path_packages(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        dep = next(iter(lock.dependencies.values()))
        dep.source = "local"
        dep.local_path = "../pkg"
        assert count_local_edits(tmp_path, lock, list(lock.dependencies)) == 0

    def test_ignores_files_without_recorded_hash(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        dep = next(iter(lock.dependencies.values()))
        dep.deployed_file_hashes = {}
        assert count_local_edits(tmp_path, lock, list(lock.dependencies)) == 0

    def test_ignores_paths_escaping_the_project(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_bytes(_ORIGINAL)
        recorded = compute_file_hash(outside)
        outside.write_bytes(_EDITED)
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                deployed_files=["../outside.md"],
                deployed_file_hashes={"../outside.md": recorded},
            )
        )
        assert count_local_edits(project, lock, list(lock.dependencies)) == 0

    def test_no_lockfile_or_keys(self, tmp_path: Path) -> None:
        assert count_local_edits(tmp_path, None, ["org/pkg"]) == 0
        assert count_local_edits(tmp_path, LockFile(), []) == 0


class TestUpstreamOwnership:
    def test_project_local_and_local_path_owners_are_not_upstream(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        key = next(iter(lock.dependencies))
        assert is_upstream_package(key, lock)
        assert not is_upstream_package(".", lock)
        assert not is_upstream_package("unknown/pkg", lock)
        assert is_upstream_package("", lock)
        lock.dependencies[key].local_path = "../pkg"
        assert not is_upstream_package(key, lock)

    def test_count_exportable_findings_matches_export_scope(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        key = next(iter(lock.dependencies))
        findings = [
            DriftFinding(path=".github/a.md", kind="modified", package=key),
            DriftFinding(path="AGENTS.md", kind="modified", package="."),
            DriftFinding(path=".github/b.md", kind="unintegrated", package=key),
        ]
        assert count_exportable_findings(findings, lock) == 1
        lock.dependencies[key].source = "local"
        assert count_exportable_findings(findings, lock) == 0


class TestDriftTextHint:
    def test_modified_findings_add_export_hint(self) -> None:
        out = render_drift_text(
            [
                DriftFinding(path=".github/a.md", kind="modified", package="org/pkg"),
                DriftFinding(path=".github/b.md", kind="modified", package="org/pkg"),
            ]
        )
        assert local_edit_hint(2) in out
        # The hint must precede the re-sync advice that would overwrite edits.
        assert out.index("apm export-patch") < out.index("Run 'apm install'")

    def test_local_path_package_findings_get_no_hint(self, tmp_path: Path) -> None:
        lock = _lockfile(tmp_path, **{".github/a.md": (_ORIGINAL, _EDITED)})
        key = next(iter(lock.dependencies))
        lock.dependencies[key].local_path = "../pkg"
        out = render_drift_text(
            [DriftFinding(path=".github/a.md", kind="modified", package=key)], lockfile=lock
        )
        assert "apm export-patch" not in out

    def test_project_local_and_unintegrated_findings_get_no_hint(self) -> None:
        out = render_drift_text(
            [
                DriftFinding(path="AGENTS.md", kind="modified", package="."),
                DriftFinding(path=".github/c.md", kind="unintegrated", package="org/pkg"),
            ]
        )
        assert "apm export-patch" not in out


def _pipeline_patches(deploy_root: Path, lockfile: object) -> list[AbstractContextManager]:
    lockfile_cls = MagicMock()
    lockfile_cls.read.return_value = lockfile
    return [
        patch("apm_cli.deps.lockfile.LockFile", lockfile_cls),
        patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=deploy_root / "x.lock"),
        patch("apm_cli.core.scope.get_deploy_root", return_value=deploy_root),
        patch("apm_cli.core.scope.get_apm_dir", return_value=deploy_root),
        patch(
            "apm_cli.install.phases.local_content._project_has_root_primitives",
            return_value=False,
        ),
        patch("apm_cli.utils.install_tui.InstallTui"),
        patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None),
        patch(
            "apm_cli.integration.base_integrator.BaseIntegrator.normalize_managed_files",
            return_value=set(),
        ),
        patch("apm_cli.commands._helpers._update_gitignore_for_apm_modules"),
    ]


def _run_pipeline(tmp_path: Path, edited: int, **kwargs) -> tuple[MagicMock, MagicMock]:
    from apm_cli.install.pipeline import run_install_pipeline

    pkg = MagicMock()
    pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="org/pkg", host="github.com")]
    pkg.get_dev_apm_dependencies.return_value = []
    pkg.get_mcp_dependencies.return_value = []
    lockfile = MagicMock()
    lockfile.dependencies = {}
    logger = MagicMock()

    dep = MagicMock()
    dep.get_unique_key.return_value = "org/pkg"

    def _phase(name, phase, ctx):
        if name == "resolve":
            ctx.deps_to_install = [dep]
        elif name == "targets":
            ctx.integrators["skill"] = MagicMock()

    counter = MagicMock(return_value=edited)
    with (
        patch("apm_cli.install.pipeline._run_phase", side_effect=_phase),
        patch("apm_cli.install.local_edits.count_local_edits", counter),
    ):
        stack = _pipeline_patches(tmp_path, lockfile)
        for cm in stack:
            cm.__enter__()
        try:
            run_install_pipeline(pkg, logger=logger, target="claude", **kwargs)
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)
    return logger, counter


def _hint_calls(logger: MagicMock) -> list[str]:
    return [
        str(call.args[0])
        for call in logger.info.call_args_list
        if call.args and "apm export-patch" in str(call.args[0])
    ]


class TestInstallPipelineHint:
    def test_project_install_shows_hint_once(self, tmp_path: Path) -> None:
        logger, counter = _run_pipeline(tmp_path, 3, scope=InstallScope.PROJECT)
        assert _hint_calls(logger) == [local_edit_hint(3, installing=True)]
        _, _, keys = counter.call_args.args
        assert set(keys) == {"org/pkg"}

    def test_no_edits_no_hint(self, tmp_path: Path) -> None:
        logger, _ = _run_pipeline(tmp_path, 0, scope=InstallScope.PROJECT)
        assert _hint_calls(logger) == []

    def test_global_scope_skips_check(self, tmp_path: Path) -> None:
        logger, counter = _run_pipeline(tmp_path, 3, scope=InstallScope.USER)
        assert _hint_calls(logger) == []
        counter.assert_not_called()

    def test_lockfile_only_skips_check(self, tmp_path: Path) -> None:
        logger, counter = _run_pipeline(tmp_path, 3, scope=InstallScope.PROJECT, lockfile_only=True)
        assert _hint_calls(logger) == []
        counter.assert_not_called()


class TestRenderingChangeIsNotALocalEdit:
    """Drift whose bytes still match the lock is a rendering change (#3654)."""

    def _findings(self, tmp_path: Path) -> tuple[LockFile, list[DriftFinding]]:
        lock = _lockfile(
            tmp_path,
            **{".github/a.md": (_ORIGINAL, _ORIGINAL), ".github/b.md": (_ORIGINAL, _EDITED)},
        )
        key = next(iter(lock.dependencies))
        return lock, [
            DriftFinding(path=".github/a.md", kind="modified", package=key),
            DriftFinding(path=".github/b.md", kind="modified", package=key),
        ]

    def test_unedited_paths_match_the_locked_hash(self, tmp_path: Path) -> None:
        lock, findings = self._findings(tmp_path)
        assert unedited_modified_paths(findings, lock, tmp_path) == {".github/a.md"}
        assert unedited_modified_paths(findings, lock, None) == frozenset()
        assert unedited_modified_paths(findings, None, tmp_path) == frozenset()

    def test_drift_text_separates_edits_from_rendering_changes(self, tmp_path: Path) -> None:
        lock, findings = self._findings(tmp_path)
        out = render_drift_text(findings, lockfile=lock, project_root=tmp_path)
        assert local_edit_hint(1) in out
        assert rendering_change_hint(1) in out
        assert rendering_change_hint(1).isascii()

    def test_rendering_change_alone_gets_no_export_hint(self, tmp_path: Path) -> None:
        lock, findings = self._findings(tmp_path)
        out = render_drift_text(findings[:1], lockfile=lock, project_root=tmp_path)
        assert "apm export-patch" not in out
        assert rendering_change_hint(1) in out

    def test_without_project_root_every_modified_finding_is_exportable(
        self, tmp_path: Path
    ) -> None:
        lock, findings = self._findings(tmp_path)
        out = render_drift_text(findings, lockfile=lock)
        assert local_edit_hint(2) in out
        assert "renders them differently" not in out
