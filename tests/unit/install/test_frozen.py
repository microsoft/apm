"""Unit tests for ``InstallService.enforce_frozen``.

Issue: https://github.com/microsoft/apm/issues/1203 (P0).
The no-rewrite half (req-lk-006) is issue #2379.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile, suppress_lockfile_writes
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService, _outermost
from apm_cli.models.dependency.reference import DependencyReference


def _build_lockfile(deps: list[LockedDependency]) -> LockFile:
    lock = LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )
    for dep in deps:
        lock.add_dependency(dep)
    return lock


def _write_lockfile(project_dir: Path, deps: list[LockedDependency]) -> None:
    (project_dir / "apm.lock.yaml").write_text(_build_lockfile(deps).to_yaml())


def _write_apm_yml(project_dir: Path) -> None:
    (project_dir / "apm.yml").write_text("name: test\nversion: 1.0.0\n")


def _make_request(*, project_dir: Path, manifest_deps: list[DependencyReference]) -> InstallRequest:
    pkg = MagicMock()
    pkg.package_path = project_dir / "apm.yml"
    pkg.get_apm_dependencies.return_value = manifest_deps
    pkg.get_dev_apm_dependencies.return_value = []
    return InstallRequest(apm_package=pkg, frozen=True)


class TestEnforceFrozen:
    def test_raises_when_lockfile_missing(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        req = _make_request(project_dir=tmp_path, manifest_deps=[])

        with pytest.raises(FrozenInstallError, match=r"requires apm\.lock\.yaml"):
            InstallService.enforce_frozen(req)

    def test_raises_when_manifest_dep_missing_from_lockfile(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [])
        dep = DependencyReference(repo_url="https://github.com/declared/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        with pytest.raises(FrozenInstallError, match="out of sync") as exc_info:
            InstallService.enforce_frozen(req)

        assert any("declared/r" in r for r in exc_info.value.reasons)

    def test_succeeds_when_lockfile_has_all_manifest_deps(self, tmp_path: Path):
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService.enforce_frozen(req)

    def test_orphan_lockfile_entries_dont_fail(self, tmp_path: Path):
        """Mirrors npm ci: extra lock entries are tolerated; only direct deps must be present."""
        _write_apm_yml(tmp_path)
        _write_lockfile(
            tmp_path,
            [
                LockedDependency(
                    repo_url="https://github.com/o/r",
                    resolved_ref="main",
                    resolved_commit="a" * 40,
                    depth=1,
                ),
                LockedDependency(
                    repo_url="https://github.com/orphan/r",
                    resolved_ref="main",
                    resolved_commit="b" * 40,
                    depth=1,
                ),
            ],
        )
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])

        InstallService.enforce_frozen(req)

    def test_mixed_project_rejects_stale_locked_mcp_state(self, tmp_path: Path):
        from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

        _write_apm_yml(tmp_path)
        locked_dep = LockedDependency(
            repo_url="https://github.com/o/r",
            resolved_ref="main",
            resolved_commit="a" * 40,
            depth=1,
        )
        _write_lockfile(tmp_path, [locked_dep])
        lock = LockFile.read(tmp_path / "apm.lock.yaml")
        assert lock is not None
        lock.mcp_servers = ["stale-mcp"]
        lock.mcp_configs = {"stale-mcp": {"name": "stale-mcp"}}
        lock.save(tmp_path / "apm.lock.yaml")
        dep = DependencyReference(repo_url="https://github.com/o/r")
        req = _make_request(project_dir=tmp_path, manifest_deps=[dep])
        req.apm_package.get_all_mcp_dependencies.return_value = []
        current = CurrentMcpConfigView(
            dependencies=(),
            configs={},
            provenance={},
            problems=(),
        )

        with (
            patch.object(CurrentMcpConfigView, "derive", return_value=current),
            pytest.raises(FrozenInstallError, match="out of sync") as exc_info,
        ):
            InstallService.enforce_frozen(req)

        assert any("stale-mcp" in reason for reason in exc_info.value.reasons)


def _locked(deployed: list[str]) -> LockedDependency:
    return LockedDependency(
        repo_url="https://github.com/o/r",
        resolved_ref="main",
        resolved_commit="a" * 40,
        depth=1,
        deployed_files=list(deployed),
        deployed_file_hashes={p: f"sha256:{'0' * 64}" for p in deployed if "." in Path(p).name},
    )


class TestSuppressLockfileWrites:
    """req-lk-006's "never written or rewritten" half, at the chokepoint."""

    def test_write_is_withheld_and_recorded(self, tmp_path: Path):
        path = tmp_path / "apm.lock.yaml"
        lock = _build_lockfile([_locked([".claude/skills/a/SKILL.md"])])

        with suppress_lockfile_writes() as withheld:
            lock.write(path)

        assert not path.exists(), "frozen mode must not create the lockfile"
        assert len(withheld) == 1
        assert ".claude/skills/a/SKILL.md" in withheld[0]

    def test_recorded_payload_is_a_snapshot_not_a_reference(self, tmp_path: Path):
        """Later mutation of the same object must not rewrite history."""
        lock = _build_lockfile([_locked([".claude/skills/a/SKILL.md"])])

        with suppress_lockfile_writes() as withheld:
            lock.write(tmp_path / "apm.lock.yaml")
            lock.add_dependency(
                LockedDependency(repo_url="https://github.com/late/r", resolved_ref="main")
            )

        assert "late/r" not in withheld[0]

    def test_writes_resume_after_the_context_exits(self, tmp_path: Path):
        path = tmp_path / "apm.lock.yaml"
        lock = _build_lockfile([])

        with suppress_lockfile_writes():
            pass
        lock.write(path)

        assert path.exists(), "suppression must not leak past its context"


class TestEnforceFrozenNoRewrite:
    """A frozen install that withheld a *different* lockfile must fail.

    Suppressing the write alone would satisfy req-lk-006's letter while
    leaving the project claiming less than it deploys -- the state that
    puts deployed files outside the audit's scanners (#2379).
    """

    def _request(self, tmp_path: Path, committed: list[str]) -> InstallRequest:
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [_locked(committed)])
        return _make_request(project_dir=tmp_path, manifest_deps=[])

    def test_no_withheld_writes_passes(self, tmp_path: Path):
        req = self._request(tmp_path, [".claude/skills/a", ".claude/skills/a/SKILL.md"])

        InstallService._enforce_frozen_no_rewrite(req, [])

    def test_equivalent_withheld_write_passes(self, tmp_path: Path):
        """A site that saves an unchanged lockfile must not fail the install."""
        deployed = [".claude/skills/a", ".claude/skills/a/SKILL.md"]
        req = self._request(tmp_path, deployed)
        same = _build_lockfile([_locked(deployed)]).to_yaml()

        InstallService._enforce_frozen_no_rewrite(req, [same])

    def test_provenance_only_difference_passes(self, tmp_path: Path):
        """Section 5.5: generated_at / apm_version MUST NOT affect equivalence.

        Otherwise every CI run on a newer CLI than the one that wrote the
        committed lockfile would fail --frozen.
        """
        deployed = [".claude/skills/a", ".claude/skills/a/SKILL.md"]
        req = self._request(tmp_path, deployed)
        newer = _build_lockfile([_locked(deployed)])
        newer.generated_at = "2099-12-31T23:59:59+00:00"
        newer.apm_version = "99.0.0"

        InstallService._enforce_frozen_no_rewrite(req, [newer.to_yaml()])

    def test_unrecorded_deployed_file_fails_and_is_named(self, tmp_path: Path):
        req = self._request(tmp_path, [".claude/skills/a", ".claude/skills/a/SKILL.md"])
        grown = _build_lockfile(
            [
                _locked(
                    [
                        ".claude/skills/a",
                        ".claude/skills/a/SKILL.md",
                        ".claude/skills/b",
                        ".claude/skills/b/SKILL.md",
                    ]
                )
            ]
        ).to_yaml()

        with pytest.raises(FrozenInstallError, match="does not record everything") as exc:
            InstallService._enforce_frozen_no_rewrite(req, [grown])

        # Only the outermost unclaimed path: the directory covers the file.
        assert exc.value.reasons == [
            "  - .claude/skills/b is deployed by this install but not recorded in apm.lock.yaml"
        ]
        assert "without --frozen" in exc.value.tip

    def test_dropped_claims_alone_do_not_fail(self, tmp_path: Path):
        """A narrower install (target filter, --only, removed dep) is tolerated.

        ``FrozenInstallError`` already documents that removed deps are
        allowed; only the under-recording direction is a frozen failure.
        Verified against microsoft/apm's own committed lockfile, which
        differs from a local install by 180 dropped ledger rows.
        """
        req = self._request(
            tmp_path, [".claude/skills/a", ".claude/skills/a/SKILL.md", ".claude/skills/gone"]
        )
        narrower = _build_lockfile(
            [_locked([".claude/skills/a", ".claude/skills/a/SKILL.md"])]
        ).to_yaml()

        InstallService._enforce_frozen_no_rewrite(req, [narrower])

    def test_non_ledger_difference_alone_does_not_fail(self, tmp_path: Path):
        """Dependency metadata churn is not an unrecorded deployed file."""
        deployed = [".claude/skills/a", ".claude/skills/a/SKILL.md"]
        req = self._request(tmp_path, deployed)
        extra_dep = _build_lockfile(
            [
                _locked(deployed),
                LockedDependency(
                    repo_url="https://github.com/new/dep", resolved_ref="main", depth=1
                ),
            ]
        ).to_yaml()

        InstallService._enforce_frozen_no_rewrite(req, [extra_dep])


class TestFrozenRunEndToEnd:
    """``run()`` wiring: withhold the write, then judge it -- but only when
    the pipeline got far enough for the verdict to mean anything."""

    def _project(self, tmp_path: Path) -> InstallRequest:
        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [_locked([".claude/skills/a", ".claude/skills/a/SKILL.md"])])
        return _make_request(project_dir=tmp_path, manifest_deps=[])

    def _under_recording_pipeline(self, tmp_path: Path, disposition):
        """A pipeline that deploys an unclaimed file, then reports *disposition*."""
        from apm_cli.models.results import InstallResult

        def _fake_pipeline(_pkg, **_kwargs):
            _build_lockfile(
                [_locked([".claude/skills/a", ".claude/skills/a/SKILL.md", ".claude/skills/b"])]
            ).write(tmp_path / "apm.lock.yaml")
            return InstallResult(disposition=disposition, exit_code=0)

        return _fake_pipeline

    def test_failed_pipeline_keeps_its_own_diagnostics(self, tmp_path: Path):
        """A failed install must not be re-reported as a lockfile complaint.

        Its diagnostics are the real cause and its ledger is unreliable, so
        the frozen verdict is skipped entirely.
        """
        from apm_cli.models.results import InstallDisposition

        request = self._project(tmp_path)
        committed = (tmp_path / "apm.lock.yaml").read_bytes()

        with patch(
            "apm_cli.install.pipeline.run_install_pipeline",
            new=self._under_recording_pipeline(tmp_path, InstallDisposition.FAILED),
        ):
            result = InstallService().run(request)

        assert result.disposition is InstallDisposition.FAILED
        assert (tmp_path / "apm.lock.yaml").read_bytes() == committed

    def test_successful_pipeline_is_judged_and_lockfile_untouched(self, tmp_path: Path):
        from apm_cli.models.results import InstallDisposition

        request = self._project(tmp_path)
        committed = (tmp_path / "apm.lock.yaml").read_bytes()

        with (
            patch(
                "apm_cli.install.pipeline.run_install_pipeline",
                new=self._under_recording_pipeline(tmp_path, InstallDisposition.SUCCESS),
            ),
            pytest.raises(FrozenInstallError, match="does not record everything"),
        ):
            InstallService().run(request)

        assert (tmp_path / "apm.lock.yaml").read_bytes() == committed

    def test_non_frozen_install_writes_normally(self, tmp_path: Path):
        """Suppression must be scoped to --frozen, not global."""
        from apm_cli.models.results import InstallDisposition

        _write_apm_yml(tmp_path)
        _write_lockfile(tmp_path, [_locked([".claude/skills/a"])])
        pkg = MagicMock()
        pkg.package_path = tmp_path / "apm.yml"
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        request = InstallRequest(apm_package=pkg, frozen=False)
        committed = (tmp_path / "apm.lock.yaml").read_bytes()

        with patch(
            "apm_cli.install.pipeline.run_install_pipeline",
            new=self._under_recording_pipeline(tmp_path, InstallDisposition.SUCCESS),
        ):
            InstallService().run(request)

        assert (tmp_path / "apm.lock.yaml").read_bytes() != committed


class TestOutermost:
    def test_drops_paths_covered_by_a_listed_ancestor(self):
        assert _outermost(
            {".claude/skills/b/SKILL.md", ".claude/skills/b", ".agents/skills/c"}
        ) == [".agents/skills/c", ".claude/skills/b"]

    def test_keeps_siblings_and_near_prefix_matches(self):
        """``a/b`` must not swallow ``a/bc`` -- only a real path segment counts."""
        assert _outermost({".claude/skills/b", ".claude/skills/bc"}) == [
            ".claude/skills/b",
            ".claude/skills/bc",
        ]
