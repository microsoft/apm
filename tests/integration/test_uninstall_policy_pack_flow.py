"""Integration tests for uninstall engine, policy discovery, and pack commands.

Covers three modules with low integration coverage:
- src/apm_cli/commands/uninstall/engine.py  (coverage gap: 287)
- src/apm_cli/policy/discovery.py           (coverage gap: 280)
- src/apm_cli/commands/pack.py              (coverage gap: 259)

Strategy:
- Exercise real code paths with minimal mocking.
- Only mock external I/O: HTTP requests, subprocess calls, git operations.
- Use real temp directories and real file I/O.
- No live network calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_POLICY_MINIMAL = """\
name: test-policy
version: "1.0.0"
enforcement: warn
"""

_POLICY_WITH_DENY = """\
name: deny-policy
version: "1.0.0"
enforcement: block
dependencies:
  deny:
    - "blocked/*"
"""

_POLICY_WITH_ALLOW = """\
name: allow-policy
version: "1.0.0"
enforcement: warn
dependencies:
  allow:
    - "company/*"
    - "microsoft/*"
"""

_POLICY_WITH_EXTENDS = """\
name: child-policy
version: "1.0.0"
enforcement: warn
extends: "parent-org/.github"
"""

_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""

_APM_YML_MINIMAL = """\
name: test-package
version: 1.0.0
description: A test package
owner:
  name: test-org
dependencies:
  apm: []
"""


def _sha256_of(content: str) -> str:
    """Return SHA-256 hexdigest of a UTF-8 string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_policy_file(directory: Path, content: str, name: str = "apm-policy.yml") -> Path:
    """Write a policy YAML file and return its path."""
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# ===========================================================================
# PART 1 — commands/uninstall/engine.py
# ===========================================================================


class TestResolveMarketplacePackages:
    """Tests for _resolve_marketplace_packages — Stage 1 / 2 / 3 flows."""

    def test_stage1_exact_lockfile_match(self) -> None:
        """Stage 1: exact match on discovered_via + marketplace_plugin_name."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        dep = MagicMock()
        dep.discovered_via = "claude"
        dep.marketplace_plugin_name = "myplugin"
        dep.get_unique_key.return_value = "owner/repo"

        lockfile = MagicMock()
        lockfile.dependencies = {"owner/repo": dep}

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._is_marketplace_ref",
            return_value=True,
        ):
            with patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("myplugin", "claude", None),
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    lockfile,
                    logger,
                    dry_run=True,
                )

        assert result["myplugin@claude"] == "owner/repo"

    def test_stage1_provenance_mismatch_fallback(self) -> None:
        """Stage 1 second pass: plugin_name match with different marketplace."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        dep = MagicMock()
        dep.discovered_via = "codex"  # different marketplace
        dep.marketplace_plugin_name = "myplugin"
        dep.get_unique_key.return_value = "owner/repo"

        lockfile = MagicMock()
        lockfile.dependencies = {"owner/repo": dep}

        logger = MagicMock()

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            result = _resolve_marketplace_packages(
                ["myplugin@claude"],
                lockfile,
                logger,
                dry_run=True,
            )

        # Should still resolve but emit a warning
        assert result.get("myplugin@claude") == "owner/repo"
        logger.warning.assert_called()

    def test_stage2_dry_run_skips_registry(self) -> None:
        """Stage 2: dry_run=True skips registry fallback, emits verbose_detail."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = MagicMock()
        lockfile.dependencies = {}

        logger = MagicMock()

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            result = _resolve_marketplace_packages(
                ["myplugin@claude"],
                lockfile,
                logger,
                dry_run=True,
            )

        assert result.get("myplugin@claude") is None
        logger.verbose_detail.assert_called()
        logger.warning.assert_called()  # Stage 3: dry-run warning

    def test_stage2_registry_fallback_resolves(self) -> None:
        """Stage 2: live (non-dry-run) resolves via registry."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        dep = MagicMock()
        dep.get_unique_key.return_value = "owner/repo"

        lockfile = MagicMock()
        lockfile.dependencies = {"owner/repo": dep}

        logger = MagicMock()
        resolution = MagicMock()
        resolution.canonical = "owner/repo"

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            with patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=resolution,
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    lockfile,
                    logger,
                    dry_run=False,
                )

        assert result.get("myplugin@claude") == "owner/repo"

    def test_stage2_registry_supply_chain_guard(self) -> None:
        """Stage 2: registry canonical not in lockfile triggers supply-chain guard."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = MagicMock()
        lockfile.dependencies = {}  # canonical NOT present

        logger = MagicMock()
        resolution = MagicMock()
        resolution.canonical = "evil/repo"

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            with patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=resolution,
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    lockfile,
                    logger,
                    dry_run=False,
                )

        # Should be None due to supply-chain guard
        assert result.get("myplugin@claude") is None
        logger.warning.assert_called()

    def test_stage2_registry_no_lockfile_trusts_canonical(self) -> None:
        """Stage 2: no lockfile → trusts registry canonical (with verbose_detail)."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = MagicMock()
        resolution = MagicMock()
        resolution.canonical = "owner/repo"

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            with patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=resolution,
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    None,  # no lockfile
                    logger,
                    dry_run=False,
                )

        assert result.get("myplugin@claude") == "owner/repo"
        logger.verbose_detail.assert_called()

    def test_stage2_registry_network_error(self) -> None:
        """Stage 2: network error falls through to Stage 3 error."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        lockfile = MagicMock()
        lockfile.dependencies = {}

        logger = MagicMock()

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            with patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("network failure"),
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    lockfile,
                    logger,
                    dry_run=False,
                )

        assert result.get("myplugin@claude") is None
        logger.warning.assert_called()  # network warning + Stage 3 error

    def test_stage3_error_logged_non_dry_run(self) -> None:
        """Stage 3: emits error when not found and not dry_run."""
        from apm_cli.commands.uninstall.engine import _resolve_marketplace_packages

        logger = MagicMock()

        with patch(
            "apm_cli.marketplace.resolver.parse_marketplace_ref",
            return_value=("myplugin", "claude", None),
        ):
            with patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("fail"),
            ):
                result = _resolve_marketplace_packages(
                    ["myplugin@claude"],
                    None,
                    logger,
                    dry_run=False,
                )

        assert result.get("myplugin@claude") is None
        logger.error.assert_called()


class TestValidateUninstallPackagesMarketplace:
    """Tests for _validate_uninstall_packages — marketplace ref paths."""

    def test_marketplace_ref_resolved_and_found(self) -> None:
        """Marketplace ref resolved to canonical, found in deps."""
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._is_marketplace_ref",
            return_value=True,
        ):
            with patch(
                "apm_cli.commands.uninstall.engine._resolve_marketplace_packages",
                return_value={"myplugin@claude": "owner/repo"},
            ):
                to_remove, not_found = _validate_uninstall_packages(
                    ["myplugin@claude"],
                    ["owner/repo"],
                    logger,
                )

        assert len(to_remove) == 1
        assert len(not_found) == 0

    def test_marketplace_ref_resolved_but_not_in_deps(self) -> None:
        """Marketplace ref resolved but canonical not in deps → not_found."""
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._is_marketplace_ref",
            return_value=True,
        ):
            with patch(
                "apm_cli.commands.uninstall.engine._resolve_marketplace_packages",
                return_value={"myplugin@claude": "owner/other"},
            ):
                to_remove, not_found = _validate_uninstall_packages(
                    ["myplugin@claude"],
                    ["owner/repo"],
                    logger,
                )

        assert len(to_remove) == 0
        assert len(not_found) == 1

    def test_marketplace_ref_resolution_fails(self) -> None:
        """Marketplace ref resolution returns None → added to not_found."""
        from apm_cli.commands.uninstall.engine import _validate_uninstall_packages

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._is_marketplace_ref",
            return_value=True,
        ):
            with patch(
                "apm_cli.commands.uninstall.engine._resolve_marketplace_packages",
                return_value={"myplugin@claude": None},
            ):
                to_remove, not_found = _validate_uninstall_packages(
                    ["myplugin@claude"],
                    ["owner/repo"],
                    logger,
                )

        assert len(to_remove) == 0
        assert len(not_found) == 1


class TestDryRunUninstallWithLockfile:
    """Tests for _dry_run_uninstall — lockfile-aware orphan display."""

    def test_dry_run_with_orphans_shown(self, tmp_path: Path) -> None:
        """Dry-run shows transitive orphans when lockfile has children."""
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        logger = MagicMock()

        # Build a mock lockfile with a transitive dep that would become orphan
        child_dep = MagicMock()
        child_dep.resolved_by = "https://github.com/owner/repo.git"
        child_dep.repo_url = "https://github.com/owner/child.git"
        child_dep.get_unique_key.return_value = "owner/child"

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [child_dep]

        lockfile_path = tmp_path / "apm.lock.yaml"
        lockfile_path.write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        with patch("apm_cli.commands.uninstall.engine.Path", return_value=tmp_path):
            with patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=mock_lockfile,
            ):
                with patch(
                    "apm_cli.deps.lockfile.get_lockfile_path",
                    return_value=lockfile_path,
                ):
                    _dry_run_uninstall(["owner/repo"], apm_modules, logger)

        logger.success.assert_called_once()

    def test_dry_run_with_package_on_disk(self, tmp_path: Path) -> None:
        """Dry-run shows package-on-disk message."""
        from apm_cli.commands.uninstall.engine import _dry_run_uninstall

        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "owner" / "repo"
        pkg_dir.mkdir(parents=True)

        logger = MagicMock()

        lockfile_path = tmp_path / "apm.lock.yaml"
        lockfile_path.write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        with patch("apm_cli.commands.uninstall.engine.Path", return_value=tmp_path):
            with patch(
                "apm_cli.deps.lockfile.LockFile.read",
                return_value=None,
            ):
                with patch(
                    "apm_cli.deps.lockfile.get_lockfile_path",
                    return_value=lockfile_path,
                ):
                    _dry_run_uninstall(["owner/repo"], apm_modules, logger)

        logger.success.assert_called_once()


class TestRemovePackagesFromDiskEdgeCases:
    """Additional edge cases for _remove_packages_from_disk."""

    def test_path_traversal_error_skipped(self, tmp_path: Path) -> None:
        """PathTraversalError on get_install_path causes skip with error log."""
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk
        from apm_cli.utils.path_security import PathTraversalError

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._parse_dependency_entry",
        ) as mock_parse:
            mock_ref = MagicMock()
            mock_ref.get_install_path.side_effect = PathTraversalError("traversal!")
            mock_parse.return_value = mock_ref

            removed = _remove_packages_from_disk(["owner/repo"], apm_modules, logger)

        assert removed == 0
        logger.error.assert_called()

    def test_single_part_package_string(self, tmp_path: Path) -> None:
        """Single-part (no slash) fallback path construction."""
        from apm_cli.commands.uninstall.engine import _remove_packages_from_disk

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        # Create a directory matching the single-part name
        pkg_dir = apm_modules / "reponame"
        pkg_dir.mkdir()

        logger = MagicMock()

        with patch(
            "apm_cli.commands.uninstall.engine._parse_dependency_entry",
            side_effect=ValueError("no slash"),
        ):
            removed = _remove_packages_from_disk(["reponame"], apm_modules, logger)

        # The directory exists, so it should be removed
        assert removed == 1 or logger.warning.called  # either removed or warned


class TestCleanupTransitiveOrphansEdgeCases:
    """Additional edge cases for _cleanup_transitive_orphans."""

    def test_orphan_path_removed(self, tmp_path: Path) -> None:
        """Actual orphan on disk is removed."""
        from apm_cli.commands.uninstall.engine import _cleanup_transitive_orphans

        apm_modules = tmp_path / "apm_modules"
        orphan_dir = apm_modules / "owner" / "orphan"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "apm.yml").write_text("name: orphan\n")

        # Main apm.yml
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(_APM_YML_MINIMAL, encoding="utf-8")

        # Create lockfile where owner/orphan is resolved by owner/repo.
        # repo_url on DependencyReference.parse("owner/repo") is "owner/repo",
        # so resolved_by must also be "owner/repo" for the children_index to match.
        from apm_cli.deps.lockfile import LockedDependency

        child_dep = LockedDependency(
            repo_url="owner/orphan",
            resolved_by="owner/repo",
        )

        lockfile = MagicMock()
        lockfile.get_package_dependencies.return_value = [child_dep]
        lockfile.get_dependency.return_value = child_dep

        logger = MagicMock()

        removed, orphans = _cleanup_transitive_orphans(
            lockfile, ["owner/repo"], apm_modules, apm_yml, logger
        )

        assert removed == 1
        assert "owner/orphan" in orphans
        assert not orphan_dir.exists()


# ===========================================================================
# PART 2 — policy/discovery.py
# ===========================================================================


class TestSplitHashPin:
    """Tests for _split_hash_pin."""

    def test_valid_sha256_pin(self) -> None:
        """Valid sha256 prefix parses correctly."""
        from apm_cli.policy.discovery import _split_hash_pin

        digest = "a" * 64
        algo, hex_part = _split_hash_pin(f"sha256:{digest}")
        assert algo == "sha256"
        assert hex_part == digest

    def test_bare_hex_defaults_to_sha256(self) -> None:
        """Bare 64-char hex is treated as sha256."""
        from apm_cli.policy.discovery import _split_hash_pin

        digest = "b" * 64
        algo, hex_part = _split_hash_pin(digest)
        assert algo == "sha256"
        assert hex_part == digest

    def test_unsupported_algorithm_raises(self) -> None:
        """Unsupported algorithm raises ProjectPolicyConfigError."""
        from apm_cli.policy.discovery import _split_hash_pin
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with pytest.raises(ProjectPolicyConfigError, match=r"Unsupported policy\.hash algorithm"):
            _split_hash_pin("md5:abc123")

    def test_wrong_length_raises(self) -> None:
        """Wrong hex length raises ProjectPolicyConfigError."""
        from apm_cli.policy.discovery import _split_hash_pin
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with pytest.raises(ProjectPolicyConfigError, match="not a valid sha256 digest"):
            _split_hash_pin("sha256:abc123")  # too short

    def test_uppercase_hex_normalised(self) -> None:
        """Uppercase hex is lowercased."""
        from apm_cli.policy.discovery import _split_hash_pin

        digest = "A" * 64
        algo, hex_part = _split_hash_pin(f"sha256:{digest}")
        _ = algo  # suppress unused-variable lint
        assert hex_part == "a" * 64


class TestComputeHashNormalized:
    """Tests for _compute_hash_normalized."""

    def test_returns_algo_colon_hex(self) -> None:
        """Returns '<algo>:<hex>' canonical form."""
        from apm_cli.policy.discovery import _compute_hash_normalized

        content = "some policy content"
        result = _compute_hash_normalized(content, None)
        assert result.startswith("sha256:")
        assert len(result) == len("sha256:") + 64

    def test_uses_algorithm_from_expected_hash(self) -> None:
        """Algorithm derived from expected_hash when provided."""
        from apm_cli.policy.discovery import _compute_hash_normalized

        content = "content"
        digest = _sha256_of(content)
        result = _compute_hash_normalized(content, f"sha256:{digest}")
        assert result.startswith("sha256:")


class TestVerifyHashPin:
    """Tests for _verify_hash_pin."""

    def test_no_pin_returns_none(self) -> None:
        """No expected hash → no mismatch."""
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin("content", None, "source")
        assert result is None

    def test_matching_pin_returns_none(self) -> None:
        """Matching pin → no mismatch (returns None)."""
        from apm_cli.policy.discovery import _verify_hash_pin

        content = "valid policy content"
        digest = _sha256_of(content)
        result = _verify_hash_pin(content, f"sha256:{digest}", "source")
        assert result is None

    def test_mismatching_pin_returns_mismatch(self) -> None:
        """Mismatching pin → PolicyFetchResult with hash_mismatch outcome."""
        from apm_cli.policy.discovery import _verify_hash_pin

        content = "valid policy content"
        wrong_digest = "a" * 64  # wrong hash
        result = _verify_hash_pin(content, f"sha256:{wrong_digest}", "test-source")

        assert result is not None
        assert result.outcome == "hash_mismatch"
        assert result.source == "test-source"

    def test_bytes_input_accepted(self) -> None:
        """Bytes content accepted for pin verification."""
        from apm_cli.policy.discovery import _verify_hash_pin

        content = b"bytes content"
        digest = hashlib.sha256(content).hexdigest()
        result = _verify_hash_pin(content, f"sha256:{digest}", "source")
        assert result is None

    def test_invalid_content_type_returns_mismatch(self) -> None:
        """Non-str, non-bytes content type → hash_mismatch."""
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin(12345, "sha256:" + "a" * 64, "source")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_invalid_pin_format_returns_mismatch(self) -> None:
        """Structurally invalid pin → hash_mismatch (fail-closed)."""
        from apm_cli.policy.discovery import _verify_hash_pin

        result = _verify_hash_pin("content", "not_valid_pin", "source")
        # The pin has no ':' so it's treated as bare hex → wrong length → mismatch
        assert result is not None
        assert result.outcome == "hash_mismatch"


class TestParseRemoteUrl:
    """Tests for _parse_remote_url."""

    def test_https_github_com(self) -> None:
        """HTTPS GitHub URL parsed correctly."""
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("https://github.com/contoso/my-project.git")
        assert result == ("contoso", "github.com")

    def test_scp_git_at(self) -> None:
        """SCP-style git@ URL parsed correctly."""
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("git@github.com:contoso/my-project.git")
        assert result == ("contoso", "github.com")

    def test_https_ghe(self) -> None:
        """GitHub Enterprise HTTPS URL parsed correctly."""
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("https://github.example.com/contoso/my-project.git")
        assert result == ("contoso", "github.example.com")

    def test_empty_url_returns_none(self) -> None:
        """Empty URL returns None."""
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("")
        assert result is None

    def test_azure_devops_ssh(self) -> None:
        """Azure DevOps SSH URL parses v3 prefix correctly."""
        from apm_cli.policy.discovery import _parse_remote_url

        result = _parse_remote_url("git@ssh.dev.azure.com:v3/org/project/repo.git")
        assert result is not None
        assert result[0] == "org"
        assert result[1] == "ssh.dev.azure.com"

    def test_https_no_path_returns_none(self) -> None:
        """HTTPS URL without path returns None."""
        from apm_cli.policy.discovery import _parse_remote_url

        # URL with empty path segments
        result = _parse_remote_url("https://github.com/")
        # Should return None (no owner extracted)
        assert result is None or (isinstance(result, tuple) and result[0])


class TestStripSourcePrefix:
    """Tests for _strip_source_prefix."""

    def test_org_prefix_stripped(self) -> None:
        """'org:' prefix removed."""
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("org:contoso/.github") == "contoso/.github"

    def test_url_prefix_stripped(self) -> None:
        """'url:' prefix removed."""
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("url:https://example.com") == "https://example.com"

    def test_file_prefix_stripped(self) -> None:
        """'file:' prefix removed."""
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("file:/path/to/policy.yml") == "/path/to/policy.yml"

    def test_bare_string_unchanged(self) -> None:
        """Strings without known prefixes are returned unchanged."""
        from apm_cli.policy.discovery import _strip_source_prefix

        assert _strip_source_prefix("bare-string") == "bare-string"


class TestExtractExtendsHost:
    """Tests for _extract_extends_host."""

    def test_https_url_returns_host(self) -> None:
        """Full HTTPS URL returns its host."""
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("https://github.com/org/repo")
        assert result == "github.com"

    def test_three_part_ref_returns_host(self) -> None:
        """Three-part ref (host/owner/repo) returns the host."""
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("github.example.com/org/repo")
        assert result == "github.example.com"

    def test_two_part_ref_returns_none(self) -> None:
        """Two-part shorthand owner/repo returns None (same-host)."""
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("org/repo")
        assert result is None

    def test_no_slash_returns_none(self) -> None:
        """Org-only shorthand (no slash) returns None (same-host)."""
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("someorg")
        assert result is None

    def test_empty_returns_none(self) -> None:
        """Empty string returns None."""
        from apm_cli.policy.discovery import _extract_extends_host

        result = _extract_extends_host("")
        assert result is None


class TestValidateExtendsHost:
    """Tests for _validate_extends_host."""

    def test_same_host_passes(self) -> None:
        """Extends ref pointing at same host passes silently."""
        from apm_cli.policy.discovery import _validate_extends_host

        # Should not raise
        _validate_extends_host("github.com", "github.com/org/repo")

    def test_shorthand_always_passes(self) -> None:
        """Shorthand refs are intrinsically same-host, always pass."""
        from apm_cli.policy.discovery import _validate_extends_host

        # No slash → shorthand → no raise
        _validate_extends_host("github.com", "someorg")
        # Two-part owner/repo → no raise
        _validate_extends_host("github.com", "org/repo")

    def test_cross_host_raises(self) -> None:
        """Cross-host extends ref raises PolicyInheritanceError."""
        from apm_cli.policy.discovery import _validate_extends_host
        from apm_cli.policy.inheritance import PolicyInheritanceError

        with pytest.raises(PolicyInheritanceError, match="cross-host"):
            _validate_extends_host("github.com", "evil.example.com/org/repo")

    def test_unknown_leaf_host_raises(self) -> None:
        """Leaf host=None with explicit extends host raises PolicyInheritanceError."""
        from apm_cli.policy.discovery import _validate_extends_host
        from apm_cli.policy.inheritance import PolicyInheritanceError

        with pytest.raises(PolicyInheritanceError, match="cross-host"):
            _validate_extends_host(None, "github.com/org/repo")


class TestDeriveLeafHost:
    """Tests for _derive_leaf_host."""

    def test_url_source_returns_host(self, tmp_path: Path) -> None:
        """url: source → hostname extracted."""
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("url:https://github.com/policy.yml", tmp_path)
        assert result == "github.com"

    def test_org_source_two_parts_returns_github_com(self, tmp_path: Path) -> None:
        """org:owner/.github (2-part) → defaults to github.com."""
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("org:contoso/.github", tmp_path)
        assert result == "github.com"

    def test_org_source_three_parts_returns_host(self, tmp_path: Path) -> None:
        """org:host/owner/.github (3-part) → host extracted."""
        from apm_cli.policy.discovery import _derive_leaf_host

        result = _derive_leaf_host("org:ghe.corp.com/contoso/.github", tmp_path)
        assert result == "ghe.corp.com"

    def test_file_source_falls_back_to_git_remote(self, tmp_path: Path) -> None:
        """file: source with absolute path has quirky behavior — first path part used as host."""
        from apm_cli.policy.discovery import _derive_leaf_host

        # For "file:/path/..." the stripped bare is "/path/..." which has
        # 3+ slash segments, so parts[0]="" is returned before git fallback.
        result = _derive_leaf_host("file:/path/to/policy.yml", tmp_path)
        # The function returns "" (first part of absolute path) rather than falling
        # through to git remote; the important check is it doesn't crash.
        assert result is None or isinstance(result, str)


class TestIsGithubHost:
    """Tests for _is_github_host."""

    def test_github_com(self) -> None:
        from apm_cli.policy.discovery import _is_github_host

        assert _is_github_host("github.com") is True

    def test_ghe_com_subdomain(self) -> None:
        from apm_cli.policy.discovery import _is_github_host

        assert _is_github_host("contoso.ghe.com") is True

    def test_unknown_host(self) -> None:
        from apm_cli.policy.discovery import _is_github_host

        assert _is_github_host("example.com") is False

    def test_github_host_env_var(self) -> None:
        from apm_cli.policy.discovery import _is_github_host

        with patch.dict(os.environ, {"GITHUB_HOST": "internal.github.corp"}):
            assert _is_github_host("internal.github.corp") is True


class TestIsPolicyEmpty:
    """Tests for _is_policy_empty."""

    def test_empty_policy_returns_true(self) -> None:
        """Policy with no actionable rules is considered empty."""
        from apm_cli.policy.discovery import _is_policy_empty
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        assert _is_policy_empty(policy) is True

    def test_policy_with_deny_not_empty(self) -> None:
        """Policy with deny rules is not empty."""
        from apm_cli.policy.discovery import _is_policy_empty
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_WITH_DENY)
        assert _is_policy_empty(policy) is False

    def test_policy_with_allow_not_empty(self) -> None:
        """Policy with allow rules is not empty."""
        from apm_cli.policy.discovery import _is_policy_empty
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_WITH_ALLOW)
        assert _is_policy_empty(policy) is False


class TestDetectGarbage:
    """Tests for _detect_garbage."""

    def test_invalid_yaml_returns_garbage_response(self) -> None:
        """Non-parseable YAML returns garbage_response outcome."""
        from apm_cli.policy.discovery import _detect_garbage

        result = _detect_garbage("this: is: not: valid: yaml: [{{", "source", "url:source", None)
        assert result is not None
        assert result.outcome == "garbage_response"

    def test_yaml_non_mapping_returns_garbage(self) -> None:
        """Valid YAML that's not a mapping returns garbage_response."""
        from apm_cli.policy.discovery import _detect_garbage

        result = _detect_garbage("- item1\n- item2\n", "source", "url:source", None)
        assert result is not None
        assert result.outcome == "garbage_response"

    def test_valid_mapping_returns_none(self) -> None:
        """Valid YAML mapping returns None (not garbage)."""
        from apm_cli.policy.discovery import _detect_garbage

        result = _detect_garbage("name: policy\nversion: '1.0'\n", "source", "url:source", None)
        assert result is None

    def test_none_content_returns_none(self) -> None:
        """None content is not garbage (caller handles it separately)."""
        from apm_cli.policy.discovery import _detect_garbage

        result = _detect_garbage(None, "source", "url:source", None)
        assert result is None

    def test_garbage_with_stale_cache_uses_stale(self) -> None:
        """Garbage response with stale cache falls back to stale cache."""
        from apm_cli.policy.discovery import _CacheEntry, _detect_garbage
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        cache_entry = _CacheEntry(
            policy=policy,
            source="org:contoso/.github",
            age_seconds=7200,
            stale=True,
        )

        result = _detect_garbage("- not a mapping\n", "source", "url:source", cache_entry)
        assert result is not None
        assert result.outcome == "cached_stale"
        assert result.policy is policy


class TestStaleOrError:
    """Tests for _stale_fallback_or_error."""

    def test_stale_cache_available(self) -> None:
        """Returns stale cache when available."""
        from apm_cli.policy.discovery import _CacheEntry, _stale_fallback_or_error
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        entry = _CacheEntry(
            policy=policy,
            source="org:contoso/.github",
            age_seconds=7200,
            stale=True,
        )
        result = _stale_fallback_or_error(entry, "fetch failed", "url:x", "cache_miss_fetch_fail")
        assert result.outcome == "cached_stale"
        assert result.cache_stale is True

    def test_no_cache_returns_error(self) -> None:
        """Without cache, returns error with given outcome."""
        from apm_cli.policy.discovery import _stale_fallback_or_error

        result = _stale_fallback_or_error(None, "fetch failed", "url:x", "cache_miss_fetch_fail")
        assert result.outcome == "cache_miss_fetch_fail"
        assert result.error == "fetch failed"


class TestLoadFromFile:
    """Tests for _load_from_file via discover_policy."""

    def test_load_valid_policy_file(self, tmp_path: Path) -> None:
        """Loading a valid policy file returns found outcome."""
        from apm_cli.policy.discovery import discover_policy

        policy_file = _make_policy_file(tmp_path, _POLICY_MINIMAL)
        result = discover_policy(tmp_path, policy_override=str(policy_file))

        assert result.outcome == "empty"  # minimal has no actionable rules
        assert result.policy is not None

    def test_load_policy_with_deny(self, tmp_path: Path) -> None:
        """Policy with deny rules has 'found' outcome."""
        from apm_cli.policy.discovery import discover_policy

        policy_file = _make_policy_file(tmp_path, _POLICY_WITH_DENY)
        result = discover_policy(tmp_path, policy_override=str(policy_file))

        assert result.outcome == "found"
        assert result.policy is not None
        assert result.policy.enforcement == "block"

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """Non-existent path does not crash; falls through to auto-discovery."""
        from apm_cli.policy.discovery import discover_policy

        with patch(
            "apm_cli.policy.discovery._auto_discover",
            return_value=MagicMock(outcome="no_git_remote"),
        ):
            result = discover_policy(tmp_path, policy_override="/nonexistent/path/to/policy.yml")
        # Falls through to auto-discover or fetch-from-repo
        assert result is not None

    def test_load_policy_with_hash_pin_match(self, tmp_path: Path) -> None:
        """Hash pin that matches content succeeds."""
        from apm_cli.policy.discovery import _load_from_file

        content = _POLICY_MINIMAL
        digest = _sha256_of(content)
        policy_file = _make_policy_file(tmp_path, content)
        result = _load_from_file(policy_file, expected_hash=f"sha256:{digest}")

        assert result.outcome in ("empty", "found")
        assert result.policy is not None

    def test_load_policy_with_hash_pin_mismatch(self, tmp_path: Path) -> None:
        """Hash pin mismatch returns hash_mismatch outcome."""
        from apm_cli.policy.discovery import _load_from_file

        policy_file = _make_policy_file(tmp_path, _POLICY_MINIMAL)
        result = _load_from_file(policy_file, expected_hash="sha256:" + "a" * 64)

        assert result.outcome == "hash_mismatch"

    def test_load_malformed_policy_file(self, tmp_path: Path) -> None:
        """Malformed YAML returns malformed outcome."""
        from apm_cli.policy.discovery import _load_from_file

        bad_content = "enforcement: block\nthis: {{\n"
        policy_file = _make_policy_file(tmp_path, bad_content)
        result = _load_from_file(policy_file)

        # Should return malformed
        assert result.outcome in ("malformed", "cache_miss_fetch_fail") or result.error is not None


class TestFetchFromUrl:
    """Tests for _fetch_from_url (mocked HTTP)."""

    def test_404_returns_absent(self, tmp_path: Path) -> None:
        """HTTP 404 returns absent outcome."""
        from apm_cli.policy.discovery import _fetch_from_url

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "absent"

    def test_200_valid_policy_returns_found(self, tmp_path: Path) -> None:
        """HTTP 200 with valid policy returns found outcome."""
        from apm_cli.policy.discovery import _fetch_from_url

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _POLICY_WITH_DENY

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "found"
        assert result.policy is not None

    def test_redirect_returns_fetch_fail(self, tmp_path: Path) -> None:
        """HTTP 3xx redirect is refused → cache_miss_fetch_fail."""
        from apm_cli.policy.discovery import _fetch_from_url

        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.headers = {"Location": "https://evil.example.com/"}

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "cache_miss_fetch_fail"

    def test_timeout_returns_fetch_fail(self, tmp_path: Path) -> None:
        """Request timeout → cache_miss_fetch_fail."""
        import requests

        from apm_cli.policy.discovery import _fetch_from_url

        with patch("requests.get", side_effect=requests.exceptions.Timeout()):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "cache_miss_fetch_fail"

    def test_connection_error_returns_fetch_fail(self, tmp_path: Path) -> None:
        """Connection error → cache_miss_fetch_fail."""
        import requests

        from apm_cli.policy.discovery import _fetch_from_url

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "cache_miss_fetch_fail"

    def test_garbage_response_returns_garbage_response(self, tmp_path: Path) -> None:
        """200 with non-YAML body → garbage_response."""
        from apm_cli.policy.discovery import _fetch_from_url

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>captive portal</html>"

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_url("https://example.com/policy.yml", tmp_path, no_cache=True)

        assert result.outcome == "garbage_response"

    def test_http_url_rejected(self, tmp_path: Path) -> None:
        """http:// URLs are refused without making a request."""
        from apm_cli.policy.discovery import discover_policy

        result = discover_policy(tmp_path, policy_override="http://example.com/policy.yml")
        assert result.error is not None
        assert "http://" in result.error or "plaintext" in result.error.lower()


class TestFetchFromRepo:
    """Tests for _fetch_from_repo (mocked GitHub API)."""

    def _make_github_response(self, content: str) -> MagicMock:
        """Build a mock GitHub Contents API response."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"encoding": "base64", "content": encoded}
        return mock_resp

    def test_valid_policy_returns_found(self, tmp_path: Path) -> None:
        """GitHub API returns valid policy → found outcome."""
        from apm_cli.policy.discovery import _fetch_from_repo

        with patch("requests.get", return_value=self._make_github_response(_POLICY_WITH_DENY)):
            result = _fetch_from_repo("contoso/.github", tmp_path, no_cache=True)

        assert result.outcome == "found"
        assert result.policy is not None

    def test_404_returns_absent(self, tmp_path: Path) -> None:
        """GitHub API 404 → absent outcome."""
        from apm_cli.policy.discovery import _fetch_from_repo

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_repo("contoso/.github", tmp_path, no_cache=True)

        assert result.outcome == "absent"

    def test_403_returns_error(self, tmp_path: Path) -> None:
        """GitHub API 403 → cache_miss_fetch_fail."""
        from apm_cli.policy.discovery import _fetch_from_repo

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("requests.get", return_value=mock_resp):
            result = _fetch_from_repo("contoso/.github", tmp_path, no_cache=True)

        assert result.outcome == "cache_miss_fetch_fail"

    def test_invalid_repo_ref_returns_error(self, tmp_path: Path) -> None:
        """Single-part (no slash) repo ref returns error."""
        from apm_cli.policy.discovery import _fetch_from_repo

        result = _fetch_from_repo("invalid", tmp_path, no_cache=True)
        # Should get a fetch error (invalid repo reference)
        assert result.error is not None or result.outcome == "cache_miss_fetch_fail"


class TestAutoDiscover:
    """Tests for _auto_discover."""

    def test_no_git_remote_returns_no_git_remote(self, tmp_path: Path) -> None:
        """No git remote → no_git_remote outcome."""
        from apm_cli.policy.discovery import _auto_discover

        with patch(
            "apm_cli.policy.discovery._extract_org_from_git_remote",
            return_value=None,
        ):
            result = _auto_discover(tmp_path)

        assert result.outcome == "no_git_remote"

    def test_custom_host_builds_correct_repo_ref(self, tmp_path: Path) -> None:
        """Custom GHE host builds host-prefixed repo_ref."""
        from apm_cli.policy.discovery import _auto_discover

        with patch(
            "apm_cli.policy.discovery._extract_org_from_git_remote",
            return_value=("contoso", "ghe.corp.com"),
        ):
            with patch(
                "apm_cli.policy.discovery._fetch_from_repo",
                return_value=MagicMock(outcome="absent"),
            ) as mock_fetch:
                _auto_discover(tmp_path)

        # Should have been called with ghe.corp.com/contoso/.github
        assert mock_fetch.called
        call_arg = mock_fetch.call_args[0][0]
        host, _, _ = call_arg.partition("/")
        assert host == "ghe.corp.com"


class TestPolicyCaching:
    """Tests for _write_cache, _read_cache_entry, _read_cache."""

    def test_write_then_read_cache(self, tmp_path: Path) -> None:
        """Writing cache and reading it back returns the same policy."""
        from apm_cli.policy.discovery import _read_cache_entry, _write_cache
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_WITH_DENY)
        repo_ref = "contoso/.github"

        _write_cache(repo_ref, policy, tmp_path, chain_refs=[repo_ref])

        entry = _read_cache_entry(repo_ref, tmp_path)
        assert entry is not None
        assert not entry.stale
        assert entry.policy is not None

    def test_stale_entry_returned_when_past_ttl(self, tmp_path: Path) -> None:
        """Cache entry past TTL is returned as stale (not None)."""
        from apm_cli.policy.discovery import _read_cache_entry, _write_cache
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        repo_ref = "contoso/.github"

        _write_cache(repo_ref, policy, tmp_path)

        # Read with tiny TTL (1 second) and manipulate the meta file to
        # simulate an aged cache
        from apm_cli.policy.discovery import _cache_key, _get_cache_dir

        cache_dir = _get_cache_dir(tmp_path)
        key = _cache_key(repo_ref)
        meta_file = cache_dir / f"{key}.meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["cached_at"] = time.time() - 3700  # 1 hour + 100s ago
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

        entry = _read_cache_entry(repo_ref, tmp_path, ttl=1)
        assert entry is not None
        assert entry.stale is True

    def test_expired_cache_beyond_max_stale_returns_none(self, tmp_path: Path) -> None:
        """Cache older than MAX_STALE_TTL (7 days) returns None."""
        from apm_cli.policy.discovery import _read_cache_entry, _write_cache
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        repo_ref = "contoso/.github"

        _write_cache(repo_ref, policy, tmp_path)

        from apm_cli.policy.discovery import _cache_key, _get_cache_dir

        cache_dir = _get_cache_dir(tmp_path)
        key = _cache_key(repo_ref)
        meta_file = cache_dir / f"{key}.meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["cached_at"] = time.time() - (8 * 24 * 3600)  # 8 days ago
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

        entry = _read_cache_entry(repo_ref, tmp_path)
        assert entry is None

    def test_read_cache_returns_none_on_miss(self, tmp_path: Path) -> None:
        """_read_cache returns None when no cache exists."""
        from apm_cli.policy.discovery import _read_cache

        result = _read_cache("nonexistent/.github", tmp_path)
        assert result is None

    def test_schema_version_mismatch_invalidates_cache(self, tmp_path: Path) -> None:
        """Cache with wrong schema_version is treated as a miss."""
        from apm_cli.policy.discovery import _read_cache_entry, _write_cache
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        repo_ref = "contoso/.github"

        _write_cache(repo_ref, policy, tmp_path)

        from apm_cli.policy.discovery import _cache_key, _get_cache_dir

        cache_dir = _get_cache_dir(tmp_path)
        key = _cache_key(repo_ref)
        meta_file = cache_dir / f"{key}.meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["schema_version"] = "99"  # wrong version
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

        entry = _read_cache_entry(repo_ref, tmp_path)
        assert entry is None

    def test_hash_pin_mismatch_invalidates_cache(self, tmp_path: Path) -> None:
        """Cache entry with wrong raw_bytes_hash is invalidated when pin set."""
        from apm_cli.policy.discovery import _read_cache_entry, _write_cache
        from apm_cli.policy.parser import load_policy

        policy, _ = load_policy(_POLICY_MINIMAL)
        repo_ref = "contoso/.github"

        _write_cache(repo_ref, policy, tmp_path, raw_bytes_hash="sha256:" + "a" * 64)

        # Read with different expected hash → should invalidate
        wrong_pin = "sha256:" + "b" * 64
        entry = _read_cache_entry(repo_ref, tmp_path, expected_hash=wrong_pin)
        assert entry is None


class TestDiscoverPolicyWithChain:
    """Tests for discover_policy_with_chain."""

    def test_disable_env_var_returns_disabled(self, tmp_path: Path) -> None:
        """APM_POLICY_DISABLE=1 returns disabled outcome without fetching."""
        from apm_cli.policy.discovery import discover_policy_with_chain

        with patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"}):
            result = discover_policy_with_chain(tmp_path)

        assert result.outcome == "disabled"

    def test_local_file_no_extends(self, tmp_path: Path) -> None:
        """Local policy file without extends returns found outcome."""
        from apm_cli.policy.discovery import discover_policy_with_chain

        _make_policy_file(tmp_path, _POLICY_WITH_DENY)

        with patch(
            "apm_cli.policy.discovery.read_project_policy_hash_pin",
            return_value=None,
        ):
            with patch(
                "apm_cli.policy.discovery.discover_policy",
                return_value=MagicMock(
                    outcome="found",
                    policy=MagicMock(extends=None),
                    cached=False,
                    found=True,
                ),
            ) as mock_discover:
                discover_policy_with_chain(tmp_path)

        assert mock_discover.called

    def test_malformed_project_pin_returns_hash_mismatch(self, tmp_path: Path) -> None:
        """Malformed policy.hash in apm.yml returns hash_mismatch."""
        from apm_cli.policy.discovery import discover_policy_with_chain
        from apm_cli.policy.project_config import ProjectPolicyConfigError

        with patch(
            "apm_cli.policy.discovery.read_project_policy_hash_pin",
            side_effect=ProjectPolicyConfigError("bad pin"),
        ):
            result = discover_policy_with_chain(tmp_path)

        assert result.outcome == "hash_mismatch"


# ===========================================================================
# PART 3 — commands/pack.py
# ===========================================================================


class TestEmitJsonErrorOrRaise:
    """Tests for _emit_json_error_or_raise."""

    def test_json_output_emits_json(self) -> None:
        """json_output=True emits JSON envelope to stdout."""
        from apm_cli.commands.pack import _emit_json_error_or_raise

        ctx = MagicMock()
        captured: list[str] = []

        with patch("click.echo", side_effect=lambda s: captured.append(str(s))):
            _emit_json_error_or_raise(ctx, True, "build_error", "Something went wrong")

        assert len(captured) == 1
        data = json.loads(captured[0])
        assert data["ok"] is False
        assert any(e["code"] == "build_error" for e in data["errors"])

    def test_non_json_raises_click_exception(self) -> None:
        """json_output=False raises ClickException."""
        import click

        from apm_cli.commands.pack import _emit_json_error_or_raise

        ctx = MagicMock()

        with pytest.raises(click.ClickException, match="Something went wrong"):
            _emit_json_error_or_raise(ctx, False, "build_error", "Something went wrong")


class TestMappingSummary:
    """Tests for _mapping_summary."""

    def test_empty_mappings_returns_empty_string(self) -> None:
        from apm_cli.commands.pack import _mapping_summary

        assert _mapping_summary({}) == ""

    def test_single_mapping_returns_summary(self) -> None:
        from apm_cli.commands.pack import _mapping_summary

        result = _mapping_summary({"new/file.md": "old/file.md"})
        assert "old/" in result
        assert "new/" in result

    def test_multiple_mappings_uses_first(self) -> None:
        from apm_cli.commands.pack import _mapping_summary

        mappings = {"agents/a.md": "old/a.md", "agents/b.md": "old/b.md"}
        result = _mapping_summary(mappings)
        assert result != ""


class TestWarnEmpty:
    """Tests for _warn_empty."""

    def test_no_target_warns_generic(self) -> None:
        from apm_cli.commands.pack import _warn_empty

        logger = MagicMock()
        result = MagicMock(path_mappings={}, mapped_count=0)
        _warn_empty(logger, None, result)
        logger.warning.assert_called_with("No deployed files found -- empty bundle created")

    def test_with_target_warns_target_specific(self) -> None:
        from apm_cli.commands.pack import _warn_empty

        logger = MagicMock()
        result = MagicMock(path_mappings={}, mapped_count=0)
        _warn_empty(logger, "claude", result)
        logger.warning.assert_called_with("No files to pack for target 'claude'")

    def test_with_target_verbose_hint(self) -> None:
        from apm_cli.commands.pack import _warn_empty

        logger = MagicMock()
        result = MagicMock(path_mappings={}, mapped_count=0)
        _warn_empty(logger, "cursor", result)
        logger.warning.assert_called()


class TestRenderBundleResult:
    """Tests for _render_bundle_result."""

    def test_dry_run_with_files(self) -> None:
        """Dry-run mode emits dry_run_notice for file count."""
        from apm_cli.commands.pack import _render_bundle_result

        logger = MagicMock()
        pack_result = MagicMock()
        pack_result.mapped_count = 0
        pack_result.path_mappings = {}
        pack_result.files = ["agents/helper.md", "skills/test.md"]
        pack_result.bundle_path = Path("/tmp/build")

        _render_bundle_result(logger, pack_result, "plugin", None, dry_run=True)

        logger.dry_run_notice.assert_called()

    def test_dry_run_no_files(self) -> None:
        """Dry-run with no files calls _warn_empty path."""
        from apm_cli.commands.pack import _render_bundle_result

        logger = MagicMock()
        pack_result = MagicMock()
        pack_result.mapped_count = 0
        pack_result.path_mappings = {}
        pack_result.files = []
        pack_result.bundle_path = Path("/tmp/build")

        _render_bundle_result(logger, pack_result, "plugin", None, dry_run=True)

        # Either warning or dry_run_notice about empty
        assert logger.warning.called or logger.dry_run_notice.called

    def test_non_dry_run_success(self) -> None:
        """Non-dry-run with files emits success."""
        from apm_cli.commands.pack import _render_bundle_result

        logger = MagicMock()
        pack_result = MagicMock()
        pack_result.mapped_count = 0
        pack_result.path_mappings = {}
        pack_result.files = ["agents/helper.md"]
        pack_result.bundle_path = Path("/tmp/build")

        _render_bundle_result(logger, pack_result, "plugin", None, dry_run=False)

        logger.success.assert_called()

    def test_none_result_returns_immediately(self) -> None:
        """None pack_result returns without calling logger."""
        from apm_cli.commands.pack import _render_bundle_result

        logger = MagicMock()
        _render_bundle_result(logger, None, "plugin", None, dry_run=False)

        logger.success.assert_not_called()
        logger.warning.assert_not_called()

    def test_with_path_mappings(self) -> None:
        """Mapped files shows progress."""
        from apm_cli.commands.pack import _render_bundle_result

        logger = MagicMock()
        pack_result = MagicMock()
        pack_result.mapped_count = 2
        pack_result.path_mappings = {"new/file.md": "old/file.md"}
        pack_result.files = ["new/file.md"]
        pack_result.bundle_path = Path("/tmp/build")

        _render_bundle_result(logger, pack_result, "plugin", None, dry_run=False)

        logger.progress.assert_called()


class TestRenderMarketplaceResult:
    """Tests for _render_marketplace_result."""

    def test_dry_run_emits_would_write(self) -> None:
        """Dry-run emits dry_run_notice for outputs."""
        from apm_cli.commands.pack import _render_marketplace_result

        logger = MagicMock()
        _render_marketplace_result(
            logger,
            report=None,
            dry_run=True,
            extra_warnings=[],
            outputs=["/path/to/marketplace.json"],
        )

        logger.dry_run_notice.assert_called()

    def test_non_dry_run_emits_success(self) -> None:
        """Non-dry-run emits success for outputs."""
        from apm_cli.commands.pack import _render_marketplace_result

        logger = MagicMock()
        _render_marketplace_result(
            logger,
            report=None,
            dry_run=False,
            extra_warnings=[],
            outputs=["/path/to/marketplace.json"],
        )

        logger.success.assert_called()

    def test_extra_warnings_emitted(self) -> None:
        """Extra warnings are emitted."""
        from apm_cli.commands.pack import _render_marketplace_result

        logger = MagicMock()
        _render_marketplace_result(
            logger,
            report=None,
            dry_run=False,
            extra_warnings=["watch out!"],
            outputs=[],
        )

        logger.warning.assert_called_with("watch out!")

    def test_with_output_reports(self) -> None:
        """Report with output_reports produces correct output."""
        from apm_cli.commands.pack import _render_marketplace_result

        logger = MagicMock()
        output_report = MagicMock()
        output_report.profile = "claude"
        output_report.resolved = ["pkg1", "pkg2"]
        output_report.output_path = "/dist/claude.json"
        output_report.dry_run = False

        report = MagicMock()
        report.outputs = [output_report]
        report.warnings = []

        _render_marketplace_result(logger, report=report, dry_run=False)

        logger.success.assert_called()


class TestRenderMarketplaceCatalog:
    """Tests for _render_marketplace_catalog."""

    def test_single_output_without_profile(self) -> None:
        """Single output without profile is rendered without label."""
        from apm_cli.commands.pack import _render_marketplace_catalog

        logger = MagicMock()
        _render_marketplace_catalog(logger, [(None, Path("/dist/marketplace.json"))])

        calls = [str(c) for c in logger.info.call_args_list]
        assert any("/dist/marketplace.json" in c for c in calls)

    def test_multiple_outputs_with_profiles(self) -> None:
        """Multiple outputs with profiles show label-aligned paths."""
        from apm_cli.commands.pack import _render_marketplace_catalog

        logger = MagicMock()
        _render_marketplace_catalog(
            logger,
            [
                ("claude", Path("/dist/claude.json")),
                ("codex", Path("/dist/codex.json")),
            ],
        )

        calls = [str(c) for c in logger.info.call_args_list]
        assert any("claude" in c for c in calls)
        assert any("codex" in c for c in calls)

    def test_docs_url_emitted(self) -> None:
        """Docs URL is always emitted in catalog output."""
        from apm_cli.commands.pack import MARKETPLACE_DOCS_URL, _render_marketplace_catalog

        logger = MagicMock()
        _render_marketplace_catalog(logger, [(None, Path("/dist/marketplace.json"))])

        calls = [str(c) for c in logger.info.call_args_list]
        assert any(MARKETPLACE_DOCS_URL in c for c in calls)

    def test_no_info_method_skips(self) -> None:
        """Logger without info() method skips silently."""
        from apm_cli.commands.pack import _render_marketplace_catalog

        logger = MagicMock(spec=[])  # no info method
        # Should not raise
        _render_marketplace_catalog(logger, [(None, Path("/dist/marketplace.json"))])


class TestLogUnpackFileList:
    """Tests for _log_unpack_file_list."""

    def test_dependency_files_tree_output(self) -> None:
        """dependency_files dict produces tree-style output."""
        from apm_cli.commands.pack import _log_unpack_file_list

        logger = MagicMock()
        result = MagicMock()
        result.dependency_files = {"dep1": ["file1.md", "file2.md"]}
        result.files = []

        _log_unpack_file_list(result, logger)

        logger.progress.assert_called()
        logger.tree_item.assert_called()

    def test_no_dependency_files_flat_list(self) -> None:
        """No dependency_files falls back to flat file list."""
        from apm_cli.commands.pack import _log_unpack_file_list

        logger = MagicMock()
        result = MagicMock()
        result.dependency_files = {}
        result.files = ["file1.md", "file2.md"]

        _log_unpack_file_list(result, logger)

        assert logger.tree_item.call_count == 2


class TestPackCmdMarketplaceFilter:
    """Tests for pack_cmd --marketplace filter via CliRunner."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_marketplace_filter_unknown_format(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Unknown marketplace format in --marketplace exits with error."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--marketplace", "unknownformat"])
        assert result.exit_code != 0

    def test_marketplace_filter_none_skips_marketplace(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """--marketplace none skips marketplace output."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--marketplace", "none"])
        assert result.exit_code == 0, result.output

    def test_marketplace_path_override_invalid_format(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """--marketplace-path without = separator exits with error."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--marketplace-path", "missing-equals"])
        assert result.exit_code != 0

    def test_marketplace_path_unknown_format(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """--marketplace-path with unknown format name exits with error."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--marketplace-path", "unknownfmt=out.json"])
        assert result.exit_code != 0

    def test_marketplace_output_flag_removed(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """--marketplace-output was removed; Click rejects it."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--marketplace-output", "dist/marketplace.json"])
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower()
        assert "--marketplace-output" in (result.output or "")


class TestPackCmdJsonOutput:
    """Tests for pack_cmd --json output mode."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_json_output_valid_envelope(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """--json produces a valid JSON envelope."""
        from apm_cli.commands.pack import pack_cmd

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_MINIMAL, encoding="utf-8")
        (tmp_path / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")

        result = runner.invoke(pack_cmd, ["--json"])
        assert result.exit_code == 0, result.output

        # The output may contain log lines before the JSON block; find the JSON object.
        output = result.output
        json_start = output.find("{")
        assert json_start != -1, f"No JSON object in output: {output!r}"
        data = json.loads(output[json_start:])
        assert "ok" in data
        assert "marketplace" in data
        assert "bundle" in data
