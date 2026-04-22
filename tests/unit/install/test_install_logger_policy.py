"""Unit tests for InstallLogger policy methods and CATEGORY_POLICY diagnostics.

Covers W1-logger deliverables from issue #827:
- policy_resolved verbose/non-verbose behaviour for warn/off/block
- policy_violation routes to DiagnosticCollector under CATEGORY_POLICY
- block severity also prints inline error
- policy_disabled emits loud warning
- policy reason helpers produce actionable text
- DiagnosticCollector.policy() records under CATEGORY_POLICY
- _render_policy_group renders blocked vs warn items correctly
"""

from unittest.mock import call, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.utils.diagnostics import (
    CATEGORY_POLICY,
    CATEGORY_SECURITY,
    DiagnosticCollector,
    _CATEGORY_ORDER,
)


# ── CATEGORY_POLICY placement in _CATEGORY_ORDER ───────────────────


class TestCategoryPolicyOrder:
    def test_category_policy_exists(self):
        assert CATEGORY_POLICY == "policy"

    def test_category_policy_in_order(self):
        assert CATEGORY_POLICY in _CATEGORY_ORDER

    def test_category_policy_after_security(self):
        sec_idx = _CATEGORY_ORDER.index(CATEGORY_SECURITY)
        pol_idx = _CATEGORY_ORDER.index(CATEGORY_POLICY)
        assert pol_idx == sec_idx + 1, (
            f"CATEGORY_POLICY should be immediately after CATEGORY_SECURITY; "
            f"got security={sec_idx}, policy={pol_idx}"
        )


# ── DiagnosticCollector.policy() recording ──────────────────────────


class TestDiagnosticCollectorPolicy:
    def test_policy_records_under_category_policy(self):
        dc = DiagnosticCollector()
        dc.policy("Blocked by deny list", package="acme/evil", severity="block")
        groups = dc.by_category()
        assert CATEGORY_POLICY in groups
        assert len(groups[CATEGORY_POLICY]) == 1
        d = groups[CATEGORY_POLICY][0]
        assert d.message == "Blocked by deny list"
        assert d.package == "acme/evil"
        assert d.severity == "block"
        assert d.category == CATEGORY_POLICY

    def test_policy_count(self):
        dc = DiagnosticCollector()
        dc.policy("warn1", severity="warning")
        dc.policy("block1", severity="block")
        dc.policy("warn2", severity="warning")
        assert dc.policy_count == 3

    def test_policy_count_zero_when_empty(self):
        dc = DiagnosticCollector()
        assert dc.policy_count == 0

    def test_policy_does_not_pollute_other_categories(self):
        dc = DiagnosticCollector()
        dc.policy("pol", severity="block")
        dc.warn("general warning")
        groups = dc.by_category()
        assert CATEGORY_POLICY in groups
        assert "warning" in groups
        assert len(groups[CATEGORY_POLICY]) == 1
        assert len(groups["warning"]) == 1


# ── policy_resolved ─────────────────────────────────────────────────


class TestPolicyResolved:
    """policy_resolved: verbose-only for warn/off; always visible for block."""

    @patch("apm_cli.core.command_logger._rich_info")
    def test_warn_verbose_shows_info(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="warn",
        )
        mock_info.assert_called_once()
        msg = mock_info.call_args[0][0]
        assert "acme/.github/apm-policy.yml" in msg
        assert "enforcement=warn" in msg
        assert mock_info.call_args[1].get("symbol") == "info"

    @patch("apm_cli.core.command_logger._rich_info")
    def test_warn_non_verbose_silent(self, mock_info):
        logger = InstallLogger(verbose=False)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="warn",
        )
        mock_info.assert_not_called()

    @patch("apm_cli.core.command_logger._rich_info")
    def test_off_verbose_shows_info(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="off",
        )
        mock_info.assert_called_once()
        msg = mock_info.call_args[0][0]
        assert "enforcement=off" in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_off_non_verbose_silent(self, mock_info):
        logger = InstallLogger(verbose=False)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="off",
        )
        mock_info.assert_not_called()

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_block_always_visible_non_verbose(self, mock_warning):
        logger = InstallLogger(verbose=False)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="block",
        )
        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "enforcement=block" in msg
        assert mock_warning.call_args[1].get("symbol") == "warning"

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_block_always_visible_verbose(self, mock_warning):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=False,
            enforcement="block",
        )
        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "enforcement=block" in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_cached_with_age_seconds(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="acme/.github/apm-policy.yml",
            cached=True,
            enforcement="warn",
            age_seconds=300,
        )
        msg = mock_info.call_args[0][0]
        assert "cached" in msg
        assert "fetched 5m ago" in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_cached_with_age_seconds_less_than_60(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="org/.github/apm-policy.yml",
            cached=True,
            enforcement="warn",
            age_seconds=45,
        )
        msg = mock_info.call_args[0][0]
        assert "cached" in msg
        assert "fetched 45s ago" in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_cached_with_age_seconds_hours(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="org/.github/apm-policy.yml",
            cached=True,
            enforcement="warn",
            age_seconds=7200,
        )
        msg = mock_info.call_args[0][0]
        assert "cached" in msg
        assert "fetched 2h ago" in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_cached_without_age(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="org/.github/apm-policy.yml",
            cached=True,
            enforcement="warn",
        )
        msg = mock_info.call_args[0][0]
        assert "(cached)" in msg
        assert "fetched" not in msg

    @patch("apm_cli.core.command_logger._rich_info")
    def test_not_cached(self, mock_info):
        logger = InstallLogger(verbose=True)
        logger.policy_resolved(
            source="org/.github/apm-policy.yml",
            cached=False,
            enforcement="warn",
        )
        msg = mock_info.call_args[0][0]
        assert "cached" not in msg


# ── policy_violation ────────────────────────────────────────────────


class TestPolicyViolation:
    """policy_violation: always pushes to DiagnosticCollector; block also prints inline."""

    def test_warn_pushes_to_diagnostics(self):
        logger = InstallLogger(verbose=False)
        logger.policy_violation(
            dep_ref="acme/shady-pkg",
            reason="Dependency on deny list",
            severity="warn",
        )
        groups = logger.diagnostics.by_category()
        assert CATEGORY_POLICY in groups
        d = groups[CATEGORY_POLICY][0]
        assert d.package == "acme/shady-pkg"
        assert d.message == "Dependency on deny list"
        assert d.severity == "warn"

    @patch("apm_cli.core.command_logger._rich_error")
    def test_warn_does_not_print_inline(self, mock_error):
        logger = InstallLogger(verbose=False)
        logger.policy_violation(
            dep_ref="acme/shady-pkg",
            reason="Dependency on deny list",
            severity="warn",
        )
        mock_error.assert_not_called()

    def test_block_pushes_to_diagnostics(self):
        logger = InstallLogger(verbose=False)
        logger.policy_violation(
            dep_ref="acme/evil-pkg",
            reason="Blocked by org deny list",
            severity="block",
        )
        groups = logger.diagnostics.by_category()
        assert CATEGORY_POLICY in groups
        d = groups[CATEGORY_POLICY][0]
        assert d.severity == "block"

    @patch("apm_cli.core.command_logger._rich_error")
    def test_block_prints_inline_error(self, mock_error):
        logger = InstallLogger(verbose=False)
        logger.policy_violation(
            dep_ref="acme/evil-pkg",
            reason="Blocked by org deny list",
            severity="block",
        )
        mock_error.assert_called_once()
        msg = mock_error.call_args[0][0]
        assert "acme/evil-pkg" in msg
        assert "Blocked by org deny list" in msg
        assert mock_error.call_args[1].get("symbol") == "error"

    @patch("apm_cli.core.command_logger._rich_error")
    def test_block_inline_verbose(self, mock_error):
        """Block prints inline error regardless of verbose setting."""
        logger = InstallLogger(verbose=True)
        logger.policy_violation(
            dep_ref="acme/evil-pkg",
            reason="Blocked by org deny list",
            severity="block",
        )
        mock_error.assert_called_once()

    def test_multiple_violations_accumulate(self):
        logger = InstallLogger(verbose=False)
        logger.policy_violation("pkg-a", "denied", "warn")
        logger.policy_violation("pkg-b", "blocked", "block")
        logger.policy_violation("pkg-c", "also denied", "warn")
        assert logger.diagnostics.policy_count == 3


# ── policy_disabled ─────────────────────────────────────────────────


class TestPolicyDisabled:
    """policy_disabled: always emits loud warning, never silenceable."""

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_emits_warning_non_verbose(self, mock_warning):
        logger = InstallLogger(verbose=False)
        logger.policy_disabled("--no-policy")
        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "--no-policy" in msg
        assert "apm audit --ci" in msg
        assert mock_warning.call_args[1].get("symbol") == "warning"

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_emits_warning_verbose(self, mock_warning):
        logger = InstallLogger(verbose=True)
        logger.policy_disabled("APM_POLICY_DISABLE=1")
        mock_warning.assert_called_once()
        msg = mock_warning.call_args[0][0]
        assert "APM_POLICY_DISABLE=1" in msg
        assert "apm audit --ci" in msg

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_mentions_audit_bypass_not_affected(self, mock_warning):
        """Warning must clarify that audit --ci is NOT bypassed."""
        logger = InstallLogger(verbose=False)
        logger.policy_disabled("--no-policy")
        msg = mock_warning.call_args[0][0]
        assert "does NOT bypass" in msg.lower() or "does NOT bypass" in msg


# ── Policy reason helpers (I9 actionable wording) ───────────────────


class TestPolicyReasonHelpers:
    """Static helpers produce actionable remediation text per rubber-duck I9."""

    def test_reason_auth(self):
        msg = InstallLogger._policy_reason_auth("acme/.github/apm-policy.yml")
        assert "acme/.github/apm-policy.yml" in msg
        assert "gh auth status" in msg
        assert "GITHUB_APM_PAT" in msg

    def test_reason_unreachable(self):
        msg = InstallLogger._policy_reason_unreachable("acme/.github/apm-policy.yml")
        assert "unreachable" in msg
        assert "--no-policy" in msg
        assert "VPN" in msg or "firewall" in msg

    def test_reason_malformed(self):
        msg = InstallLogger._policy_reason_malformed("acme/.github/apm-policy.yml")
        assert "malformed" in msg
        assert "org admin" in msg

    def test_reason_blocked(self):
        msg = InstallLogger._policy_reason_blocked(
            "acme/evil-pkg", "acme/.github/apm-policy.yml"
        )
        assert "acme/evil-pkg" in msg
        assert "acme/.github/apm-policy.yml" in msg
        assert "--no-policy" in msg
        assert "apm.yml" in msg


# ── _render_policy_group (via DiagnosticCollector.render_summary) ───


class TestRenderPolicyGroup:
    """Policy diagnostics render correctly in the summary."""

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_block_renders_red(self, _console, _info, mock_warning, mock_echo):
        dc = DiagnosticCollector(verbose=False)
        dc.policy("Blocked by deny list", package="acme/evil", severity="block")
        dc.render_summary()

        # Find the red bold call for the block header
        red_bold_calls = [
            c for c in mock_echo.call_args_list
            if c[1].get("color") == "red" and c[1].get("bold") is True
        ]
        assert len(red_bold_calls) >= 1
        header = red_bold_calls[0][0][0]
        assert "1" in header
        assert "blocked by org policy" in header

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_warn_renders_yellow(self, _console, _info, mock_warning, mock_echo):
        dc = DiagnosticCollector(verbose=False)
        dc.policy("Dependency on deny list", package="acme/shady", severity="warning")
        dc.render_summary()

        # Warning header via _rich_warning
        warning_calls = [
            c for c in mock_warning.call_args_list
            if "policy warning" in str(c).lower()
        ]
        assert len(warning_calls) >= 1

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_mixed_block_and_warn(self, _console, _info, mock_warning, mock_echo):
        dc = DiagnosticCollector(verbose=False)
        dc.policy("blocked dep", package="acme/evil", severity="block")
        dc.policy("warned dep", package="acme/shady", severity="warning")
        dc.render_summary()

        # Both sections rendered
        all_text = " ".join(str(c) for c in mock_echo.call_args_list)
        assert "blocked by org policy" in all_text
        all_warn_text = " ".join(str(c) for c in mock_warning.call_args_list)
        assert "policy warning" in all_warn_text

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_detail_shown_for_block(self, _console, _info, _warning, mock_echo):
        """Block items always show detail (not gated on verbose)."""
        dc = DiagnosticCollector(verbose=False)
        dc.policy(
            "Blocked by deny list",
            package="acme/evil",
            severity="block",
            detail="Use --no-policy to bypass",
        )
        dc.render_summary()

        detail_calls = [
            c for c in mock_echo.call_args_list
            if "Use --no-policy" in str(c)
        ]
        assert len(detail_calls) >= 1

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_warn_detail_gated_on_verbose(self, _console, _info, _warning, mock_echo):
        """Warn items only show detail in verbose mode."""
        dc = DiagnosticCollector(verbose=False)
        dc.policy(
            "Warned dep",
            package="acme/shady",
            severity="warning",
            detail="Consider removing",
        )
        dc.render_summary()

        detail_calls = [
            c for c in mock_echo.call_args_list
            if "Consider removing" in str(c)
        ]
        assert len(detail_calls) == 0

    @patch("apm_cli.utils.diagnostics._rich_echo")
    @patch("apm_cli.utils.diagnostics._rich_warning")
    @patch("apm_cli.utils.diagnostics._rich_info")
    @patch("apm_cli.utils.diagnostics._get_console", return_value=None)
    def test_warn_detail_shown_in_verbose(self, _console, _info, _warning, mock_echo):
        """Warn items show detail when verbose=True."""
        dc = DiagnosticCollector(verbose=True)
        dc.policy(
            "Warned dep",
            package="acme/shady",
            severity="warning",
            detail="Consider removing",
        )
        dc.render_summary()

        detail_calls = [
            c for c in mock_echo.call_args_list
            if "Consider removing" in str(c)
        ]
        assert len(detail_calls) >= 1


# ── ASCII-only constraint ───────────────────────────────────────────


class TestAsciiOnly:
    """All output from policy methods must be ASCII-only (no emoji, no unicode)."""

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_policy_resolved_ascii(self, mock_warning):
        logger = InstallLogger(verbose=False)
        logger.policy_resolved("org/.github/apm-policy.yml", True, "block", 300)
        msg = mock_warning.call_args[0][0]
        assert msg.isascii(), f"Non-ASCII in policy_resolved output: {msg!r}"

    @patch("apm_cli.core.command_logger._rich_error")
    def test_policy_violation_ascii(self, mock_error):
        logger = InstallLogger(verbose=False)
        logger.policy_violation("acme/pkg", "Blocked by deny list", "block")
        msg = mock_error.call_args[0][0]
        assert msg.isascii(), f"Non-ASCII in policy_violation output: {msg!r}"

    @patch("apm_cli.core.command_logger._rich_warning")
    def test_policy_disabled_ascii(self, mock_warning):
        logger = InstallLogger(verbose=False)
        logger.policy_disabled("--no-policy")
        msg = mock_warning.call_args[0][0]
        assert msg.isascii(), f"Non-ASCII in policy_disabled output: {msg!r}"

    def test_reason_helpers_ascii(self):
        for fn, args in [
            (InstallLogger._policy_reason_auth, ("src",)),
            (InstallLogger._policy_reason_unreachable, ("src",)),
            (InstallLogger._policy_reason_malformed, ("src",)),
            (InstallLogger._policy_reason_blocked, ("dep", "src")),
        ]:
            msg = fn(*args)
            assert msg.isascii(), f"Non-ASCII in {fn.__name__}: {msg!r}"
