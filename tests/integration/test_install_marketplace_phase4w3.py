"""Integration tests for install + marketplace modules -- phase 4 wave 3.

Targets:
- apm_cli.install.heals.buggy_lockfile_recovery (remaining integration paths)
- apm_cli.install.phases.finalize (stats, unpinned warnings, bare success)
- apm_cli.install.helpers.security_scan (_pre_deploy_security_scan)
- apm_cli.install.summary (render_post_install_summary hard-fail + errors)
- apm_cli.install.errors (FrozenInstallError, PolicyViolationError, AuthenticationError)
- apm_cli.install.gitlab_resolver (_try_resolve_gitlab_direct_shorthand)
- apm_cli.marketplace.shadow_detector (detect_shadows)
- apm_cli.marketplace._io (atomic_write)
- apm_cli.marketplace._shared (iter_semver_tags)
- apm_cli.marketplace.output_profiles (_validate_profile edge cases)
- apm_cli.marketplace.init_template (render_marketplace_block)
- apm_cli.compilation.constitution (read_constitution caching / OSError)
- apm_cli.deps._shared (_validate_and_load_package)
- apm_cli.integration.coverage (check_primitive_coverage edge cases)
- apm_cli.adapters.package_manager.base (abstract methods enforcement)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# install/heals/buggy_lockfile_recovery -- integration-level coverage
# ---------------------------------------------------------------------------


class TestBuggyLockfileRecoveryIntegration:
    """Integration paths for BuggyLockfileRecoveryHeal via the heal chain."""

    def _make_hctx(
        self,
        *,
        lockfile_match: bool = True,
        lockfile_match_via_content_hash_only: bool = True,
        update_refs: bool = False,
        apm_version: str = "0.12.2",
        has_lockfile: bool = True,
        ref_type_branch: bool = True,
    ):
        from apm_cli.install.heals.base import HealContext
        from apm_cli.models.apm_package import GitReferenceType

        rr = MagicMock()
        rr.ref_type = GitReferenceType.BRANCH if ref_type_branch else GitReferenceType.TAG

        existing_lockfile = None
        if has_lockfile:
            existing_lockfile = MagicMock()
            existing_lockfile.apm_version = apm_version

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "github.com/owner/myrepo"

        return HealContext(
            dep_ref=dep_ref,
            package_key="github.com/owner/myrepo",
            resolved_ref=rr,
            existing_lockfile=existing_lockfile,
            lockfile_match=lockfile_match,
            lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
            update_refs=update_refs,
        )

    def test_applies_buggy_version_branch(self) -> None:
        from apm_cli.install.heals.buggy_lockfile_recovery import BuggyLockfileRecoveryHeal

        heal = BuggyLockfileRecoveryHeal()
        ctx = self._make_hctx(apm_version="0.12.0")
        assert heal.applies(ctx) is True

    def test_execute_sets_bypass_and_warn(self) -> None:
        from apm_cli.install.heals.base import HealMessageLevel
        from apm_cli.install.heals.buggy_lockfile_recovery import BuggyLockfileRecoveryHeal

        heal = BuggyLockfileRecoveryHeal()
        ctx = self._make_hctx()
        heal.execute(ctx)

        assert ctx.lockfile_match is False
        assert ctx.ref_changed is True
        assert "github.com/owner/myrepo" in ctx.bypass_keys
        assert len(ctx.messages) == 1
        assert ctx.messages[0].level == HealMessageLevel.WARN
        assert "0.12.2" in ctx.messages[0].text

    def test_does_not_apply_resolved_ref_none(self) -> None:
        from apm_cli.install.heals.base import HealContext
        from apm_cli.install.heals.buggy_lockfile_recovery import BuggyLockfileRecoveryHeal

        dep_ref = MagicMock()
        dep_ref.get_unique_key.return_value = "k"
        existing_lockfile = MagicMock()
        existing_lockfile.apm_version = "0.12.2"

        ctx = HealContext(
            dep_ref=dep_ref,
            package_key="k",
            resolved_ref=None,  # no resolved ref
            existing_lockfile=existing_lockfile,
            lockfile_match=True,
            lockfile_match_via_content_hash_only=True,
            update_refs=False,
        )
        heal = BuggyLockfileRecoveryHeal()
        assert heal.applies(ctx) is False


# ---------------------------------------------------------------------------
# install/phases/finalize -- stats and unpinned warnings
# ---------------------------------------------------------------------------


class TestFinalizePhase:
    """run() in finalize phase."""

    def _make_ctx(self, **kwargs):
        from apm_cli.install.context import InstallContext
        from apm_cli.utils.diagnostics import DiagnosticCollector

        defaults = {
            "project_root": Path("/tmp/proj"),
            "apm_dir": Path("/tmp/proj/.apm"),
            "installed_count": 0,
            "total_prompts_integrated": 0,
            "total_agents_integrated": 0,
            "total_links_resolved": 0,
            "total_commands_integrated": 0,
            "total_hooks_integrated": 0,
            "total_instructions_integrated": 0,
            "unpinned_count": 0,
            "installed_packages": [],
            "diagnostics": DiagnosticCollector(),
            "package_types": {},
            "logger": None,
        }
        defaults.update(kwargs)
        return InstallContext(**defaults)

    def test_bare_success_emitted_without_logger(self) -> None:
        from apm_cli.install.phases.finalize import run

        ctx = self._make_ctx(installed_count=2, logger=None)
        with patch("apm_cli.commands.install._rich_success") as mock_success:
            result = run(ctx)
        mock_success.assert_called_once()
        assert result.installed_count == 2

    def test_no_bare_success_with_logger(self) -> None:
        from apm_cli.install.phases.finalize import run

        mock_logger = MagicMock()
        ctx = self._make_ctx(installed_count=1, logger=mock_logger)
        with patch("apm_cli.commands.install._rich_success") as mock_success:
            run(ctx)
        mock_success.assert_not_called()

    def test_verbose_links_logged(self) -> None:
        from apm_cli.install.phases.finalize import run

        mock_logger = MagicMock()
        ctx = self._make_ctx(total_links_resolved=5, logger=mock_logger)
        run(ctx)
        mock_logger.verbose_detail.assert_any_call("Resolved 5 context file links")

    def test_verbose_commands_logged(self) -> None:
        from apm_cli.install.phases.finalize import run

        mock_logger = MagicMock()
        ctx = self._make_ctx(total_commands_integrated=3, logger=mock_logger)
        run(ctx)
        mock_logger.verbose_detail.assert_any_call("Integrated 3 command(s)")

    def test_verbose_hooks_logged(self) -> None:
        from apm_cli.install.phases.finalize import run

        mock_logger = MagicMock()
        ctx = self._make_ctx(total_hooks_integrated=2, logger=mock_logger)
        run(ctx)
        mock_logger.verbose_detail.assert_any_call("Integrated 2 hook(s)")

    def test_verbose_instructions_logged(self) -> None:
        from apm_cli.install.phases.finalize import run

        mock_logger = MagicMock()
        ctx = self._make_ctx(total_instructions_integrated=4, logger=mock_logger)
        run(ctx)
        mock_logger.verbose_detail.assert_any_call("Integrated 4 instruction(s)")

    def test_unpinned_count_warns_with_names(self) -> None:
        from apm_cli.install.phases.finalize import run
        from apm_cli.utils.diagnostics import DiagnosticCollector

        dep_ref = MagicMock()
        dep_ref.reference = None  # unpinned (no reference)
        dep_ref.repo_url = "github.com/owner/dep1"

        pkg = MagicMock()
        pkg.dep_ref = dep_ref

        diag = DiagnosticCollector()
        ctx = self._make_ctx(
            unpinned_count=1,
            installed_packages=[pkg],
            diagnostics=diag,
        )
        run(ctx)
        # Should have a warning about unpinned
        assert diag.has_diagnostics

    def test_unpinned_count_warns_without_names(self) -> None:
        from apm_cli.install.phases.finalize import run
        from apm_cli.utils.diagnostics import DiagnosticCollector

        pkg = MagicMock()
        pkg.dep_ref = None  # no dep_ref => no name

        diag = DiagnosticCollector()
        ctx = self._make_ctx(
            unpinned_count=2,
            installed_packages=[pkg],
            diagnostics=diag,
        )
        run(ctx)
        assert diag.has_diagnostics

    def test_unpinned_more_than_5_shows_and_more(self) -> None:
        from apm_cli.install.phases.finalize import run
        from apm_cli.utils.diagnostics import DiagnosticCollector

        packages = []
        for i in range(7):
            dep_ref = MagicMock()
            dep_ref.reference = None
            dep_ref.repo_url = f"github.com/owner/dep{i}"
            pkg = MagicMock()
            pkg.dep_ref = dep_ref
            packages.append(pkg)

        diag = DiagnosticCollector()
        ctx = self._make_ctx(
            unpinned_count=7,
            installed_packages=packages,
            diagnostics=diag,
        )
        run(ctx)
        assert diag.has_diagnostics
        # Find the warning message in _diagnostics
        messages = [e.message for e in diag._diagnostics if "unpinned" in e.message]
        assert any("and 2 more" in m for m in messages)

    def test_returns_install_result(self) -> None:
        from apm_cli.install.phases.finalize import run
        from apm_cli.models.results import InstallResult

        ctx = self._make_ctx(installed_count=3, logger=MagicMock())
        result = run(ctx)
        assert isinstance(result, InstallResult)
        assert result.installed_count == 3


# ---------------------------------------------------------------------------
# install/helpers/security_scan
# ---------------------------------------------------------------------------


class TestPreDeploySecurityScan:
    """_pre_deploy_security_scan() -- all branches."""

    def test_no_findings_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
        from apm_cli.utils.diagnostics import DiagnosticCollector

        clean_verdict = MagicMock()
        clean_verdict.has_findings = False

        with patch("apm_cli.security.gate.SecurityGate.scan_files", return_value=clean_verdict):
            result = _pre_deploy_security_scan(
                tmp_path, DiagnosticCollector(), package_name="mypkg"
            )
        assert result is True

    def test_blocking_verdict_returns_false_with_logger(self, tmp_path: Path) -> None:
        from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
        from apm_cli.utils.diagnostics import DiagnosticCollector

        blocking_verdict = MagicMock()
        blocking_verdict.has_findings = True
        blocking_verdict.should_block = True

        mock_logger = MagicMock()

        with (
            patch("apm_cli.security.gate.SecurityGate.scan_files", return_value=blocking_verdict),
            patch("apm_cli.security.gate.SecurityGate.report"),
        ):
            result = _pre_deploy_security_scan(
                tmp_path,
                DiagnosticCollector(),
                package_name="evil-pkg",
                logger=mock_logger,
            )
        assert result is False
        mock_logger.error.assert_called_once()
        mock_logger.tree_item.assert_called()

    def test_blocking_verdict_returns_false_without_logger(self, tmp_path: Path) -> None:
        from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
        from apm_cli.utils.diagnostics import DiagnosticCollector

        blocking_verdict = MagicMock()
        blocking_verdict.has_findings = True
        blocking_verdict.should_block = True

        with (
            patch("apm_cli.security.gate.SecurityGate.scan_files", return_value=blocking_verdict),
            patch("apm_cli.security.gate.SecurityGate.report"),
        ):
            result = _pre_deploy_security_scan(tmp_path, DiagnosticCollector(), package_name="pkg")
        assert result is False

    def test_non_blocking_finding_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
        from apm_cli.utils.diagnostics import DiagnosticCollector

        warn_verdict = MagicMock()
        warn_verdict.has_findings = True
        warn_verdict.should_block = False

        with (
            patch("apm_cli.security.gate.SecurityGate.scan_files", return_value=warn_verdict),
            patch("apm_cli.security.gate.SecurityGate.report"),
        ):
            result = _pre_deploy_security_scan(tmp_path, DiagnosticCollector(), package_name="pkg")
        assert result is True

    def test_force_flag_passed_to_gate(self, tmp_path: Path) -> None:
        from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
        from apm_cli.utils.diagnostics import DiagnosticCollector

        clean_verdict = MagicMock()
        clean_verdict.has_findings = False

        with patch(
            "apm_cli.security.gate.SecurityGate.scan_files", return_value=clean_verdict
        ) as mock_scan:
            _pre_deploy_security_scan(
                tmp_path, DiagnosticCollector(), package_name="pkg", force=True
            )
        _, kwargs = mock_scan.call_args
        assert kwargs.get("force") is True


# ---------------------------------------------------------------------------
# install/errors
# ---------------------------------------------------------------------------


class TestInstallErrors:
    """Exception types in install/errors.py."""

    def test_frozen_install_error_with_reasons(self) -> None:
        from apm_cli.install.errors import FrozenInstallError

        err = FrozenInstallError("lockfile missing", reasons=["dep1", "dep2"])
        assert err.reasons == ["dep1", "dep2"]
        assert "lockfile missing" in str(err)

    def test_frozen_install_error_no_reasons(self) -> None:
        from apm_cli.install.errors import FrozenInstallError

        err = FrozenInstallError("no lockfile")
        assert err.reasons == []

    def test_policy_violation_error_default_attrs(self) -> None:
        from apm_cli.install.errors import PolicyViolationError

        err = PolicyViolationError("blocked by policy")
        assert err.audit_result is None
        assert err.policy_source == ""

    def test_policy_violation_error_with_attrs(self) -> None:
        from apm_cli.install.errors import PolicyViolationError

        audit = MagicMock()
        err = PolicyViolationError("blocked", audit_result=audit, policy_source="org:acme")
        assert err.audit_result is audit
        assert err.policy_source == "org:acme"

    def test_authentication_error_diagnostic_context(self) -> None:
        from apm_cli.install.errors import AuthenticationError

        err = AuthenticationError("auth failed", diagnostic_context="Check your PAT")
        assert err.diagnostic_context == "Check your PAT"
        assert "auth failed" in str(err)


# ---------------------------------------------------------------------------
# install/summary -- render_post_install_summary
# ---------------------------------------------------------------------------


class TestRenderPostInstallSummary:
    """render_post_install_summary() hard-fail and error-count paths."""

    def _make_logger(self):
        mock_logger = MagicMock()
        mock_logger.stale_cleaned_total = 0
        return mock_logger

    def test_no_diagnostics_calls_blank_line(self) -> None:
        from apm_cli.install.summary import render_post_install_summary

        logger = self._make_logger()
        with patch("apm_cli.install.summary._rich_blank_line") as mock_blank:
            render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=None,
                force=False,
            )
        mock_blank.assert_called_once()

    def test_critical_security_exits_without_force(self) -> None:
        from apm_cli.install.summary import render_post_install_summary

        logger = self._make_logger()
        diag = MagicMock()
        diag.has_diagnostics = False
        diag.has_critical_security = True
        diag.error_count = 0

        with (
            patch("apm_cli.install.summary._rich_blank_line"),
            pytest.raises(SystemExit) as exc_info,
        ):
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        assert exc_info.value.code == 1

    def test_critical_security_no_exit_with_force(self) -> None:
        from apm_cli.install.summary import render_post_install_summary

        logger = self._make_logger()
        diag = MagicMock()
        diag.has_diagnostics = False
        diag.has_critical_security = True
        diag.error_count = 0

        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=0,
                mcp_count=0,
                apm_diagnostics=diag,
                force=True,  # force suppresses exit
            )
        # No SystemExit

    def test_invalid_error_count_defaults_to_zero(self) -> None:
        from apm_cli.install.summary import render_post_install_summary

        logger = self._make_logger()
        diag = MagicMock()
        diag.has_diagnostics = False
        diag.has_critical_security = False
        diag.error_count = "not-a-number"

        with patch("apm_cli.install.summary._rich_blank_line"):
            render_post_install_summary(
                logger=logger,
                apm_count=1,
                mcp_count=0,
                apm_diagnostics=diag,
                force=False,
            )
        logger.install_summary.assert_called_once()
        call_kwargs = logger.install_summary.call_args[1]
        assert call_kwargs["errors"] == 0


# ---------------------------------------------------------------------------
# install/gitlab_resolver
# ---------------------------------------------------------------------------


class TestGitlabResolver:
    """_try_resolve_gitlab_direct_shorthand()."""

    def test_returns_none_for_non_gitlab_package(self) -> None:
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        auth = MagicMock()
        with patch(
            "apm_cli.models.apm_package.DependencyReference.split_gitlab_direct_shorthand_parts",
            return_value=None,
        ):
            result = _try_resolve_gitlab_direct_shorthand("github.com/owner/repo", auth)
        assert result is None

    def test_creates_auth_resolver_when_none(self) -> None:
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        with (
            patch(
                "apm_cli.models.apm_package.DependencyReference.split_gitlab_direct_shorthand_parts",
                return_value=None,
            ),
            patch("apm_cli.install.gitlab_resolver.AuthResolver") as mock_ar,
        ):
            _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", None)
        mock_ar.assert_called_once()

    def test_returns_candidate_when_package_valid(self) -> None:
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        mock_candidate = MagicMock()

        with (
            patch(
                "apm_cli.models.apm_package.DependencyReference.split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], None),
            ),
            patch(
                "apm_cli.models.apm_package.DependencyReference.iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=[("https://gitlab.com/owner/repo", "")],
            ),
            patch(
                "apm_cli.models.apm_package.DependencyReference.from_gitlab_shorthand_probe",
                return_value=mock_candidate,
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                return_value=True,
            ),
        ):
            result = _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock())
        assert result is mock_candidate

    def test_returns_none_when_no_candidate_valid(self) -> None:
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        with (
            patch(
                "apm_cli.models.apm_package.DependencyReference.split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], None),
            ),
            patch(
                "apm_cli.models.apm_package.DependencyReference.iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=[("https://gitlab.com/owner/repo", "")],
            ),
            patch(
                "apm_cli.models.apm_package.DependencyReference.from_gitlab_shorthand_probe",
                return_value=MagicMock(),
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                return_value=False,
            ),
        ):
            result = _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock())
        assert result is None


# ---------------------------------------------------------------------------
# marketplace/shadow_detector
# ---------------------------------------------------------------------------


class TestDetectShadows:
    """detect_shadows() -- registered marketplaces iteration."""

    def test_no_registered_marketplaces_returns_empty(self) -> None:
        from apm_cli.marketplace.shadow_detector import detect_shadows

        with patch(
            "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
            return_value=[],
        ):
            result = detect_shadows("myplugin", "acme")
        assert result == []

    def test_skips_primary_marketplace(self) -> None:
        from apm_cli.marketplace.shadow_detector import detect_shadows

        source = MagicMock()
        source.name = "acme"

        with patch(
            "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
            return_value=[source],
        ):
            result = detect_shadows("myplugin", "acme")
        assert result == []

    def test_returns_shadow_match_when_found(self) -> None:
        from apm_cli.marketplace.shadow_detector import ShadowMatch, detect_shadows

        source = MagicMock()
        source.name = "rival"

        plugin = MagicMock()
        plugin.name = "myplugin"

        manifest = MagicMock()
        manifest.find_plugin.return_value = plugin

        with (
            patch(
                "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
                return_value=[source],
            ),
            patch(
                "apm_cli.marketplace.shadow_detector.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            result = detect_shadows("myplugin", "acme")

        assert len(result) == 1
        assert isinstance(result[0], ShadowMatch)
        assert result[0].marketplace_name == "rival"
        assert result[0].plugin_name == "myplugin"

    def test_no_shadow_when_plugin_not_in_other_marketplace(self) -> None:
        from apm_cli.marketplace.shadow_detector import detect_shadows

        source = MagicMock()
        source.name = "rival"

        manifest = MagicMock()
        manifest.find_plugin.return_value = None

        with (
            patch(
                "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
                return_value=[source],
            ),
            patch(
                "apm_cli.marketplace.shadow_detector.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            result = detect_shadows("myplugin", "acme")
        assert result == []

    def test_exception_during_fetch_is_swallowed(self) -> None:
        from apm_cli.marketplace.shadow_detector import detect_shadows

        source = MagicMock()
        source.name = "flaky"

        with (
            patch(
                "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
                return_value=[source],
            ),
            patch(
                "apm_cli.marketplace.shadow_detector.fetch_or_cache",
                side_effect=RuntimeError("network error"),
            ),
        ):
            result = detect_shadows("myplugin", "acme")
        assert result == []

    def test_case_insensitive_primary_comparison(self) -> None:
        from apm_cli.marketplace.shadow_detector import detect_shadows

        source = MagicMock()
        source.name = "ACME"  # upper-case version of the primary

        with patch(
            "apm_cli.marketplace.shadow_detector.get_registered_marketplaces",
            return_value=[source],
        ):
            result = detect_shadows("myplugin", "acme")
        assert result == []


# ---------------------------------------------------------------------------
# marketplace/_io
# ---------------------------------------------------------------------------


class TestMarketplaceAtomicWrite:
    """atomic_write() -- error path."""

    def test_writes_content(self, tmp_path: Path) -> None:
        from apm_cli.marketplace._io import atomic_write

        target = tmp_path / "marketplace.json"
        atomic_write(target, '{"key": "value"}')
        assert target.read_text(encoding="utf-8") == '{"key": "value"}'

    def test_cleans_tmp_on_failure(self, tmp_path: Path) -> None:
        from apm_cli.marketplace._io import atomic_write

        target = tmp_path / "out.json"
        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write(target, "data")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_write_failure_original_preserved(self, tmp_path: Path) -> None:
        from apm_cli.marketplace._io import atomic_write

        target = tmp_path / "existing.json"
        target.write_text('{"old": true}', encoding="utf-8")

        with patch("os.replace", side_effect=OSError("fail")):
            with pytest.raises(OSError):
                atomic_write(target, '{"new": true}')

        assert target.read_text(encoding="utf-8") == '{"old": true}'


# ---------------------------------------------------------------------------
# marketplace/_shared
# ---------------------------------------------------------------------------


class TestIterSemverTags:
    """iter_semver_tags() -- tag filtering and semver parsing."""

    def _make_ref(self, name: str, sha: str = "abc12345"):
        ref = MagicMock()
        ref.name = name
        ref.sha = sha
        return ref

    def test_yields_valid_version_tag(self) -> None:
        import re

        from apm_cli.marketplace._shared import iter_semver_tags

        rx = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
        refs = [self._make_ref("refs/tags/v1.2.3", "deadbeef")]
        results = list(iter_semver_tags(refs, rx))
        assert len(results) == 1
        _sv, tag, sha = results[0]
        assert tag == "v1.2.3"
        assert sha == "deadbeef"

    def test_skips_non_tag_refs(self) -> None:
        import re

        from apm_cli.marketplace._shared import iter_semver_tags

        rx = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
        refs = [self._make_ref("refs/heads/main")]
        results = list(iter_semver_tags(refs, rx))
        assert results == []

    def test_skips_tags_not_matching_regex(self) -> None:
        import re

        from apm_cli.marketplace._shared import iter_semver_tags

        rx = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
        refs = [self._make_ref("refs/tags/release-abc")]
        results = list(iter_semver_tags(refs, rx))
        assert results == []

    def test_skips_invalid_semver(self) -> None:
        import re

        from apm_cli.marketplace._shared import iter_semver_tags

        rx = re.compile(r"^v(?P<version>.+)$")
        refs = [self._make_ref("refs/tags/v-not-a-version")]
        results = list(iter_semver_tags(refs, rx))
        assert results == []


# ---------------------------------------------------------------------------
# marketplace/output_profiles
# ---------------------------------------------------------------------------


class TestValidateOutputProfile:
    """_validate_profile() error branches."""

    def test_reserved_name_raises(self) -> None:
        from apm_cli.marketplace.output_profiles import MarketplaceOutputProfile, _validate_profile

        profile = MarketplaceOutputProfile(
            name="all",  # reserved
            config_attr="all",
            default_output="out.json",
            mapper="all",
            path_env_var="APM_MARKETPLACE_ALL_PATH",
        )
        with pytest.raises(ValueError, match="reserved"):
            _validate_profile(profile)

    def test_invalid_chars_in_name_raises(self) -> None:
        from apm_cli.marketplace.output_profiles import MarketplaceOutputProfile, _validate_profile

        profile = MarketplaceOutputProfile(
            name="my profile",  # space is invalid
            config_attr="my_profile",
            default_output="out.json",
            mapper="my_profile",
            path_env_var="APM_MARKETPLACE_MY_PROFILE_PATH",
        )
        with pytest.raises(ValueError, match="CLI-reserved"):
            _validate_profile(profile)

    def test_name_starting_with_dash_raises(self) -> None:
        from apm_cli.marketplace.output_profiles import MarketplaceOutputProfile, _validate_profile

        profile = MarketplaceOutputProfile(
            name="-badname",
            config_attr="badname",
            default_output="out.json",
            mapper="badname",
            path_env_var="APM_MARKETPLACE_BADNAME_PATH",
        )
        with pytest.raises(ValueError, match="CLI-reserved"):
            _validate_profile(profile)

    def test_bad_env_var_pattern_raises(self) -> None:
        from apm_cli.marketplace.output_profiles import MarketplaceOutputProfile, _validate_profile

        profile = MarketplaceOutputProfile(
            name="custom",
            config_attr="custom",
            default_output="out.json",
            mapper="custom",
            path_env_var="WRONG_ENV_VAR",  # doesn't match pattern
        )
        with pytest.raises(ValueError, match="expected"):
            _validate_profile(profile)

    def test_known_output_names(self) -> None:
        from apm_cli.marketplace.output_profiles import known_output_names

        names = known_output_names()
        assert "claude" in names
        assert "codex" in names


# ---------------------------------------------------------------------------
# marketplace/init_template
# ---------------------------------------------------------------------------


class TestInitTemplate:
    """render_marketplace_block() -- owner substitution."""

    def test_render_marketplace_block_custom_owner(self) -> None:
        from apm_cli.marketplace.init_template import render_marketplace_block

        result = render_marketplace_block(owner="my-org")
        assert "my-org" in result
        assert "marketplace:" in result

    def test_render_marketplace_block_default_owner(self) -> None:
        from apm_cli.marketplace.init_template import render_marketplace_block

        result = render_marketplace_block()
        assert "acme-org" in result


# ---------------------------------------------------------------------------
# compilation/constitution
# ---------------------------------------------------------------------------


class TestConstitution:
    """read_constitution() -- cache and OSError paths."""

    def setup_method(self):
        from apm_cli.compilation.constitution import clear_constitution_cache

        clear_constitution_cache()

    def test_reads_file_when_present(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constants import CONSTITUTION_RELATIVE_PATH
        from apm_cli.compilation.constitution import read_constitution

        constitution_path = tmp_path / CONSTITUTION_RELATIVE_PATH
        constitution_path.parent.mkdir(parents=True, exist_ok=True)
        constitution_path.write_text("# My Constitution\n", encoding="utf-8")

        result = read_constitution(tmp_path)
        assert result == "# My Constitution\n"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constitution import read_constitution

        result = read_constitution(tmp_path)
        assert result is None

    def test_result_is_cached(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constants import CONSTITUTION_RELATIVE_PATH
        from apm_cli.compilation.constitution import read_constitution

        constitution_path = tmp_path / CONSTITUTION_RELATIVE_PATH
        constitution_path.parent.mkdir(parents=True, exist_ok=True)
        constitution_path.write_text("cached content", encoding="utf-8")

        result1 = read_constitution(tmp_path)
        # Remove the file after first read
        constitution_path.unlink()
        result2 = read_constitution(tmp_path)

        assert result1 == result2 == "cached content"

    def test_os_error_returns_none(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constants import CONSTITUTION_RELATIVE_PATH
        from apm_cli.compilation.constitution import read_constitution

        constitution_path = tmp_path / CONSTITUTION_RELATIVE_PATH
        constitution_path.parent.mkdir(parents=True, exist_ok=True)
        constitution_path.write_text("content", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
            result = read_constitution(tmp_path)
        assert result is None

    def test_find_constitution_returns_path(self, tmp_path: Path) -> None:
        from apm_cli.compilation.constants import CONSTITUTION_RELATIVE_PATH
        from apm_cli.compilation.constitution import find_constitution

        path = find_constitution(tmp_path)
        assert path == tmp_path / CONSTITUTION_RELATIVE_PATH


# ---------------------------------------------------------------------------
# deps/_shared
# ---------------------------------------------------------------------------


class TestValidateAndLoadPackage:
    """_validate_and_load_package() -- error and success paths."""

    def test_invalid_package_raises_runtime_error(self, tmp_path: Path) -> None:
        from apm_cli.deps._shared import _validate_and_load_package

        validation_result = MagicMock()
        validation_result.is_valid = False
        validation_result.errors = ["missing apm.yml"]

        dep_ref = MagicMock()
        dep_ref.repo_url = "github.com/owner/pkg"

        target_path = tmp_path / "pkg"
        target_path.mkdir()

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError, match="Invalid APM package"):
                _validate_and_load_package(validation_result, target_path, dep_ref)
        mock_rmtree.assert_called_once()

    def test_invalid_package_no_target_path_no_rmtree(self, tmp_path: Path) -> None:
        from apm_cli.deps._shared import _validate_and_load_package

        validation_result = MagicMock()
        validation_result.is_valid = False
        validation_result.errors = ["bad manifest"]

        dep_ref = MagicMock()
        dep_ref.repo_url = "github.com/owner/pkg"

        nonexistent = tmp_path / "nonexistent"

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError):
                _validate_and_load_package(validation_result, nonexistent, dep_ref)
        mock_rmtree.assert_not_called()

    def test_valid_but_no_package_raises(self, tmp_path: Path) -> None:
        from apm_cli.deps._shared import _validate_and_load_package

        validation_result = MagicMock()
        validation_result.is_valid = True
        validation_result.package = None

        dep_ref = MagicMock()
        dep_ref.repo_url = "github.com/owner/pkg"

        with pytest.raises(RuntimeError, match="no package metadata"):
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

    def test_valid_package_sets_source(self, tmp_path: Path) -> None:
        from apm_cli.deps._shared import _validate_and_load_package

        pkg = MagicMock()
        validation_result = MagicMock()
        validation_result.is_valid = True
        validation_result.package = pkg

        dep_ref = MagicMock()
        dep_ref.to_github_url.return_value = "https://github.com/owner/pkg"

        result = _validate_and_load_package(validation_result, tmp_path, dep_ref)
        assert result is pkg
        assert pkg.source == "https://github.com/owner/pkg"


# ---------------------------------------------------------------------------
# integration/coverage -- check_primitive_coverage
# ---------------------------------------------------------------------------


class TestCheckPrimitiveCoverage:
    """check_primitive_coverage() -- extra entry and method-existence checks."""

    def test_missing_primitives_raises(self) -> None:
        from apm_cli.integration.coverage import check_primitive_coverage

        # Inject a KNOWN_TARGETS with primitives not in dispatch table
        fake_target = MagicMock()
        fake_target.primitives = {"ghost_primitive": MagicMock()}

        with patch("apm_cli.integration.targets.KNOWN_TARGETS", {"fake": fake_target}):
            with pytest.raises(RuntimeError, match="ghost_primitive"):
                check_primitive_coverage({})

    def test_extra_dispatch_entries_raises(self) -> None:
        from apm_cli.integration.coverage import check_primitive_coverage

        fake_target = MagicMock()
        fake_target.primitives = {}

        with patch("apm_cli.integration.targets.KNOWN_TARGETS", {"fake": fake_target}):
            with pytest.raises(RuntimeError, match="stale entries"):
                check_primitive_coverage({"phantom_entry": MagicMock()})

    def test_missing_method_on_integrator_raises(self) -> None:
        from apm_cli.integration.coverage import check_primitive_coverage

        fake_target = MagicMock()
        fake_target.primitives = {"myprim": MagicMock()}

        class FakeIntegrator:
            pass

        entry = MagicMock()
        entry.integrator_class = FakeIntegrator
        entry.integrate_method = "do_integrate"
        entry.sync_method = None

        with patch("apm_cli.integration.targets.KNOWN_TARGETS", {"fake": fake_target}):
            with pytest.raises(RuntimeError, match="missing method"):
                check_primitive_coverage({"myprim": entry})

    def test_special_cases_cover_missing_primitives(self) -> None:
        from apm_cli.integration.coverage import check_primitive_coverage

        fake_target = MagicMock()
        fake_target.primitives = {"special": MagicMock()}

        # special_cases covers the primitive
        with patch("apm_cli.integration.targets.KNOWN_TARGETS", {"fake": fake_target}):
            check_primitive_coverage({}, special_cases={"special"})
        # No exception


# ---------------------------------------------------------------------------
# adapters/package_manager/base
# ---------------------------------------------------------------------------


class TestMCPPackageManagerAdapterBase:
    """MCPPackageManagerAdapter abstract method enforcement."""

    def test_cannot_instantiate_directly(self) -> None:
        from apm_cli.adapters.package_manager.base import MCPPackageManagerAdapter

        with pytest.raises(TypeError):
            MCPPackageManagerAdapter()  # type: ignore[abstract]

    def test_concrete_implementation_works(self) -> None:
        from apm_cli.adapters.package_manager.base import MCPPackageManagerAdapter

        class ConcreteAdapter(MCPPackageManagerAdapter):
            def install(self, package_name, version=None):
                return f"installed {package_name}"

            def uninstall(self, package_name):
                return f"uninstalled {package_name}"

            def list_installed(self):
                return []

            def search(self, query):
                return [query]

        adapter = ConcreteAdapter()
        assert adapter.install("mcp-test") == "installed mcp-test"
        assert adapter.uninstall("mcp-test") == "uninstalled mcp-test"
        assert adapter.list_installed() == []
        assert adapter.search("query") == ["query"]
