"""Tests for #832 PR-review findings: cross-cutting policy hardening.

Covers:
- #2 / #3: ``PolicyViolationError`` is the canonical class; it propagates
  through the install pipeline without being wrapped into a generic
  ``RuntimeError("Failed to resolve APM dependencies: ...")``.
- #4: shared 9-outcome routing table behaves identically when called
  from either the gate phase or the preflight helper (smoke).
- #5: dry-run path falls back to ``check.name`` when ``CheckResult.details``
  is empty, so a failed check is never silently omitted.
- #6: ``_extract_dep_ref`` honours the ``"{ref}: {reason}"`` contract
  with a defensive fallback to ``check.name`` for malformed details.
- #7: the policy cache path is asserted to live inside ``apm_modules``;
  a symlinked ``apm_modules`` pointing outside the project is rejected.
- #8: ``discover_policy_with_chain`` no longer accepts ``no_policy``.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _get_cache_dir,
    discover_policy_with_chain,
)
from apm_cli.policy.install_preflight import (
    PolicyBlockError,
    _extract_dep_ref,
    run_policy_preflight,
)
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.outcome_routing import route_discovery_outcome
from apm_cli.policy.schema import ApmPolicy
from apm_cli.utils.path_security import PathTraversalError


# ──────────────────────────────────────────────────────────────────────
# #2: PolicyBlockError is an alias of PolicyViolationError
# ──────────────────────────────────────────────────────────────────────


class TestPolicyExceptionConsolidation:
    def test_policy_block_error_is_alias(self):
        """The two names must resolve to the same class object so any
        ``except PolicyBlockError`` clause catches a fresh
        ``raise PolicyViolationError`` and vice versa.
        """
        assert PolicyBlockError is PolicyViolationError

    def test_policy_violation_carries_optional_attrs(self):
        err = PolicyViolationError(
            "blocked",
            audit_result=CIAuditResult(checks=[]),
            policy_source="org:acme/.github",
        )
        assert err.policy_source == "org:acme/.github"
        assert err.audit_result is not None

    def test_policy_violation_works_without_kwargs(self):
        """Backward-compat: callers that just raise with a message still work."""
        err = PolicyViolationError("blocked")
        assert err.audit_result is None
        assert err.policy_source == ""


# ──────────────────────────────────────────────────────────────────────
# #3: pipeline does not double-wrap PolicyViolationError
# ──────────────────────────────────────────────────────────────────────


class TestPipelineDoesNotDoubleWrap:
    """The bare ``except Exception`` at the bottom of
    ``install/pipeline.py`` previously wrapped PolicyViolationError into
    ``RuntimeError("Failed to resolve APM dependencies: ...")`` which
    then got wrapped a SECOND time at ``commands/install.py`` into
    ``"Failed to install APM dependencies: Failed to resolve ..."``.
    """

    def test_pipeline_module_catches_policy_violation_first(self):
        """Source-level guarantee: the dedicated ``except PolicyViolationError``
        clause appears BEFORE the bare ``except Exception`` so the typed
        exception escapes unwrapped.
        """
        from apm_cli.install import pipeline

        src = inspect.getsource(pipeline)
        # The function must have a typed PolicyViolationError handler
        # appearing BEFORE the bare-Exception handler.
        pv_idx = src.find("except PolicyViolationError:\n")
        ex_idx = src.find('raise RuntimeError(f"Failed to resolve APM')
        assert pv_idx != -1, (
            "pipeline.py must catch PolicyViolationError explicitly so "
            "the policy message surfaces to the caller without wrapping"
        )
        assert ex_idx != -1
        assert pv_idx < ex_idx, (
            "PolicyViolationError must be caught BEFORE the bare "
            "Exception wrapper, otherwise the policy message gets nested "
            "into 'Failed to resolve APM dependencies: ...'"
        )


# ──────────────────────────────────────────────────────────────────────
# #4: outcome routing table -- single source of truth smoke
# ──────────────────────────────────────────────────────────────────────


class TestOutcomeRoutingTable:
    def test_absent_returns_none_no_raise(self):
        logger = InstallLogger(verbose=True)
        fetch = PolicyFetchResult(policy=None, source="org:acme/.github", outcome="absent")
        with patch("apm_cli.core.command_logger._rich_info") as mock_info:
            policy = route_discovery_outcome(
                fetch, logger=logger, fetch_failure_default="warn"
            )
        assert policy is None
        # absent + verbose => one info line
        assert mock_info.call_count == 1

    def test_hash_mismatch_always_raises(self):
        logger = InstallLogger()
        fetch = PolicyFetchResult(
            policy=None, source="org:acme/.github", outcome="hash_mismatch"
        )
        with pytest.raises(PolicyViolationError, match="hash mismatch"):
            route_discovery_outcome(
                fetch, logger=logger, fetch_failure_default="warn"
            )

    def test_hash_mismatch_dry_run_no_raise(self):
        logger = InstallLogger()
        fetch = PolicyFetchResult(
            policy=None, source="org:acme/.github", outcome="hash_mismatch"
        )
        result = route_discovery_outcome(
            fetch, logger=logger, fetch_failure_default="warn",
            raise_blocking_errors=False,
        )
        assert result is None

    def test_fetch_failure_default_block_raises(self):
        logger = InstallLogger()
        fetch = PolicyFetchResult(
            policy=None, source="org:acme/.github", outcome="malformed",
            error="bad yaml",
        )
        with pytest.raises(PolicyViolationError, match="fetch_failure_default=block"):
            route_discovery_outcome(
                fetch, logger=logger, fetch_failure_default="block"
            )

    def test_fetch_failure_default_warn_does_not_raise(self):
        logger = InstallLogger()
        fetch = PolicyFetchResult(
            policy=None, source="org:acme/.github", outcome="malformed",
            error="bad yaml",
        )
        result = route_discovery_outcome(
            fetch, logger=logger, fetch_failure_default="warn"
        )
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# #5: dry-run falls back to check.name when details is empty
# ──────────────────────────────────────────────────────────────────────


class TestDryRunEmptyDetailsFallback:
    """A failed ``CheckResult`` with empty ``details`` must still appear
    in the dry-run preview -- otherwise users get a silent block.
    """

    def _policy(self, enforcement="block"):
        return ApmPolicy(
            name="test",
            version="1.0",
            enforcement=enforcement,
        )

    def test_dry_run_preview_falls_back_to_check_name(self):
        # Custom audit result whose failed check has empty details.
        empty_failed = CheckResult(
            name="dependency-allowlist",
            passed=False,
            message="1 dependency(ies) not in allow list",
            details=[],  # Intentionally empty
        )
        audit = CIAuditResult(checks=[empty_failed])

        fetch = PolicyFetchResult(
            policy=self._policy(),
            source="org:acme/.github",
            outcome="found",
            cached=False,
        )

        logger = MagicMock()
        # Force the audit_result returned by run_dependency_policy_checks
        # to be ours, so the empty-details edge case is exercised.
        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch,
        ), patch(
            "apm_cli.policy.install_preflight.run_dependency_policy_checks",
            return_value=audit,
        ):
            run_policy_preflight(
                project_root=Path("/tmp/fake"),
                apm_deps=[],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # logger.warning() must have been called and the message must
        # contain the check name (the fallback) since details is empty.
        warn_calls = [c.args[0] for c in logger.warning.call_args_list]
        assert any("dependency-allowlist" in m for m in warn_calls), (
            f"Expected dry-run preview to mention 'dependency-allowlist' "
            f"as a fallback for empty details, got: {warn_calls!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# #6: dep-ref parsing contract + defensive fallback
# ──────────────────────────────────────────────────────────────────────


class TestExtractDepRefContract:
    def test_standard_ref_colon_reason(self):
        # Standard policy_checks output: "{ref}: {reason}"
        assert _extract_dep_ref(
            "acme/server: not in allow list", "dependency-allowlist"
        ) == "acme/server"

    def test_empty_detail_falls_back_to_check_name(self):
        assert _extract_dep_ref("", "dependency-denylist") == "dependency-denylist"

    def test_no_colon_returns_stripped_detail(self):
        assert _extract_dep_ref(
            "  some weird detail  ", "rule-x"
        ) == "some weird detail"

    def test_colon_only_falls_back_to_check_name(self):
        # Pathological: ":foo" -> head is empty -> fallback
        assert _extract_dep_ref(":foo", "rule-x") == "rule-x"


# ──────────────────────────────────────────────────────────────────────
# #7: cache path containment
# ──────────────────────────────────────────────────────────────────────


class TestCachePathContainment:
    def test_normal_layout_returns_path_under_apm_modules(self, tmp_path):
        # No symlinks: cache path lives under <project>/apm_modules.
        (tmp_path / "apm_modules").mkdir()
        cache_dir = _get_cache_dir(tmp_path)
        assert cache_dir.parent.name == "apm_modules"
        assert cache_dir.is_relative_to(tmp_path / "apm_modules")

    def test_symlinked_apm_modules_outside_project_is_rejected(self, tmp_path):
        # Set up an evil layout: <project>/apm_modules is a symlink
        # pointing OUTSIDE the project tree.
        project = tmp_path / "project"
        evil = tmp_path / "elsewhere"
        project.mkdir()
        evil.mkdir()
        symlink = project / "apm_modules"
        try:
            os.symlink(evil, symlink)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform")

        with pytest.raises(PathTraversalError):
            _get_cache_dir(project)

    def test_unresolved_project_root_does_not_raise(self, tmp_path):
        # Regression for #886: on Windows, tempfile.mkdtemp() may return
        # an 8.3 short-name path (e.g. RUNNER~1). _get_cache_dir must
        # resolve project_root before building the candidate path so
        # both sides of ensure_path_within use consistent long names.
        real = tmp_path / "real-project"
        real.mkdir()
        try:
            link = tmp_path / "indirect"
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform")

        cache_dir = _get_cache_dir(link)
        assert cache_dir.parent.name == "apm_modules"


# ──────────────────────────────────────────────────────────────────────
# #8: discover_policy_with_chain has no ``no_policy`` parameter
# ──────────────────────────────────────────────────────────────────────


class TestDiscoverPolicyHasNoNoPolicy:
    def test_signature_omits_no_policy(self):
        sig = inspect.signature(discover_policy_with_chain)
        assert "no_policy" not in sig.parameters, (
            "#832: discover_policy_with_chain should not accept no_policy "
            "(escape hatch is enforced by callers)"
        )

    def test_env_var_still_short_circuits(self):
        with patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"}):
            result = discover_policy_with_chain(Path("/fake"))
        assert result.outcome == "disabled"
