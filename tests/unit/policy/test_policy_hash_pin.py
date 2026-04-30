"""Tests for the project-side ``policy.hash`` pin (#827).

Covers:
- Pin matches -> policy applies normally
- Pin mismatches -> install fails closed regardless of fetch_failure
- No pin -> existing behavior preserved
- Garbage response with pin -> fail closed (mismatch on garbage bytes)
- Alternate algorithm (sha384) accepted
- Malformed pin rejected at parse time
- Hash computed on raw bytes (semantically equivalent YAML differs)
"""

from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.phases.policy_gate import PolicyViolationError, run
from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _verify_hash_pin,
    discover_policy_with_chain,
)
from apm_cli.policy.project_config import (
    ProjectPolicyConfigError,
    parse_project_policy_hash_pin,
    read_project_policy_hash_pin,
)

_VALID_POLICY_YAML = "name: org-policy\nversion: '1.0'\nenforcement: warn\n"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha384(content: str) -> str:
    return hashlib.sha384(content.encode("utf-8")).hexdigest()


# =====================================================================
# _verify_hash_pin: low-level helper
# =====================================================================


class TestVerifyHashPin:
    def test_no_pin_returns_none(self):
        assert _verify_hash_pin("anything", None, "file:x") is None

    def test_match_returns_none(self):
        digest = _sha256(_VALID_POLICY_YAML)
        result = _verify_hash_pin(_VALID_POLICY_YAML, f"sha256:{digest}", "file:x")
        assert result is None

    def test_mismatch_returns_hash_mismatch_outcome(self):
        wrong = "0" * 64
        result = _verify_hash_pin(_VALID_POLICY_YAML, f"sha256:{wrong}", "file:x")
        assert result is not None
        assert result.outcome == "hash_mismatch"
        assert result.policy is None
        assert "expected sha256:" in result.error
        assert "got sha256:" in result.error

    def test_bytes_input_accepted(self):
        digest = hashlib.sha256(b"raw bytes").hexdigest()
        assert _verify_hash_pin(b"raw bytes", f"sha256:{digest}", "file:x") is None

    def test_invalid_pin_treated_as_mismatch(self):
        result = _verify_hash_pin("x", "sha256:not-hex", "file:x")
        assert result is not None
        assert result.outcome == "hash_mismatch"

    def test_sha384_supported(self):
        digest = _sha384(_VALID_POLICY_YAML)
        result = _verify_hash_pin(_VALID_POLICY_YAML, f"sha384:{digest}", "file:x")
        assert result is None

    def test_hash_computed_on_raw_bytes_not_parsed(self):
        # Two YAML strings that parse to the same dict but differ byte-wise.
        a = "name: x\nversion: '1.0'\n"
        b = "version: '1.0'\nname: x\n"
        digest_a = _sha256(a)
        # The pin is taken from `a`. Verifying `b` against it must fail
        # even though `b` parses to the same data.
        assert _verify_hash_pin(a, f"sha256:{digest_a}", "x") is None
        mismatch = _verify_hash_pin(b, f"sha256:{digest_a}", "x")
        assert mismatch is not None
        assert mismatch.outcome == "hash_mismatch"


# =====================================================================
# parse_project_policy_hash_pin: malformed pins rejected at parse time
# =====================================================================


class TestParsePolicyHashPin:
    def test_no_block_returns_none(self):
        assert parse_project_policy_hash_pin(None) is None

    def test_no_hash_key_returns_none(self):
        assert parse_project_policy_hash_pin({"unrelated": "x"}) is None

    def test_valid_sha256_pin_accepted(self):
        digest = _sha256("payload")
        pin = parse_project_policy_hash_pin({"hash": f"sha256:{digest}"})
        assert pin is not None
        assert pin.algorithm == "sha256"
        assert pin.digest == digest

    def test_valid_bare_hex_accepted(self):
        digest = _sha256("payload")
        pin = parse_project_policy_hash_pin({"hash": digest})
        assert pin is not None
        assert pin.normalized == f"sha256:{digest}"

    def test_sha384_pin_accepted(self):
        digest = _sha384("payload")
        pin = parse_project_policy_hash_pin(
            {"hash_algorithm": "sha384", "hash": f"sha384:{digest}"}
        )
        assert pin is not None
        assert pin.algorithm == "sha384"

    def test_md5_rejected(self):
        with pytest.raises(ProjectPolicyConfigError):
            parse_project_policy_hash_pin({"hash_algorithm": "md5", "hash": "x" * 32})

    def test_wrong_length_rejected(self):
        with pytest.raises(ProjectPolicyConfigError):
            parse_project_policy_hash_pin({"hash": "abc123"})

    def test_non_hex_rejected(self):
        with pytest.raises(ProjectPolicyConfigError):
            parse_project_policy_hash_pin({"hash": "z" * 64})

    def test_prefix_mismatch_rejected(self):
        digest = _sha256("payload")
        with pytest.raises(ProjectPolicyConfigError):
            parse_project_policy_hash_pin({"hash_algorithm": "sha256", "hash": f"sha384:{digest}"})

    def test_non_string_rejected(self):
        with pytest.raises(ProjectPolicyConfigError):
            parse_project_policy_hash_pin({"hash": 12345})


# =====================================================================
# discover_policy_with_chain: end-to-end pin enforcement on a file source
# =====================================================================


def _write_apm_yml(root: Path, *, pin: str | None) -> None:
    if pin is None:
        (root / "apm.yml").write_text("name: proj\nversion: '1.0'\n", encoding="utf-8")
    else:
        (root / "apm.yml").write_text(
            textwrap.dedent(f"""\
                name: proj
                version: '1.0'
                policy:
                  hash: "{pin}"
                """),
            encoding="utf-8",
        )


class TestDiscoverPolicyWithChainHashPin:
    def _patch_file_discovery(self, content: str):
        """Patch _fetch_from_repo / _auto_discover so tests don't need git."""
        return patch(
            "apm_cli.policy.discovery._auto_discover",
            return_value=PolicyFetchResult(
                policy=None,
                source="org:fake/.github",
                outcome="cache_miss_fetch_fail",
                error="patched",
            ),
        )

    def test_no_pin_no_apm_yml_passes_through(self, tmp_path: Path):
        # Sanity: without apm.yml or pin, discover_policy_with_chain runs
        # auto-discovery normally (returns whatever _auto_discover yields).
        with patch("apm_cli.policy.discovery.discover_policy") as mock_disc:
            mock_disc.return_value = PolicyFetchResult(policy=None, outcome="absent")
            result = discover_policy_with_chain(tmp_path)
            assert result.outcome == "absent"
            _, kwargs = mock_disc.call_args
            assert kwargs.get("expected_hash") is None

    def test_malformed_pin_in_apm_yml_returns_hash_mismatch(self, tmp_path: Path):
        _write_apm_yml(tmp_path, pin="sha256:not-hex-garbage")
        result = discover_policy_with_chain(tmp_path)
        assert result.outcome == "hash_mismatch"
        assert "Invalid policy.hash" in (result.error or "")

    def test_pin_threads_through_to_discover_policy(self, tmp_path: Path):
        digest = _sha256("anything")
        _write_apm_yml(tmp_path, pin=f"sha256:{digest}")
        with patch("apm_cli.policy.discovery.discover_policy") as mock_disc:
            mock_disc.return_value = PolicyFetchResult(policy=None, outcome="absent")
            discover_policy_with_chain(tmp_path)
            _, kwargs = mock_disc.call_args
            assert kwargs.get("expected_hash") == f"sha256:{digest}"

    def test_pin_match_on_file_source_returns_found(self, tmp_path: Path):
        # File-based override exercises the leaf hashing path end-to-end.
        policy_file = tmp_path / "apm-policy.yml"
        policy_file.write_text(_VALID_POLICY_YAML, encoding="utf-8")
        digest = _sha256(_VALID_POLICY_YAML)

        from apm_cli.policy.discovery import discover_policy

        result = discover_policy(
            tmp_path,
            policy_override=str(policy_file),
            expected_hash=f"sha256:{digest}",
        )
        assert result.outcome in ("found", "empty")
        assert result.policy is not None
        assert result.raw_bytes_hash == f"sha256:{digest}"

    def test_pin_mismatch_on_file_source_returns_hash_mismatch(self, tmp_path: Path):
        policy_file = tmp_path / "apm-policy.yml"
        policy_file.write_text(_VALID_POLICY_YAML, encoding="utf-8")
        wrong = "0" * 64

        from apm_cli.policy.discovery import discover_policy

        result = discover_policy(
            tmp_path,
            policy_override=str(policy_file),
            expected_hash=f"sha256:{wrong}",
        )
        assert result.outcome == "hash_mismatch"
        assert result.policy is None


# =====================================================================
# policy_gate: hash_mismatch always raises regardless of fetch_failure
# =====================================================================


@dataclass
class _FakeCtx:
    project_root: Path = field(default_factory=lambda: Path("/tmp/fake"))
    apm_dir: Path = field(default_factory=lambda: Path("/tmp/fake/.apm"))
    verbose: bool = False
    logger: Any = None
    deps_to_install: list[Any] = field(default_factory=list)
    existing_lockfile: Any = None
    policy_fetch: Any = None
    policy_enforcement_active: bool = False
    no_policy: bool = False
    policy_fetch_failure_default: str = "warn"


_PATCH_DISCOVER = "apm_cli.install.phases.policy_gate._discover_with_chain"


class TestPolicyGateHashMismatch:
    @patch(_PATCH_DISCOVER)
    def test_hash_mismatch_with_warn_default_still_blocks(self, mock_discover):
        mock_discover.return_value = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:fake/.github",
            error="expected sha256:aaa, got sha256:bbb",
        )
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="warn",
        )
        with pytest.raises(PolicyViolationError) as exc:
            run(ctx)
        assert "hash mismatch" in str(exc.value).lower()

    @patch(_PATCH_DISCOVER)
    def test_hash_mismatch_with_block_default_blocks(self, mock_discover):
        mock_discover.return_value = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:fake/.github",
            error="expected sha256:aaa, got sha256:bbb",
        )
        ctx = _FakeCtx(
            logger=MagicMock(),
            policy_fetch_failure_default="block",
        )
        with pytest.raises(PolicyViolationError):
            run(ctx)

    @patch(_PATCH_DISCOVER)
    def test_hash_mismatch_logs_via_policy_discovery_miss(self, mock_discover):
        mock_discover.return_value = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:fake/.github",
            error="expected sha256:aaa, got sha256:bbb",
        )
        logger = MagicMock()
        ctx = _FakeCtx(logger=logger)
        with pytest.raises(PolicyViolationError):
            run(ctx)
        logger.policy_discovery_miss.assert_called_once()
        _, kwargs = logger.policy_discovery_miss.call_args
        assert kwargs.get("outcome") == "hash_mismatch"


# =====================================================================
# install_preflight: hash_mismatch raises PolicyBlockError
# =====================================================================


class TestPreflightHashMismatch:
    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_hash_mismatch_raises_block_error(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import (
            PolicyBlockError,
            run_policy_preflight,
        )

        mock_discover.return_value = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:fake/.github",
            error="expected sha256:aaa, got sha256:bbb",
        )
        with pytest.raises(PolicyBlockError) as exc:
            run_policy_preflight(
                project_root=tmp_path,
                apm_deps=[],
                no_policy=False,
                logger=MagicMock(),
            )
        assert "hash mismatch" in str(exc.value).lower()

    @patch("apm_cli.policy.install_preflight.discover_policy_with_chain")
    def test_hash_mismatch_dry_run_does_not_raise(self, mock_discover, tmp_path: Path):
        from apm_cli.policy.install_preflight import run_policy_preflight

        mock_discover.return_value = PolicyFetchResult(
            outcome="hash_mismatch",
            source="org:fake/.github",
            error="expected sha256:aaa, got sha256:bbb",
        )
        result, active = run_policy_preflight(
            project_root=tmp_path,
            apm_deps=[],
            no_policy=False,
            logger=MagicMock(),
            dry_run=True,
        )
        assert active is False
        assert result.outcome == "hash_mismatch"


# =====================================================================
# read_project_policy_hash_pin: end-to-end IO
# =====================================================================


class TestReadProjectPolicyHashPin:
    def test_no_apm_yml_returns_none(self, tmp_path: Path):
        assert read_project_policy_hash_pin(tmp_path) is None

    def test_no_policy_block_returns_none(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("name: x\nversion: '1.0'\n", encoding="utf-8")
        assert read_project_policy_hash_pin(tmp_path) is None

    def test_valid_pin_returns_object(self, tmp_path: Path):
        digest = _sha256("payload")
        (tmp_path / "apm.yml").write_text(
            f"name: x\nversion: '1.0'\npolicy:\n  hash: 'sha256:{digest}'\n",
            encoding="utf-8",
        )
        pin = read_project_policy_hash_pin(tmp_path)
        assert pin is not None
        assert pin.normalized == f"sha256:{digest}"

    def test_malformed_pin_raises(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text(
            "name: x\nversion: '1.0'\npolicy:\n  hash: 'sha256:bogus'\n",
            encoding="utf-8",
        )
        with pytest.raises(ProjectPolicyConfigError):
            read_project_policy_hash_pin(tmp_path)
