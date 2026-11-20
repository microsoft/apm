"""Tests for the project-side ``policy.hash`` pin (#827) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from typing import Any, List  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.policy.discovery import (
    PolicyFetchResult,
    _compute_hash_normalized,
    _fetch_from_repo,
    _write_cache,
    discover_policy_with_chain,
)
from apm_cli.policy.project_config import (
    ProjectPolicyConfigError,
    read_project_policy_hash_pin,
)
from apm_cli.policy.schema import ApmPolicy

pytestmark = pytest.mark.component

_VALID_POLICY_YAML = "name: org-policy\nversion: '1.0'\nenforcement: warn\n"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# =====================================================================
# _verify_hash_pin: low-level helper
# =====================================================================


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_existing_sha2_cache_digest_remains_compatible(tmp_path: Path, algorithm: str) -> None:
    """Existing digest sidecars remain usable; changed pins still fail closed."""
    constructors = {
        "sha256": hashlib.sha256,
        "sha384": hashlib.sha384,
        "sha512": hashlib.sha512,
    }
    digest = constructors[algorithm](_VALID_POLICY_YAML.encode("utf-8")).hexdigest()
    pin = f"{algorithm}:{digest}"
    policy = ApmPolicy(name="cached-policy", enforcement="block")
    _write_cache("org/.github", policy, tmp_path, raw_bytes_hash=pin)
    with patch("hashlib.new", side_effect=AssertionError("dynamic digest selection")):
        assert _compute_hash_normalized(_VALID_POLICY_YAML, pin.upper()) == pin
        cached = _fetch_from_repo(
            "org/.github", tmp_path, expected_hash=pin.upper(), cache_only=True
        )
        mismatch = _fetch_from_repo(
            "org/.github",
            tmp_path,
            expected_hash=f"{algorithm}:{'0' * len(digest)}",
            cache_only=True,
        )
    assert cached.cached is True
    assert cached.policy == policy
    assert cached.raw_bytes_hash == pin
    assert mismatch.outcome == "hash_mismatch"
    assert mismatch.policy is None


# =====================================================================
# parse_project_policy_hash_pin: malformed pins rejected at parse time
# =====================================================================


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
