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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.phases.policy_gate import PolicyViolationError, run
from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _compute_hash_normalized,
    _verify_hash_pin,
)
from apm_cli.policy.project_config import (
    ProjectPolicyConfigError,
    compute_policy_hash,
    parse_project_policy_hash_pin,
)

pytestmark = pytest.mark.unit

_VALID_POLICY_YAML = "name: org-policy\nversion: '1.0'\nenforcement: warn\n"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha384(content: str) -> str:
    return hashlib.sha384(content.encode("utf-8")).hexdigest()


# =====================================================================
# _verify_hash_pin: low-level helper
# =====================================================================


class TestVerifyHashPin:
    def test_default_and_bare_pin_remain_sha256(self) -> None:
        """Default hashing and case-insensitive bare pins retain SHA-256 semantics."""
        digest = _sha256(_VALID_POLICY_YAML)
        assert compute_policy_hash(_VALID_POLICY_YAML) == digest
        assert _compute_hash_normalized(_VALID_POLICY_YAML, None) == f"sha256:{digest}"
        assert _verify_hash_pin(_VALID_POLICY_YAML, digest.upper(), "file:x") is None

    @pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
    @pytest.mark.parametrize("content", ["name: caf\u00e9\r\n", b"\xffraw\r\nbytes"])
    def test_explicit_sha2_preserves_raw_bytes(self, algorithm: str, content: str | bytes) -> None:
        """All approved algorithms retain their exact byte-level digest."""
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        constructors = {
            "sha256": hashlib.sha256,
            "sha384": hashlib.sha384,
            "sha512": hashlib.sha512,
        }
        expected = constructors[algorithm](raw_bytes).hexdigest()
        with patch("hashlib.new", side_effect=AssertionError("dynamic digest selection")):
            assert compute_policy_hash(content, algorithm) == expected
            assert _verify_hash_pin(content, f"{algorithm}:{expected}", "file:x") is None
            mismatch = _verify_hash_pin(content, f"{algorithm}:{'0' * len(expected)}", "file:x")
        assert mismatch is not None
        assert mismatch.outcome == "hash_mismatch"
        assert mismatch.policy is None
        assert mismatch.raw_bytes_hash == f"{algorithm}:{expected}"

    @pytest.mark.parametrize("algorithm", ["md5", "sha1", "blake2b", "unknown"])
    def test_unsupported_algorithms_still_fail_closed(self, algorithm: str) -> None:
        """Neither direct hashing nor discovery accepts an unapproved digest."""
        with pytest.raises(ProjectPolicyConfigError, match="unsupported algorithm"):
            compute_policy_hash("payload", algorithm)
        result = _verify_hash_pin("payload", f"{algorithm}:{'0' * 64}", "file:x")
        assert result is not None
        assert result.outcome == "hash_mismatch"
        assert result.policy is None

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


# =====================================================================
# read_project_policy_hash_pin: end-to-end IO
# =====================================================================
