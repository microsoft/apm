"""Phase-3w5 unit tests for apm_cli.install.phases.integrate.

Covers missing lines/branches identified in coverage-unit.json:
- _resolve_download_strategy: update_refs lockfile SHA path (lines 65-67)
- _resolve_download_strategy: git repo check + content hash fallback (107-141)
- _resolve_download_strategy: content hash mismatch (178-183)
- _resolve_download_strategy: registry enforce_only skip (196-197)
- _integrate_root_project: exception path (297-309)
- _check_cowork_caps: count cap warning, size cap warning (353-373)
- run(): callback_failure skip, alias path, direct dep failure with diagnostics (429-493)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from apm_cli.install.context import InstallContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path: Path) -> InstallContext:
    (tmp_path / "apm.yml").write_text(yaml.safe_dump({"name": "testapp"}))
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    ctx.logger = None
    ctx.scope = None
    ctx.targets = []
    ctx.force = False
    ctx.update_refs = False
    ctx.only_packages = None
    ctx.verbose = False
    ctx.existing_lockfile = None
    ctx.all_apm_deps = []
    ctx.deps_to_install = []
    ctx.apm_modules_dir = tmp_path / "apm_modules"
    ctx.apm_modules_dir.mkdir(exist_ok=True)
    ctx.diagnostics = MagicMock()
    ctx.diagnostics.error_count = 0
    ctx.downloader = MagicMock()
    ctx.registry_config = None
    ctx.direct_dep_failed = False
    ctx.tui = None
    ctx.ref_resolver = None
    return ctx


def _make_dep_ref(key="org/pkg", is_local=False, alias=None, reference=None):
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.get_identity.return_value = key
    dep.get_display_name.return_value = key
    dep.get_install_path.side_effect = lambda d: d / key
    dep.alias = alias
    dep.is_local = is_local
    dep.local_path = None if not is_local else "./local"
    dep.reference = reference
    dep.repo_url = key
    return dep


# ---------------------------------------------------------------------------
# _resolve_download_strategy -- lockfile SHA path
# ---------------------------------------------------------------------------


class TestResolveDownloadStrategy:
    def test_already_exists_skip_download(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        dep_ref = _make_dep_ref()
        install_path = tmp_path / "org" / "pkg"
        install_path.mkdir(parents=True)

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        # Path exists, is_cacheable=False (no resolved_ref), already_resolved=False,
        # lockfile_match=False -> skip_download=False (no matching criteria)
        assert not skip_download

    def test_update_refs_with_lockfile_sha_triggers_resolution(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        ctx.update_refs = True

        locked_dep = MagicMock()
        locked_dep.resolved_commit = "abc123"
        locked_dep.content_hash = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref()
        install_path = tmp_path / "pkg"

        mock_resolved = MagicMock()
        mock_resolved.resolved_commit = "abc123"
        ctx.downloader.resolve_git_reference.return_value = mock_resolved

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, _skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        ctx.downloader.resolve_git_reference.assert_called()

    def test_content_hash_mismatch_forces_redownload(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        logger = MagicMock()
        ctx.logger = logger

        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.content_hash = "deadbeef"
        locked_dep.registry_prefix = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref()
        # Mark already resolved so skip_download starts True
        ctx.callback_downloaded["org/pkg"] = "sha1"

        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
            patch(
                "apm_cli.utils.content_hash.verify_package_hash",
                return_value=False,
            ),
            patch("apm_cli.utils.path_security.safe_rmtree"),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download
        logger.progress.assert_called()

    def test_registry_enforce_only_skips_cached(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)

        registry_cfg = MagicMock()
        registry_cfg.enforce_only = True
        ctx.registry_config = registry_cfg

        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.content_hash = None
        locked_dep.registry_prefix = None  # Not from registry

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref()
        dep_ref.is_local = False
        # Mark already resolved so skip_download starts True
        ctx.callback_downloaded["org/pkg"] = "sha1"

        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download  # Forced False by registry enforce_only

    def test_no_lockfile_no_reference_no_resolve(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        dep_ref = _make_dep_ref(reference=None)
        install_path = tmp_path / "pkg"

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            resolved_ref, _skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert resolved_ref is None
        ctx.downloader.resolve_git_reference.assert_not_called()

    def test_unpinned_git_dep_skips_when_on_disk_hash_matches(self, tmp_path: Path) -> None:
        """Issue #1548: when a git dep has no resolved_commit (e.g. ADO partial-clone
        fallback couldn't pin a SHA) but the lockfile recorded a content_hash, the
        second install must NOT re-download if the on-disk content still hashes to
        the recorded value.  Previously the cache-skip was bypassed and the fresh
        download could trip the supply-chain mismatch check with a false positive.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)

        locked_dep = MagicMock()
        # No pinned commit -- mirrors the partial-clone-fallback path.
        locked_dep.resolved_commit = None
        locked_dep.content_hash = "deadbeef"
        # Not a registry dep.
        locked_dep.source = "git"
        locked_dep.registry_prefix = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference=None)
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
            patch(
                "apm_cli.utils.content_hash.verify_package_hash",
                return_value=True,
            ),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert skip_download, (
            "unpinned git dep with on-disk content matching lockfile content_hash "
            "must skip re-download (issue #1548)"
        )

    def test_unpinned_git_dep_reuses_preverified_content_hash(self, tmp_path: Path) -> None:
        """When the download phase already verified content_hash, the sequential
        strategy must not hash the tree again before choosing the cached path.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        ctx.content_hash_verified_deps.add("org/pkg")

        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.content_hash = "deadbeef"
        locked_dep.source = "git"
        locked_dep.registry_prefix = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference=None)
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(True, False)),
            patch(
                "apm_cli.utils.content_hash.verify_package_hash",
                side_effect=AssertionError("hash tree should not be recomputed"),
            ),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert skip_download

    def test_unpinned_git_dep_redownloads_when_on_disk_hash_differs(self, tmp_path: Path) -> None:
        """Negative gate: the unpinned-skip branch must NOT mask a real mismatch.
        When on-disk content does not hash to the lockfile-recorded value, the
        cache-skip must NOT fire so the normal fresh-download / supply-chain
        verification path still runs.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)

        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.content_hash = "deadbeef"
        locked_dep.source = "git"
        locked_dep.registry_prefix = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference=None)
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
            patch(
                "apm_cli.utils.content_hash.verify_package_hash",
                return_value=False,
            ),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download, (
            "real on-disk hash mismatch must NOT be masked by the unpinned-skip "
            "branch -- the normal fresh-download flow must run so the supply-chain "
            "verification can detect tampering"
        )

    def test_unpinned_git_dep_no_skip_when_lockfile_lacks_content_hash(
        self, tmp_path: Path
    ) -> None:
        """A locked dep with neither resolved_commit nor content_hash provides no
        integrity anchor -- we have nothing to compare on-disk content against and
        must not skip the download.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)

        locked_dep = MagicMock()
        locked_dep.resolved_commit = None
        locked_dep.content_hash = None
        locked_dep.source = "git"
        locked_dep.registry_prefix = None

        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference=None)
        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download

    # --- Issue #551: skip re-download when callback SHA matches remote SHA ---

    def test_callback_sha_matches_remote_skips_redownload(self, tmp_path: Path) -> None:
        """Issue #551: when update_refs=True and the BFS callback already downloaded
        the dep with a SHA that matches the current remote ref, skip_download must
        be True -- the callback bytes are already correct, no re-download needed.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        ctx.update_refs = True

        # Stale lockfile -- lockfile SHA differs from remote so lockfile_match=False
        # without the fix (the legacy path would re-download).
        locked_dep = MagicMock()
        locked_dep.resolved_commit = "stale_sha"
        locked_dep.content_hash = None
        locked_dep.source = "git"
        locked_dep.registry_prefix = None
        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference="main")
        # Callback already downloaded this dep with the new remote SHA.
        ctx.callback_downloaded["org/pkg"] = "new_sha"

        install_path = tmp_path / "pkg"
        install_path.mkdir()

        # Remote ref resolves to same SHA the callback captured.
        mock_resolved = MagicMock()
        mock_resolved.resolved_commit = "new_sha"
        mock_resolved.ref_type = None  # not a tag/commit -> is_cacheable=False
        ctx.downloader.resolve_git_reference.return_value = mock_resolved

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert skip_download, (
            "issue #551: callback already downloaded the dep with the correct SHA -- "
            "re-download must be skipped when callback SHA matches remote SHA"
        )

    def test_callback_sha_mismatch_does_not_skip_redownload(self, tmp_path: Path) -> None:
        """Issue #551 negative gate: when callback SHA differs from the resolved
        remote SHA, the dep must be re-downloaded (content may have changed between
        BFS and sequential loop invocations).
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        ctx.update_refs = True

        locked_dep = MagicMock()
        locked_dep.resolved_commit = "stale_sha"
        locked_dep.content_hash = None
        locked_dep.source = "git"
        locked_dep.registry_prefix = None
        lf = MagicMock()
        lf.get_dependency.return_value = locked_dep
        ctx.existing_lockfile = lf

        dep_ref = _make_dep_ref(reference="main")
        # Callback captured sha_A but the remote has already moved to sha_B.
        ctx.callback_downloaded["org/pkg"] = "sha_A"

        install_path = tmp_path / "pkg"
        install_path.mkdir()

        mock_resolved = MagicMock()
        mock_resolved.resolved_commit = "sha_B"  # differs from callback SHA
        mock_resolved.ref_type = None
        ctx.downloader.resolve_git_reference.return_value = mock_resolved

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download, (
            "issue #551: callback SHA differs from remote SHA -- "
            "re-download must proceed so fresh content is materialized"
        )

    def test_callback_sha_match_no_resolved_ref_does_not_skip(self, tmp_path: Path) -> None:
        """Issue #551 boundary: when resolve_git_reference fails or returns no commit,
        we cannot confirm the remote SHA -- must not skip.
        """
        from apm_cli.install.phases.integrate import _resolve_download_strategy

        ctx = _make_ctx(tmp_path)
        ctx.update_refs = True

        dep_ref = _make_dep_ref(reference=None)  # no reference -> no resolve call
        ctx.callback_downloaded["org/pkg"] = "some_sha"

        install_path = tmp_path / "pkg"
        install_path.mkdir()

        with (
            patch("apm_cli.drift.detect_ref_change", return_value=False),
            patch("apm_cli.install.phases.heal.run_heal_chain", return_value=(False, False)),
        ):
            _resolved_ref, skip_download, _locked, _changed = _resolve_download_strategy(
                ctx, dep_ref, install_path
            )

        assert not skip_download, (
            "issue #551: no resolved_ref means no remote SHA to compare -- "
            "skip must not fire on SHA alone"
        )


# ---------------------------------------------------------------------------
# _integrate_root_project
# ---------------------------------------------------------------------------


class TestIntegrateRootProject:
    def test_no_targets_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _integrate_root_project

        ctx = _make_ctx(tmp_path)
        ctx.root_has_local_primitives = True
        ctx.targets = []  # No targets

        result = _integrate_root_project(ctx)
        assert result is None

    def test_no_local_primitives_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _integrate_root_project

        ctx = _make_ctx(tmp_path)
        ctx.root_has_local_primitives = False
        ctx.targets = [MagicMock()]

        result = _integrate_root_project(ctx)
        assert result is None

    def test_exception_in_integrate_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _integrate_root_project

        ctx = _make_ctx(tmp_path)
        ctx.root_has_local_primitives = True
        ctx.targets = [MagicMock()]
        ctx.all_apm_deps = []
        logger = MagicMock()
        ctx.logger = logger

        ctx.managed_files = set()
        ctx.old_local_deployed = []
        ctx.package_deployed_files = {}
        ctx.integrators = {
            "prompt": MagicMock(),
            "agent": MagicMock(),
            "skill": MagicMock(),
            "instruction": MagicMock(),
            "command": MagicMock(),
            "hook": MagicMock(),
        }

        with (
            patch(
                "apm_cli.install.services.integrate_local_content",
                side_effect=RuntimeError("integration boom"),
            ),
            patch(
                "apm_cli.integration.base_integrator.BaseIntegrator.normalize_managed_files",
                return_value=set(),
            ),
        ):
            result = _integrate_root_project(ctx)

        assert result is None
        ctx.diagnostics.error.assert_called()
        logger.error.assert_called()

    def test_exception_no_logger_still_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _integrate_root_project

        ctx = _make_ctx(tmp_path)
        ctx.root_has_local_primitives = True
        ctx.targets = [MagicMock()]
        ctx.all_apm_deps = [MagicMock()]  # Has deps - don't log to logger
        ctx.logger = None

        ctx.managed_files = set()
        ctx.old_local_deployed = []
        ctx.package_deployed_files = {}
        ctx.integrators = {
            "prompt": MagicMock(),
            "agent": MagicMock(),
            "skill": MagicMock(),
            "instruction": MagicMock(),
            "command": MagicMock(),
            "hook": MagicMock(),
        }

        with (
            patch(
                "apm_cli.install.services.integrate_local_content",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "apm_cli.integration.base_integrator.BaseIntegrator.normalize_managed_files",
                return_value=set(),
            ),
        ):
            result = _integrate_root_project(ctx)

        assert result is None


# ---------------------------------------------------------------------------
# _check_cowork_caps
# ---------------------------------------------------------------------------


class TestCheckCoworkCaps:
    def test_no_targets_skips(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _check_cowork_caps

        ctx = _make_ctx(tmp_path)
        ctx.targets = []

        _check_cowork_caps(ctx)  # Should not raise

    def test_no_cowork_target_skips(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _check_cowork_caps

        ctx = _make_ctx(tmp_path)
        t = MagicMock()
        t.name = "other-target"
        t.resolved_deploy_root = None
        ctx.targets = [t]

        _check_cowork_caps(ctx)  # Should not raise

    def test_count_cap_warning(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _COWORK_MAX_SKILLS, _check_cowork_caps

        ctx = _make_ctx(tmp_path)
        logger = MagicMock()
        ctx.logger = logger

        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()

        # Create more than _COWORK_MAX_SKILLS skill directories
        for i in range(_COWORK_MAX_SKILLS + 2):
            skill_dir = cowork_root / f"skill-{i:03d}"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"# Skill {i}")

        t = MagicMock()
        t.name = "copilot-cowork"
        t.resolved_deploy_root = cowork_root
        ctx.targets = [t]

        _check_cowork_caps(ctx)

        logger.warning.assert_called()

    def test_size_cap_warning(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _COWORK_MAX_SKILL_SIZE, _check_cowork_caps

        ctx = _make_ctx(tmp_path)
        logger = MagicMock()
        ctx.logger = logger

        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()

        skill_dir = cowork_root / "big-skill"
        skill_dir.mkdir()
        big_skill = skill_dir / "SKILL.md"
        big_skill.write_bytes(b"x" * (_COWORK_MAX_SKILL_SIZE + 1))

        t = MagicMock()
        t.name = "copilot-cowork"
        t.resolved_deploy_root = cowork_root
        ctx.targets = [t]

        _check_cowork_caps(ctx)

        logger.warning.assert_called()

    def test_cowork_root_not_dir_skips(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import _check_cowork_caps

        ctx = _make_ctx(tmp_path)
        t = MagicMock()
        t.name = "copilot-cowork"
        t.resolved_deploy_root = tmp_path / "nonexistent_cowork"
        ctx.targets = [t]

        _check_cowork_caps(ctx)  # Should not raise


# ---------------------------------------------------------------------------
# run() -- main integration loop paths
# ---------------------------------------------------------------------------


class TestRunIntegrationLoop:
    def _make_full_ctx(self, tmp_path: Path) -> InstallContext:
        ctx = _make_ctx(tmp_path)
        ctx.pre_downloaded_keys = set()
        ctx.installed_count = 0
        ctx.unpinned_count = 0
        ctx.total_prompts_integrated = 0
        ctx.total_agents_integrated = 0
        ctx.total_skills_integrated = 0
        ctx.total_sub_skills_promoted = 0
        ctx.total_instructions_integrated = 0
        ctx.total_commands_integrated = 0
        ctx.total_hooks_integrated = 0
        ctx.total_links_resolved = 0
        ctx.root_has_local_primitives = False
        ctx.package_deployed_files = {}
        ctx.package_types = {}
        ctx.package_hashes = {}
        ctx.installed_packages = []
        ctx.managed_files = set()
        ctx.old_local_deployed = []
        return ctx

    def test_callback_failure_skips_dep(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import run

        ctx = self._make_full_ctx(tmp_path)
        logger = MagicMock()
        ctx.logger = logger

        dep = _make_dep_ref("org/pkg")
        ctx.deps_to_install = [dep]
        ctx.callback_failures = {"org/pkg"}

        with patch("apm_cli.install.phases.integrate._integrate_root_project", return_value=None):
            run(ctx)

        logger.verbose_detail.assert_called()
        assert ctx.installed_count == 0

    def test_alias_install_path_used(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import run

        ctx = self._make_full_ctx(tmp_path)
        dep = _make_dep_ref("org/pkg", alias="my-alias")
        ctx.deps_to_install = [dep]
        ctx.callback_failures = set()

        mock_source = MagicMock()
        mock_deltas = {
            "installed": 1,
            "unpinned": 0,
            "prompts": 0,
            "agents": 0,
            "skills": 0,
            "sub_skills": 0,
            "instructions": 0,
            "commands": 0,
            "hooks": 0,
            "links_resolved": 0,
        }

        with (
            patch(
                "apm_cli.install.phases.integrate.make_dependency_source", return_value=mock_source
            ),
            patch(
                "apm_cli.install.phases.integrate.run_integration_template",
                return_value=mock_deltas,
            ),
            patch("apm_cli.install.phases.integrate._integrate_root_project", return_value=None),
        ):
            run(ctx)

        assert ctx.installed_count == 1

    def test_direct_dep_failure_with_diagnostics(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import run

        ctx = self._make_full_ctx(tmp_path)
        dep = _make_dep_ref("org/pkg")
        ctx.deps_to_install = [dep]
        ctx.all_apm_deps = [dep]  # Make it a direct dep
        ctx.callback_failures = set()

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.install.phases.integrate._resolve_download_strategy",
                return_value=(None, False, None, False),
            ),
            patch(
                "apm_cli.install.phases.integrate.make_dependency_source", return_value=mock_source
            ),
            patch("apm_cli.install.phases.integrate.run_integration_template", return_value=None),
            patch("apm_cli.install.phases.integrate._integrate_root_project", return_value=None),
        ):
            run(ctx)

        assert ctx.direct_dep_failed
        ctx.diagnostics.error.assert_called()

    def test_direct_dep_failure_no_diagnostics_uses_logger(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import run

        ctx = self._make_full_ctx(tmp_path)
        logger = MagicMock()
        ctx.logger = logger
        ctx.diagnostics = None

        dep = _make_dep_ref("org/pkg")
        ctx.deps_to_install = [dep]
        ctx.all_apm_deps = [dep]  # Make it a direct dep
        ctx.callback_failures = set()

        mock_source = MagicMock()

        with (
            patch(
                "apm_cli.install.phases.integrate._resolve_download_strategy",
                return_value=(None, False, None, False),
            ),
            patch(
                "apm_cli.install.phases.integrate.make_dependency_source", return_value=mock_source
            ),
            patch("apm_cli.install.phases.integrate.run_integration_template", return_value=None),
            patch("apm_cli.install.phases.integrate._integrate_root_project", return_value=None),
        ):
            run(ctx)

        assert ctx.direct_dep_failed
        logger.error.assert_called()

    def test_root_deltas_accumulated(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.integrate import run

        ctx = self._make_full_ctx(tmp_path)
        ctx.deps_to_install = []
        ctx.callback_failures = set()

        root_deltas = {
            "installed": 1,
            "prompts": 2,
            "agents": 0,
            "skills": 1,
            "sub_skills": 0,
            "instructions": 0,
            "commands": 0,
            "hooks": 0,
            "links_resolved": 0,
        }

        with patch(
            "apm_cli.install.phases.integrate._integrate_root_project",
            return_value=root_deltas,
        ):
            run(ctx)

        assert ctx.installed_count == 1
        assert ctx.total_prompts_integrated == 2
