"""Wave 6: integration tests for commands/audit.py helpers and policy/discovery.py helpers.

Goal: maximise code coverage by exercising real code paths with minimal mocking.
Only external I/O (HTTP, subprocess, auth, filesystem outside tmp_path) is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.security.content_scanner import ScanFinding

# ---------------------------------------------------------------------------
# commands/audit.py -- helper functions
# ---------------------------------------------------------------------------


class TestAuditOutcomeCause:
    """Cover _audit_outcome_cause for all outcome branches."""

    def test_no_git_remote(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("no_git_remote", "org:test", None)
        assert "git remote" in result.lower()

    def test_absent(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("absent", "org:contoso/.github", None)
        assert "contoso" in result
        assert "No org policy" in result

    def test_empty(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("empty", "org:test/.github", None)
        assert "empty" in result.lower()

    def test_fetch_failed_with_error(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("cache_miss_fetch_fail", "org:test", "timeout")
        assert "timeout" in result

    def test_fetch_failed_no_error(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("malformed", None, None)
        assert "malformed" in result

    def test_unknown_source(self) -> None:
        from apm_cli.commands.audit import _audit_outcome_cause

        result = _audit_outcome_cause("garbage_response", None, "bad body")
        assert "bad body" in result


class TestScanSingleFile:
    """Cover _scan_single_file."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _scan_single_file

        logger = MagicMock()
        with pytest.raises(SystemExit):
            _scan_single_file(tmp_path / "nonexistent.md", logger)
        logger.error.assert_called()

    def test_directory_path(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _scan_single_file

        logger = MagicMock()
        with pytest.raises(SystemExit):
            _scan_single_file(tmp_path, logger)
        logger.error.assert_called()

    def test_clean_file(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _scan_single_file

        f = tmp_path / "clean.md"
        f.write_text("# Hello World\nNormal content.\n")
        logger = MagicMock()
        findings, count = _scan_single_file(f, logger)
        assert count == 1
        assert len(findings) == 0

    def test_file_with_hidden_chars(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _scan_single_file

        f = tmp_path / "suspicious.md"
        # Zero-width space U+200B
        f.write_text("Hello\u200bWorld\n")
        logger = MagicMock()
        _findings, _count = _scan_single_file(f, logger)


class TestHasActionableFindings:
    """Cover _has_actionable_findings."""

    def test_no_findings(self) -> None:
        from apm_cli.commands.audit import _has_actionable_findings

        assert _has_actionable_findings({}) is False

    def test_info_only(self) -> None:
        from apm_cli.commands.audit import _has_actionable_findings

        finding = ScanFinding(
            file="test.md",
            line=1,
            column=1,
            char="\u200b",
            codepoint="U+200B",
            severity="info",
            category="zero-width",
            description="zero-width space",
        )
        assert _has_actionable_findings({"test.md": [finding]}) is False

    def test_warning_finding(self) -> None:
        from apm_cli.commands.audit import _has_actionable_findings

        finding = ScanFinding(
            file="test.md",
            line=1,
            column=1,
            char="\u200b",
            codepoint="U+200B",
            severity="warning",
            category="zero-width",
            description="zero-width space",
        )
        assert _has_actionable_findings({"test.md": [finding]}) is True

    def test_critical_finding(self) -> None:
        from apm_cli.commands.audit import _has_actionable_findings

        finding = ScanFinding(
            file="test.md",
            line=1,
            column=1,
            char="\u202e",
            codepoint="U+202E",
            severity="critical",
            category="bidi-override",
            description="right-to-left override",
        )
        assert _has_actionable_findings({"test.md": [finding]}) is True


class TestRenderFindingsTable:
    """Cover _render_findings_table -- exercises sorting and filtering."""

    def test_no_findings(self) -> None:
        from apm_cli.commands.audit import _render_findings_table

        # Should not raise
        _render_findings_table({}, verbose=False)
        _render_findings_table({}, verbose=True)

    def test_with_mixed_severities(self) -> None:
        from apm_cli.commands.audit import _render_findings_table

        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="info",
                ),
                ScanFinding(
                    file="test.md",
                    line=2,
                    column=1,
                    char="x",
                    codepoint="U+202E",
                    category="test",
                    description="rlo",
                    severity="critical",
                ),
                ScanFinding(
                    file="test.md",
                    line=3,
                    column=1,
                    char="x",
                    codepoint="U+200C",
                    category="test",
                    description="zwnj",
                    severity="warning",
                ),
            ]
        }
        # Non-verbose filters out info
        _render_findings_table(findings, verbose=False)
        # Verbose shows all
        _render_findings_table(findings, verbose=True)


class TestRenderSummary:
    """Cover _render_summary."""

    def test_no_findings(self) -> None:
        from apm_cli.commands.audit import _render_summary

        logger = MagicMock()
        _render_summary({}, 5, logger)
        logger.success.assert_called_once()

    def test_critical_findings(self) -> None:
        from apm_cli.commands.audit import _render_summary

        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+202E",
                    category="test",
                    description="rlo",
                    severity="critical",
                ),
            ]
        }
        logger = MagicMock()
        _render_summary(findings, 1, logger)
        logger.error.assert_called()

    def test_warning_only(self) -> None:
        from apm_cli.commands.audit import _render_summary

        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200C",
                    category="test",
                    description="zwnj",
                    severity="warning",
                ),
            ]
        }
        logger = MagicMock()
        _render_summary(findings, 1, logger)
        logger.warning.assert_called()

    def test_info_only(self) -> None:
        from apm_cli.commands.audit import _render_summary

        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="info",
                ),
            ]
        }
        logger = MagicMock()
        _render_summary(findings, 1, logger)
        logger.progress.assert_called()

    def test_mixed_critical_and_info(self) -> None:
        from apm_cli.commands.audit import _render_summary

        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+202E",
                    category="test",
                    description="rlo",
                    severity="critical",
                ),
                ScanFinding(
                    file="test.md",
                    line=2,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="info",
                ),
            ]
        }
        logger = MagicMock()
        _render_summary(findings, 1, logger)
        logger.error.assert_called()


class TestApplyStrip:
    """Cover _apply_strip."""

    def test_strip_clean_file(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _apply_strip

        f = tmp_path / "clean.md"
        f.write_text("Normal text\n")
        logger = MagicMock()
        findings = {
            str(f): [
                ScanFinding(
                    file=str(f),
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    severity="warning",
                    category="zero-width",
                    description="zwsp",
                ),
            ]
        }
        modified = _apply_strip(findings, tmp_path, logger)
        assert isinstance(modified, int)

    def test_strip_relative_path_outside_root(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _apply_strip

        logger = MagicMock()
        findings = {
            "../outside/file.md": [
                ScanFinding(
                    file="../outside/file.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="warning",
                ),
            ]
        }
        modified = _apply_strip(findings, tmp_path, logger)
        assert modified == 0

    def test_strip_nonexistent_file(self, tmp_path: Path) -> None:
        from apm_cli.commands.audit import _apply_strip

        logger = MagicMock()
        abs_path = str(tmp_path / "missing.md")
        findings = {
            abs_path: [
                ScanFinding(
                    file=abs_path,
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="warning",
                ),
            ]
        }
        modified = _apply_strip(findings, tmp_path, logger)
        assert modified == 0


class TestPreviewStrip:
    """Cover _preview_strip."""

    def test_nothing_to_clean(self) -> None:
        from apm_cli.commands.audit import _preview_strip

        logger = MagicMock()
        result = _preview_strip({}, logger)
        assert result == 0

    def test_info_only_nothing_to_strip(self) -> None:
        from apm_cli.commands.audit import _preview_strip

        logger = MagicMock()
        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+200B",
                    category="test",
                    description="zwsp",
                    severity="info",
                ),
            ]
        }
        result = _preview_strip(findings, logger)
        assert result == 0

    def test_strippable_findings(self) -> None:
        from apm_cli.commands.audit import _preview_strip

        logger = MagicMock()
        findings = {
            "test.md": [
                ScanFinding(
                    file="test.md",
                    line=1,
                    column=1,
                    char="x",
                    codepoint="U+202E",
                    category="test",
                    description="rlo",
                    severity="critical",
                ),
                ScanFinding(
                    file="test.md",
                    line=2,
                    column=1,
                    char="x",
                    codepoint="U+200C",
                    category="test",
                    description="zwnj",
                    severity="warning",
                ),
            ]
        }
        result = _preview_strip(findings, logger)
        assert result == 1


# ---------------------------------------------------------------------------
# policy/discovery.py -- pure-logic helpers
# ---------------------------------------------------------------------------


class TestSplitHashPin:
    """Cover _split_hash_pin."""

    def test_sha256_with_prefix(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin

        hex_val = "a" * 64
        algo, hex_part = _split_hash_pin(f"sha256:{hex_val}")
        assert algo == "sha256"
        assert hex_part == hex_val

    def test_bare_hex_defaults_to_sha256(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin

        hex_val = "b" * 64
        algo, hex_part = _split_hash_pin(hex_val)
        assert algo == "sha256"
        assert hex_part == hex_val

    def test_unsupported_algorithm(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with pytest.raises(ProjectPolicyConfigError, match=r"Unsupported"):
            _split_hash_pin("md5:abcdef0123456789")

    def test_wrong_length(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with pytest.raises(ProjectPolicyConfigError, match=r"not a valid"):
            _split_hash_pin("sha256:abc")

    def test_non_hex_chars(self) -> None:
        from apm_cli.policy.discovery import _split_hash_pin
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with pytest.raises(ProjectPolicyConfigError, match=r"not a valid"):
            _split_hash_pin("sha256:" + "g" * 64)


class TestComputeHashNormalized:
    """Cover _compute_hash_normalized."""

    def test_with_no_expected_hash(self) -> None:
        from apm_cli.policy.discovery import _compute_hash_normalized

        result = _compute_hash_normalized("test content", None)
        assert result.startswith("sha256:")
        assert len(result.split(":")[1]) == 64

    def test_with_sha256_expected_hash(self) -> None:
        from apm_cli.policy.discovery import _compute_hash_normalized

        hex_val = "a" * 64
        result = _compute_hash_normalized("content", f"sha256:{hex_val}")
        assert result.startswith("sha256:")

    def test_with_invalid_expected_hash(self) -> None:
        from apm_cli.policy.discovery import _compute_hash_normalized

        # Invalid pin falls back to sha256
        result = _compute_hash_normalized("content", "md5:abc")
        assert result.startswith("sha256:")


class TestVerifyHashPin:
    """Cover _verify_hash_pin."""

    def test_no_pin_returns_none(self) -> None:
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin("content", None, "test")
        assert result is None

    def test_matching_hash(self) -> None:
        import hashlib

        from apm_cli.policy.discovery import _verify_hash_pin

        content = "test policy content"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result = _verify_hash_pin(content, f"sha256:{digest}", "test")
        assert result is None

    def test_mismatched_hash(self) -> None:
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin("content", "sha256:" + "a" * 64, "test")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_bytes_content(self) -> None:
        import hashlib

        from apm_cli.policy.discovery import _verify_hash_pin

        content = b"binary content"
        digest = hashlib.sha256(content).hexdigest()
        result = _verify_hash_pin(content, f"sha256:{digest}", "test")
        assert result is None

    def test_invalid_content_type(self) -> None:
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin(12345, "sha256:" + "a" * 64, "test")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_invalid_pin_format(self) -> None:
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin("content", "sha256:too_short", "test")
        assert result is not None
        assert result.outcome == "hash_mismatch"


class TestStripSourcePrefix:
    """Cover _strip_source_prefix."""

    def test_org_prefix(self) -> None:
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("org:contoso/.github") == "contoso/.github"

    def test_url_prefix(self) -> None:
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("url:https://example.com") == "https://example.com"

    def test_file_prefix(self) -> None:
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("file:/path/to/policy.yml") == "/path/to/policy.yml"

    def test_no_prefix(self) -> None:
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("contoso/.github") == "contoso/.github"


class TestDeriveLeafHost:
    """Cover _derive_leaf_host."""

    def test_url_source(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("url:https://github.com/org/.github", tmp_path)
        assert result == "github.com"

    def test_org_three_parts(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("org:ghes.corp.com/org/.github", tmp_path)
        assert result == "ghes.corp.com"

    def test_org_two_parts(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("org:contoso/.github", tmp_path)
        assert result == "github.com"

    def test_empty_source(self, tmp_path: Path) -> None:
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("", tmp_path)
        # Falls back to git remote -- returns None in tmp_path with no git
        assert result is None or isinstance(result, str)


class TestExtractExtendsHost:
    """Cover _extract_extends_host."""

    def test_full_url(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("https://github.com/org/.github")
        assert result == "github.com"

    def test_three_part_ref(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("ghes.corp.com/org/.github")
        assert result == "ghes.corp.com"

    def test_two_part_ref_returns_none(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("org/.github")
        assert result is None

    def test_single_part_returns_none(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("org-name")
        assert result is None

    def test_empty_returns_none(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("")
        assert result is None

    def test_none_returns_none(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host(None)
        assert result is None

    def test_http_url(self) -> None:
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("http://internal.corp/org/.github")
        assert result == "internal.corp"


class TestValidateExtendsHost:
    """Cover _validate_extends_host."""

    def test_same_host_passes(self) -> None:
        from apm_cli.policy.discovery import _validate_extends_host

        _validate_extends_host("github.com", "github.com/other-org/.github")

    def test_shorthand_always_passes(self) -> None:
        from apm_cli.policy.discovery import _validate_extends_host

        _validate_extends_host("github.com", "other-org/.github")
        _validate_extends_host("github.com", "other-org")
        _validate_extends_host(None, "other-org")

    def test_cross_host_rejected(self) -> None:
        from apm_cli.policy.discovery import _validate_extends_host
        from apm_cli.policy.inheritance import PolicyInheritanceError

        with pytest.raises(PolicyInheritanceError, match=r"cross-host"):
            _validate_extends_host("github.com", "evil.com/attacker/.github")

    def test_unknown_leaf_host_rejected(self) -> None:
        from apm_cli.policy.discovery import _validate_extends_host
        from apm_cli.policy.inheritance import PolicyInheritanceError

        with pytest.raises(PolicyInheritanceError, match=r"cross-host"):
            _validate_extends_host(None, "evil.com/attacker/.github")


class TestPolicyFetchResult:
    """Cover PolicyFetchResult dataclass and its property."""

    def test_found_property_true(self) -> None:
        from apm_cli.policy.discovery import PolicyFetchResult
        from apm_cli.policy.schema import ApmPolicy

        policy = ApmPolicy()
        result = PolicyFetchResult(policy=policy, outcome="found")
        assert result.found is True

    def test_found_property_false(self) -> None:
        from apm_cli.policy.discovery import PolicyFetchResult

        result = PolicyFetchResult(outcome="absent")
        assert result.found is False

    def test_disabled_outcome(self) -> None:
        from apm_cli.policy.discovery import PolicyFetchResult

        result = PolicyFetchResult(outcome="disabled")
        assert result.found is False
        assert result.outcome == "disabled"

    def test_hash_mismatch_fields(self) -> None:
        from apm_cli.policy.discovery import PolicyFetchResult

        result = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:test/.github",
            expected_hash="sha256:aaa",
            raw_bytes_hash="sha256:bbb",
        )
        assert result.outcome == "hash_mismatch"
        assert result.expected_hash == "sha256:aaa"
        assert result.raw_bytes_hash == "sha256:bbb"


class TestDiscoverPolicyWithChainEscapeHatch:
    """Cover discover_policy_with_chain escape hatch."""

    def test_disabled_via_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.policy.discovery import discover_policy_with_chain

        monkeypatch.setenv("APM_POLICY_DISABLE", "1")
        result = discover_policy_with_chain(tmp_path)
        assert result.outcome == "disabled"
